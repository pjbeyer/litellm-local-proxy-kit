"""Offline sink diagnostics; run each case in a fresh process, never in a live profile."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import uuid
from typing import Any

CASES = ('symlink', 'fifo', 'rotation', 'privacy', 'record_size')
CAP = 256 * 1024
KEYS = {'ts', 'category', 'code', 'tool'}
LINE = (json.dumps({'ts': '2000-01-01T00:00:00+0000', 'category': 'VETO',
                    'code': 'LGV-001', 'tool': 'terminal'}, separators=(',', ':')) + chr(10)).encode('ascii')


def fixture_valid():
    try:
        if not LINE.endswith(bytes((10,))) or LINE.count(bytes((10,))) != 1:
            return False
        record = json.loads(LINE[:-1].decode('ascii'))
        return (record == {'ts': '2000-01-01T00:00:00+0000', 'category': 'VETO',
                           'code': 'LGV-001', 'tool': 'terminal'}
                and LINE == (json.dumps(record, separators=(',', ':')) + chr(10)).encode('ascii'))
    except (UnicodeError, ValueError, TypeError):
        return False


def exercise(case, guard, active, root, capture, helper):
    if case == 'symlink':
        protected = 0
        for mode in (0o600, 0o640):
            victim = root / ('victim-%03o.jsonl' % mode)
            victim.write_bytes(LINE)
            os.chmod(victim, mode)
            active.symlink_to(victim)
            target = os.readlink(active)
            accepted = guard.sink_write('VETO', 'LGV-001', 'terminal')
            capture()
            protected += int(accepted is False and active.is_symlink()
                             and os.readlink(active) == target
                             and victim.read_bytes() == LINE
                             and victim.stat().st_mode & 0o7777 == mode)
            active.unlink()
        return protected == 2, {'protected_variants': protected}
    if case == 'fifo':
        os.mkfifo(active, 0o600)
        os.chmod(active, 0o600)
        answer = {}
        def write_fifo():
            try:
                answer['accepted'] = guard.sink_write('VETO', 'LGV-001', 'terminal')
            except Exception:
                answer['error'] = True
        thread = threading.Thread(target=write_fifo, daemon=True)
        helper[0] = thread
        start = time.monotonic()
        thread.start()
        thread.join(4)
        seconds = round(time.monotonic() - start, 3)
        capture()
        done = not thread.is_alive()
        refused = answer.get('accepted') is False
        return done and refused, {'join_completed': done, 'returned_false': refused,
                                  'seconds': seconds}
    if case == 'rotation':
        repetitions = CAP // len(LINE)
        seed = LINE * repetitions
        valid_seed = (repetitions > 0 and len(seed) <= CAP
                      and len(seed) + len(LINE) > CAP
                      and seed.endswith(bytes((10,)))
                      and seed.count(bytes((10,))) == repetitions
                      and all(line == LINE[:-1] for line in seed.splitlines()))
        if not valid_seed:
            return False, {'valid_fixture': False}
        active.write_bytes(seed)
        os.chmod(active, 0o600)
        destination = active.with_name('decisions.jsonl.1')
        destination.mkdir(mode=0o700)
        accepted = guard.sink_write('VETO', 'LGV-001', 'terminal')
        capture()
        unchanged = active.read_bytes() == seed
        within_cap = active.stat().st_size <= CAP
        occupied = destination.is_dir()
        refused = accepted is False
        return unchanged and within_cap and occupied and refused, {
            'valid_fixture': valid_seed, 'preexisting_bytes': len(seed),
            'would_overflow': len(seed) + len(LINE) > CAP,
            'active_unchanged': unchanged, 'within_cap': within_cap,
            'occupied_directory': occupied, 'returned_false': refused}
    started = guard.sink_start() is True
    capture()
    if not started:
        return False, {'started': False}
    metadata = ('fixture/private/4711.terminal' if case == 'privacy'
                else 'fixture-tool-' + 'x' * guard.SINK_MAX_BYTES)
    guard.log_decision('VETO', metadata)
    flushed = guard.sink_flush(timeout_s=2) is True
    capture()
    exists = active.is_file()
    raw = active.read_bytes() if exists else b''
    terminated = raw.endswith(bytes((10,))) and raw.count(bytes((10,))) == 1
    record = json.loads(raw[:-1].decode('ascii')) if terminated else None
    schema = isinstance(record, dict) and set(record) == KEYS
    if not isinstance(record, dict) or not schema:
        return False, {'schema_exact': False, 'file_exists': exists,
                       'flushed': flushed, 'jsonl_terminated': terminated}
    category = schema and record['category'] == 'VETO'
    code = schema and record['code'] == 'LGV-001'
    bounded = 1 <= len(raw) <= 512 and len(raw) <= CAP
    if case == 'privacy':
        tool = schema and record['tool'] == 'terminal'
        absent = metadata.encode('ascii') not in raw
        ok = flushed and exists and terminated and bounded and schema and category and code and tool and absent
        return ok, {'started': started, 'flushed': flushed, 'file_exists': exists,
                    'jsonl_terminated': terminated, 'record_bytes': len(raw),
                    'bounded': bounded, 'schema_exact': schema,
                    'category_veto': category, 'code_lgv_001': code,
                    'tool_terminal': tool, 'metadata_absent': absent}
    tool = schema and record['tool'] == 'other'
    ok = flushed and exists and terminated and bounded and schema and category and code and tool
    return ok, {'started': started, 'flushed': flushed, 'file_exists': exists,
                'jsonl_terminated': terminated, 'schema_exact': schema,
                'category_veto': category, 'code_lgv_001': code,
                'tool_other': tool, 'record_bytes': len(raw), 'bounded': bounded}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--scratch-root', required=True, type=Path)
    parser.add_argument('case', choices=CASES)
    args = parser.parse_args()
    root = None
    guard = None
    helper = [None]
    workers = []
    baseline = set(threading.enumerate())
    ok = False
    evidence: dict[str, Any] = {'setup_failure': True}
    cleanup_ok = False
    def capture():
        for thread in threading.enumerate():
            if thread not in baseline and thread is not helper[0] and thread not in workers:
                workers.append(thread)
    try:
        if not fixture_valid():
            raise ValueError()
        source = args.source_root.resolve(strict=True)
        scratch = args.scratch_root.resolve(strict=True)
        module_path = source / 'integrations/hermes/proxy-lifecycle-guard/guard.py'
        if not source.is_dir() or not scratch.is_dir() or not module_path.is_file():
            raise ValueError()
        root = Path(tempfile.mkdtemp(prefix='guard-probe-', dir=scratch))
        previous = os.environ.get('XDG_STATE_HOME')
        try:
            state_home = root / 'state'
            state = state_home / 'hermes' / 'lifecycle-guard'
            state.mkdir(parents=True, mode=0o700)
            os.chmod(state, 0o700)
            os.environ['XDG_STATE_HOME'] = str(state_home)
            try:
                name = '_offline_guard_' + uuid.uuid4().hex
                spec = importlib.util.spec_from_file_location(name, module_path)
                if spec is None or spec.loader is None:
                    raise ValueError()
                guard = importlib.util.module_from_spec(spec)
                sys.modules[name] = guard
                spec.loader.exec_module(guard)
                capture()
                if type(guard.SINK_MAX_BYTES) is not int or guard.SINK_MAX_BYTES != CAP:
                    ok, evidence = False, {'sink_max_bytes_exact': False}
                else:
                    ok, evidence = exercise(args.case, guard, state / 'decisions.jsonl',
                                            root, capture, helper)
            except Exception:
                ok, evidence = False, {'probe_exception': True}
            finally:
                capture()
                stopped = False
                if guard is not None:
                    try:
                        stopped = guard.sink_shutdown(timeout_s=2) is True
                    except Exception:
                        pass
                capture()
                worker_alive = sum(thread.is_alive() for thread in workers)
                helper_alive = helper[0] is not None and helper[0].is_alive()
                cleanup_ok = stopped and worker_alive == 0 and not helper_alive
                if not cleanup_ok:
                    ok = False
                    evidence.update({'cleanup_complete': False,
                                     'worker_threads_alive': worker_alive,
                                     'fifo_helper_alive': helper_alive,
                                     'retained_fixture': True})
        finally:
            if previous is None:
                os.environ.pop('XDG_STATE_HOME', None)
            else:
                os.environ['XDG_STATE_HOME'] = previous
    except Exception:
        ok, evidence = False, {'setup_failure': True}
    if root is not None and cleanup_ok:
        try:
            shutil.rmtree(root)
        except Exception:
            ok, evidence = False, {'cleanup_failure': True}
    print(json.dumps({'case': args.case, 'safety_invariant_passed': bool(ok),
                      'scope': 'offline disposable state', 'evidence': evidence},
                     separators=(',', ':')))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
