"""Offline actual-evaluate probe with a deliberately stalled telemetry writer.

Only disposable source/policy/state fixtures are used. Protected command text
is evaluator input, never executed. Discovery is injected; native process APIs
are counting fail-if-reached fences. Each invocation is a fresh process.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from typing import Any, cast


def main():
    if not __debug__:
        raise RuntimeError('offline probe requires assertion-enabled execution')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--scratch-root', required=True, type=Path)
    args = parser.parse_args()
    source = args.source_root / 'integrations/hermes/proxy-lifecycle-guard'
    root = Path(tempfile.mkdtemp(prefix='sink-evaluate-', dir=args.scratch_root))
    release, entered = threading.Event(), threading.Event()
    previous_home = os.environ.get('XDG_STATE_HOME')
    module = None
    first_worker = None
    stopped = True
    result = None
    try:
        digests = {}
        for name in ('guard.py', '__init__.py'):
            data = (source / name).read_bytes()
            target = root / name
            target.write_bytes(data)
            target.chmod(0o600)
            digests[name] = hashlib.sha256(data).hexdigest()
        policy = {
            'schema_version': 1, 'protected_port': 4000,
            'protected_labels': ['example.litellm'],
            'protected_session_handles': [],
            'bounds': {'input_text_bytes': 262144, 'max_chunks': 512,
                       'max_depth': 8, 'discovery_timeout_ms': 2000,
                       'max_snapshot_pids': 256},
            'refusal_vocabulary_version': 1,
        }
        policy_path = root / 'policy.json'
        policy_bytes = json.dumps(policy).encode('utf-8')
        policy_path.write_bytes(policy_bytes)
        policy_path.chmod(0o600)
        ref = root / 'integrity.sha256'
        ref.write_text(json.dumps({
            'policy_digest': hashlib.sha256(policy_bytes).hexdigest(),
            'source_digests': digests,
        }))
        ref.chmod(0o600)
        state = root / 'state'
        os.environ['XDG_STATE_HOME'] = str(state)
        spec = importlib.util.spec_from_file_location('offline_stalled_sink_guard', root / 'guard.py')
        assert spec is not None and spec.loader is not None
        module = cast(Any, importlib.util.module_from_spec(spec))
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        ledger = {'native_process': 0, 'replacement_thread': 0,
                  'wrong_persist_thread': 0, 'persist': 0}

        def forbidden_process(*unused, **kwargs):
            ledger['native_process'] += 1
            raise AssertionError('offline process fence reached')

        class ProcessFence:
            run = staticmethod(forbidden_process)
            Popen = staticmethod(forbidden_process)
            check_output = staticmethod(forbidden_process)

        module.subprocess = ProcessFence()

        def persist(home, record, cap):
            if threading.current_thread() is not module._sink_worker:
                ledger['wrong_persist_thread'] += 1
                return False
            ledger['persist'] += 1
            entered.set()
            release.wait()  # only the main-thread finally releases this stall
            return True

        module._sink_persist = persist
        assert module.sink_start()
        stopped = False
        first_worker = module._sink_worker
        deadline = time.monotonic() + 1
        while not module._sink_admit('VETO', 'LGV-001', 'terminal'):
            assert time.monotonic() < deadline, 'fixture admission failed'
            time.sleep(0.001)
        assert entered.wait(1), 'writer never entered artificial stall'
        assert first_worker is not None and first_worker.daemon and first_worker.is_alive()
        real_threading = module.threading

        class ThreadFence:
            def __getattr__(self, name):
                return getattr(real_threading, name)
            def Thread(self, *unused, **kwargs):
                ledger['replacement_thread'] += 1
                raise AssertionError('replacement thread attempted')

        module.threading = ThreadFence()
        expected_message = (
            'Blocked by the LiteLLM proxy lifecycle guard: this request could stop '
            'the protected proxy, or its safety could not be established. Use the '
            'owner-reviewed controlled restart workflow; do not substitute another '
            'kill command or execution path.'
        )
        completed_ports = []

        def discover(port):
            snapshot = module.Snapshot(
                pids=frozenset({4711}), pgids=frozenset({4711}),
                comms=frozenset({'litellm'}), args=('litellm --port 4000',))
            assert 4711 in snapshot.pids
            completed_ports.append(port)
            return snapshot

        timings = []
        for unused in range(80):
            began = time.monotonic()
            decision = module.evaluate(
                'terminal', {'command': 'pkill -f litellm'}, policy_path,
                discover=discover)
            assert len(completed_ports) == unused + 1
            assert completed_ports[-1] == 4000
            timings.append(time.monotonic() - began)
            assert decision['action'] == 'block'
            assert decision['category'] == 'VETO'
            assert decision['log_code'] == 'LGV-001'
            assert decision['message'] == expected_message
            assert module.sink_start(), 'existing worker must remain the only worker'
        assert completed_ports == [4000] * 80
        status = module.sink_status()
        assert status['queued'] == 64 and status['in_flight'] == 1
        assert status['admitted'] == 65
        assert module._sink_worker is first_worker
        assert ledger == {'native_process': 0, 'replacement_thread': 0,
                          'wrong_persist_thread': 0, 'persist': 1}
        assert not state.exists(), 'fake persistence must not create state'
        assert max(timings) < 1.0, 'evaluate stalled behind writer'
        result = {'scope': 'offline actual evaluator, injected stalled writer',
                  'source_digests': digests, 'protected_refusals': len(timings),
                  'completed_discovery_calls': len(completed_ports),
                  'max_evaluate_seconds': round(max(timings), 6),
                  'pending': status['queued'], 'in_flight': status['in_flight'],
                  'fenced_process_api_calls': ledger['native_process'],
                  'replacement_thread_attempts': ledger['replacement_thread']}
    finally:
        release.set()
        try:
            if module is not None:
                stopped = False
                stopped = module.sink_shutdown(5.0)
                current_worker = getattr(module, '_sink_worker', None)
                stopped = stopped and all(
                    worker is None or not worker.is_alive()
                    for worker in (first_worker, current_worker))
        finally:
            if previous_home is None:
                os.environ.pop('XDG_STATE_HOME', None)
            else:
                os.environ['XDG_STATE_HOME'] = previous_home
        if stopped:
            shutil.rmtree(root)
        else:
            raise AssertionError('worker not stopped; disposable fixture retained')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
