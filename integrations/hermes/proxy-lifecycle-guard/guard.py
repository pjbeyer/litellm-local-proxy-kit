"""Bounded LiteLLM proxy lifecycle guard for Hermes tool calls.

Evaluates a tool call before execution and blocks patterns that could stop or
disrupt a protected LiteLLM proxy service. Practical command-pattern
protection only — this is intentionally NOT an arbitrary-code sandbox.

Public API (used by the parent plugin shim):

    evaluate(tool_name, args, policy_path) -> None | {"action": "block", "message": ...}

Policy config (JSON, strict schema, loaded fresh on every evaluate call so
PID/session configuration is never stale):

    {"version": 1, "port": 4000,
     "labels": ["example.litellm"],
     "protected_sessions": ["fixture-session"]}

Coverage:
- terminal command strings, execute_code/browser_exec code strings, and
  deferred tool_call payloads ({'calls': [{'name', 'arguments'}, ...]},
  recursing through nested tool_call entries and covering terminal and
  process_manage calls inside them).
- kill/pkill/killall (literal PIDs, process groups, command substitutions,
  lsof/fuser/ps pipelines), fuser -k, launchctl mutators targeting configured
  labels/plists or a whole GUI domain, brew services stop/restart/kill
  targeting litellm or --all, os.kill/os.killpg and kill-family subprocess
  argv in inline Python.
- sudo/env/nohup/timeout wrappers and compound commands (hazard in any
  segment blocks the whole call).

Dynamic discovery (read-only, bounded) runs only for potential process-kill
operations that need PID resolution: /usr/sbin/lsof -t -nP -iTCP:<port>
-sTCP:LISTEN with a timeout, then /bin/ps -p <pids> -o pid=,pgid=,comm=,args=.
Discovery output is never printed. No environment variables are read.

Intentional limitations (documented, not hidden):
- Residual risk: hidden/imported/encoded code, indirection the matcher cannot
  resolve, and model-controlled edits to the policy file itself remain
  outside this guarantee.
- kill targets that cannot be resolved deterministically (shell variables,
  unbound command substitutions, failed process discovery) are refused
  conservatively; read-only probes (kill -0, ps, lsof, health checks) pass.
- Broad interpreter selectors (python, uvicorn, ...) in pkill/killall are
  blocked because they could match the live proxy interpreter.
- No auto-approve tokens, no bypass, and no restart-controller surface.
- Every internal failure fails closed for execution tools: exceptions never
  open the gate.
"""

from __future__ import annotations

import json
import ast
import hashlib
import os
import threading
import re
import subprocess
import shlex
from collections import deque
from dataclasses import dataclass
from pathlib import Path

__all__ = ["evaluate", "load_policy", "discover_snapshot", "Policy",
           "Snapshot", "BLOCK_MESSAGE", "CATEGORIES", "DECISION_MESSAGES",
           "LOG_CODES", "log_decision", "sink_start", "sink_flush",
           "sink_shutdown", "sink_status"]

import logging
import stat
import time

try:
    import fcntl
except ImportError:
    fcntl = None

logger = logging.getLogger('proxy_lifecycle_guard')

SINK_MAX_BYTES = 256 * 1024
SINK_DIR_MODE = 0o700
SINK_FILE_MODE = 0o600
_SINK_RECORD_MAX = 512
_SINK_TOOLS = frozenset({
    'terminal', 'execute_code', 'browser_exec', 'tool_call',
    'process_manage', 'other',
})
_SINK_KEYS = frozenset({'ts', 'category', 'code', 'tool'})
_SINK_TS_RE = re.compile(
    r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:'
    r'[0-9]{2}:[0-9]{2}[+-][0-9]{4}'
)
_SINK_LF = bytes((10,))


def _sink_tool(tool):
    if type(tool) is not str or len(tool) > 128:
        return 'other'
    terminal = tool.rsplit('.', 1)[-1].lower()
    return terminal if terminal in _SINK_TOOLS else 'other'


def _sink_pair(category, code):
    return (type(category) is str and type(code) is str
            and len(category) <= 128 and len(code) <= 128
            and CATEGORIES.get(category) == code)


def _sink_record(category, code, tool):
    if not _sink_pair(category, code):
        return None
    try:
        ts = time.strftime('%Y-%m-%dT%H:%M:%S+0000', time.gmtime())
        record = json.dumps({
            'ts': ts, 'category': category, 'code': code,
            'tool': _sink_tool(tool),
        }, separators=(',', ':'), ensure_ascii=True).encode('ascii') + _SINK_LF
        return record if len(record) <= _SINK_RECORD_MAX else None
    except Exception:
        return None


def _sink_home():
    """Resolve the state home ONCE at capture, following environment symlinks.

    The environment-provided PREFIX (e.g. TMPDIR on macOS, where /var is a
    symlink to /private/var) is trusted infrastructure, not attacker
    material; the hardened no-follow walk protects the MANAGED tail
    (hermes/lifecycle-guard and the sink files) only. Normalizing the
    prefix here keeps prefix symlinks from defeating every persist, while
    the walk still refuses symlinks inside the managed hierarchy.
    """
    try:
        base = os.environ.get('XDG_STATE_HOME')
        if base is not None and type(base) is not str:
            return None
        if not base:
            home = os.environ.get('HOME')
            if type(home) is not str or not home or len(home) > 4096:
                return None
            base = home.rstrip('/') + '/.local/state'
        if len(base) > 4096 or len(base.encode('utf-8')) > 4096:
            return None
        resolved = os.path.realpath(base)
        if (type(resolved) is not str or not resolved
                or not resolved.startswith('/')
                or resolved.startswith('//')
                or len(resolved) > 4096
                or len(resolved.encode('utf-8')) > 4096
                or chr(0) in resolved):
            return None
        return resolved
    except (UnicodeError, ValueError, OSError):
        return None


def _sink_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _sink_valid_line(line):
    if type(line) is not bytes or not line or len(line) + 1 > _SINK_RECORD_MAX:
        return False
    try:
        decoded = line.decode('utf-8')
        if decoded.startswith('\ufeff'):
            return False
        item = json.loads(decoded, object_pairs_hook=_sink_pairs)
        if type(item) is not dict or set(item) != _SINK_KEYS:
            return False
        if any(type(item[key]) is not str for key in _SINK_KEYS):
            return False
        if not _sink_pair(item['category'], item['code']):
            return False
        if item['tool'] not in _SINK_TOOLS:
            return False
        ts = item['ts']
        if len(ts) > 32 or _SINK_TS_RE.fullmatch(ts) is None:
            return False
        time.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')
        return int(ts[20:22]) <= 23 and int(ts[22:24]) <= 59
    except Exception:
        return False


def _sink_valid_content(fd, max_bytes):
    size = os.fstat(fd).st_size
    if size < 0 or size > max_bytes:
        return None
    os.lseek(fd, 0, os.SEEK_SET)
    remaining = size
    chunks = []
    while remaining:
        chunk = os.read(fd, min(remaining, 65536))
        if not chunk or len(chunk) > remaining:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.fstat(fd).st_size != size:
        return None
    data = b''.join(chunks)
    if data:
        if not data.endswith(_SINK_LF):
            return None
        if any(not _sink_valid_line(line)
               for line in data[:-1].split(_SINK_LF)):
            return None
    return size


def _sink_open_dir(parent_fd, name, managed, flags):
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, SINK_DIR_MODE, dir_fd=parent_fd)
        except FileExistsError:
            pass
        fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('unsafe directory')
        if managed:
            if info.st_uid != os.getuid():
                raise ValueError('unowned directory')
            if stat.S_IMODE(info.st_mode) != SINK_DIR_MODE:
                os.fchmod(fd, SINK_DIR_MODE)
            info = os.fstat(fd)
            if (not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != SINK_DIR_MODE):
                raise ValueError('unsafe directory')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _sink_file_info(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1):
        raise ValueError('unsafe file')
    return info


def _sink_harden_file(fd):
    info = _sink_file_info(fd)
    if stat.S_IMODE(info.st_mode) != SINK_FILE_MODE:
        os.fchmod(fd, SINK_FILE_MODE)
    info = _sink_file_info(fd)
    if stat.S_IMODE(info.st_mode) != SINK_FILE_MODE:
        raise ValueError('unsafe file mode')


def _sink_open_file(dir_fd, name, create, flags, exclusive=False):
    if exclusive and not create:
        raise ValueError('exclusive open requires creation')
    created = False
    if exclusive:
        fd = os.open(name, flags | os.O_CREAT | os.O_EXCL,
                     SINK_FILE_MODE, dir_fd=dir_fd)
        created = True
    else:
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            if not create:
                return None
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL,
                         SINK_FILE_MODE, dir_fd=dir_fd)
            created = True
    try:
        _sink_file_info(fd)
        if created:
            _sink_harden_file(fd)
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _sink_persist(state_home, record, max_bytes):
    if (type(max_bytes) is not int or not 0 < max_bytes <= SINK_MAX_BYTES
            or type(record) is not bytes or not record
            or len(record) > min(max_bytes, _SINK_RECORD_MAX)
            or not record.endswith(_SINK_LF)
            or not _sink_valid_line(record[:-1])
            or type(state_home) is not str or len(state_home) > 4096):
        return False
    try:
        if (fcntl is None or not all(hasattr(os, name) for name in (
                'O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK',
                'O_APPEND', 'O_CREAT', 'O_EXCL', 'getuid',
                'fchmod', 'ftruncate'))):
            return False
        if (not state_home.startswith('/') or chr(0) in state_home
                or state_home.startswith('//')):
            return False
        if len(state_home.encode('utf-8')) > 4096:
            return False
        full_path = state_home.rstrip('/') + '/hermes/lifecycle-guard'
        if len(full_path.encode('utf-8')) > 4096:
            return False
        parts = [part for part in state_home.split('/') if part]
        if len(parts) + 2 > 64 or any(part in ('.', '..') for part in parts):
            return False
    except (UnicodeError, ValueError):
        return False

    dir_fd = lock_fd = active_fd = rotated_fd = None
    try:
        directory_flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                           | os.O_NONBLOCK)
        file_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        append_flags = file_flags | os.O_APPEND
        dir_fd = os.open('/', directory_flags)
        for part in parts:
            next_fd = _sink_open_dir(dir_fd, part, False, directory_flags)
            old_fd = dir_fd
            dir_fd = next_fd
            os.close(old_fd)
        for part in ('hermes', 'lifecycle-guard'):
            next_fd = _sink_open_dir(dir_fd, part, True, directory_flags)
            old_fd = dir_fd
            dir_fd = next_fd
            os.close(old_fd)

        lock_fd = _sink_open_file(dir_fd, 'decisions.jsonl.lock',
                                  True, file_flags)
        if lock_fd is None:
            return False
        _sink_harden_file(lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        rotated_fd = _sink_open_file(dir_fd, 'decisions.jsonl.1',
                                     False, file_flags)
        if (rotated_fd is not None
                and _sink_valid_content(rotated_fd, max_bytes) is None):
            return False
        active_fd = _sink_open_file(dir_fd, 'decisions.jsonl',
                                    False, append_flags)
        if active_fd is None:
            active_fd = _sink_open_file(dir_fd, 'decisions.jsonl',
                                        True, append_flags, exclusive=True)
        if active_fd is None:
            return False
        prior_size = _sink_valid_content(active_fd, max_bytes)
        if prior_size is None:
            return False

        if rotated_fd is not None:
            _sink_harden_file(rotated_fd)
        _sink_harden_file(active_fd)

        if prior_size + len(record) > max_bytes:
            if prior_size == 0:
                return False
            old_fd = active_fd
            active_fd = None
            os.close(old_fd)
            if rotated_fd is not None:
                old_fd = rotated_fd
                rotated_fd = None
                os.close(old_fd)
            os.replace('decisions.jsonl', 'decisions.jsonl.1',
                       src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            active_fd = _sink_open_file(dir_fd, 'decisions.jsonl',
                                        True, append_flags, exclusive=True)
            if active_fd is None:
                return False
            prior_size = _sink_valid_content(active_fd, max_bytes)
            if prior_size != 0:
                return False
            _sink_harden_file(active_fd)

        if os.fstat(active_fd).st_size != prior_size:
            return False
        try:
            written = os.write(active_fd, record)
        except Exception:
            try:
                os.ftruncate(active_fd, prior_size)
            except Exception:
                pass
            return False
        if written != len(record):
            try:
                os.ftruncate(active_fd, prior_size)
            except Exception:
                pass
            return False
        return True
    except Exception:
        return False
    finally:
        for fd in (active_fd, rotated_fd, lock_fd, dir_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def sink_write(category, code, tool_name):
    record = _sink_record(category, code, tool_name)
    if record is None:
        return False
    state_home = _sink_home()
    if state_home is None:
        return False
    return _sink_persist(state_home, record, SINK_MAX_BYTES)

# ---------------------------------------------------------------------------
# Bounded asynchronous sink worker (decision-sink contract D4/D5/D6/D12).
#
# Importing this module starts nothing and touches no filesystem: all state
# is inert per-module globals. log_decision admits capped immutable jobs
# (state home, encoded record, byte cap all captured at submission) with a
# nonblocking try-lock; busy/full/offline/wedged/startup-failure drops
# telemetry without touching the enforcement decision. Only sink_start,
# called by the adapter outside any callback, creates the single daemon
# worker; no callback ever starts, restarts, or replaces it, exactly one
# attempt per generation is latched (a failure is never retried or
# replaced), and publication plus start are serialized under the state
# lock so shutdown can never join an unstarted thread. Storage I/O
# happens only in the worker via the unchanged _sink_persist, always
# outside the state lock. The worker waits while registered (no
# idle-exit race) and exits only on a shutdown request, draining its
# queue first. flush/shutdown deadlines cover lock acquisition, waits,
# joins, and any reacquisition, and shutdown publishes its stopping
# flag before touching the lock. Diagnostics are fixed non-secret
# counters saturated at _SINK_COUNTER_MAX plus fixed flags: no record,
# path, or environment material.
# ---------------------------------------------------------------------------

_SINK_QUEUE_MAX = 64
_SINK_TIMEOUT_MAX_S = 60.0
_SINK_COUNTER_MAX = (1 << 63) - 1  # event-counter saturation bound

_sink_lock = threading.Lock()
_sink_cond = threading.Condition(_sink_lock)
_sink_queue = deque()  # immutable (home, record, cap) tuples, bounded
_sink_worker = None
_sink_started = False
_sink_stopping = False
_sink_start_attempted = False  # one start attempt per generation (D5)
_sink_start_failed = False  # latched: never retried or replaced
_sink_busy_drop = False  # busy-lock drop seen (flag, not exact count)
_sink_pre_drop = False  # pre-admission drop seen (record/home/exception)
_sink_in_flight = 0
_sink_admitted = 0
_sink_written = 0
_sink_failed = 0
_sink_dropped = 0
_sink_discarded = 0


def _sink_timeout_ok(timeout_s):
    return (type(timeout_s) in (int, float)
            and 0 <= timeout_s <= _SINK_TIMEOUT_MAX_S)


def _sink_admit(category, code, tool):
    """Nonblocking bounded admission for block-decision telemetry.

    Captures the capped record, effective state home, and byte cap via
    the existing helpers BEFORE enqueue. Never waits (try-lock only),
    never starts or touches a worker, performs no filesystem I/O, and
    never raises into the enforcement path.
    Drops when the worker is offline, stopping, or dead. The busy-lock
    path sets a fixed flag (an unlocked exact increment is not safe);
    serialized counters saturate at _SINK_COUNTER_MAX. Early
    record/home failures and admission exceptions set a fixed sticky
    pre-admission drop flag so that degradation stays observable
    (D12) without any unlocked counter.
    """
    global _sink_admitted, _sink_dropped, _sink_busy_drop
    global _sink_pre_drop
    try:
        record = _sink_record(category, code, tool)
        if record is None:
            _sink_pre_drop = True
            return False
        state_home = _sink_home()
        if state_home is None:
            _sink_pre_drop = True
            return False
        cap = SINK_MAX_BYTES
        if not _sink_lock.acquire(False):
            _sink_busy_drop = True  # flag only: no unlocked exact count
            return False
        try:
            if (not _sink_started or _sink_stopping
                    or _sink_worker is None or not _sink_worker.is_alive()):
                _sink_dropped = min(_sink_dropped + 1, _SINK_COUNTER_MAX)
                return False
            if len(_sink_queue) >= _SINK_QUEUE_MAX:
                _sink_dropped = min(_sink_dropped + 1, _SINK_COUNTER_MAX)
                return False
            _sink_queue.append((state_home, record, cap))
            _sink_admitted = min(_sink_admitted + 1, _SINK_COUNTER_MAX)
            _sink_cond.notify()
            return True
        finally:
            _sink_lock.release()
    except Exception:
        _sink_pre_drop = True  # sticky flag: silent admission failure
        return False


def _sink_worker_loop():
    """Single daemon worker: pop under lock, persist outside lock."""
    global _sink_in_flight, _sink_written, _sink_failed
    while True:
        with _sink_cond:
            while not _sink_queue and not _sink_stopping:
                # Bounded idle wait: a shutdown whose lock acquisition
                # (or notify) was missed still takes effect here within
                # the check interval, without any idle-exit while the
                # sink is not stopping.
                _sink_cond.wait(0.1)
            if not _sink_queue:
                return  # exit requested and queue drained
            job = _sink_queue.popleft()
            _sink_in_flight += 1
            _sink_cond.notify_all()
        home, record, cap = job
        try:
            ok = _sink_persist(home, record, cap)
        except Exception:
            ok = False
        with _sink_cond:
            _sink_in_flight -= 1
            if ok:
                _sink_written = min(_sink_written + 1, _SINK_COUNTER_MAX)
            else:
                _sink_failed = min(_sink_failed + 1, _SINK_COUNTER_MAX)
            _sink_cond.notify_all()


def sink_start():
    """Start the single daemon sink worker; call outside any callback.

    Exactly one attempt per module generation: construction and start
    are both contained, a failure is latched (never retried or
    replaced), and publication plus start are serialized under the
    state lock — Thread.start only waits for bootstrap, which does not
    take the lock — so shutdown can never join an unstarted thread.
    Returns True only when a started worker is alive.
    """
    global _sink_started, _sink_worker, _sink_start_attempted
    global _sink_start_failed
    with _sink_lock:
        if _sink_stopping:
            return False
        if _sink_start_attempted:
            return (not _sink_start_failed and _sink_worker is not None
                    and _sink_worker.is_alive())
        _sink_start_attempted = True
        try:
            worker = threading.Thread(
                target=_sink_worker_loop,
                name="proxy-lifecycle-guard-sink",
                daemon=True,
            )
            _sink_worker = worker
            worker.start()
        except Exception:
            _sink_worker = None
            _sink_start_failed = True
            return False
        _sink_started = True
    return True


def sink_flush(timeout_s=10.0):
    """Bounded drain: wait for queued AND in-flight jobs to complete.

    Completion of processing, not storage durability. The deadline
    covers lock acquisition and every poll interval, so the call
    cannot block behind a contended state lock past the caller's
    budget; the drain polls under short bounded lock holds instead of
    a condition wait whose reacquisition has no deadline. Returns
    False on invalid input or deadline expiry. Never called by
    evaluate or the plugin callback.
    """
    if not _sink_timeout_ok(timeout_s):
        return False
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _sink_lock.acquire(timeout=remaining):
            return False
        try:
            if not _sink_queue and not _sink_in_flight:
                return True
        finally:
            _sink_lock.release()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.005, remaining))


def sink_shutdown(timeout_s=5.0):
    """Bounded stop: halt admission, request exit, join for <= timeout_s.

    The stopping flag is published BEFORE any lock acquisition so
    admission halts even while the state lock is contended. The
    deadline covers lock acquisition, the timed join, and any
    reacquisition; expiry returns False truthfully. Returns True only
    when the worker is no longer alive. A stuck
    worker is never signaled or replaced: it remains as one daemon in
    this module generation and cannot block interpreter exit; queued
    jobs are discarded with truthful counters when the deadline
    allows, and a wedged generation may retain its bounded queue
    after deadline expiry (bounded retention is contract-allowed).
    """
    global _sink_stopping, _sink_discarded
    if not _sink_timeout_ok(timeout_s):
        return False
    _sink_stopping = True  # admission drops before the lock is contended
    deadline = time.monotonic() + timeout_s
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _sink_lock.acquire(timeout=remaining):
        return False
    try:
        worker = _sink_worker
        _sink_cond.notify_all()
    finally:
        _sink_lock.release()
    if worker is not None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                worker.join(remaining)
            except Exception:
                pass
        if worker.is_alive():
            remaining = deadline - time.monotonic()
            if remaining > 0 and _sink_lock.acquire(timeout=remaining):
                try:
                    _sink_discarded = min(
                        _sink_discarded + len(_sink_queue),
                        _SINK_COUNTER_MAX)
                    _sink_queue.clear()
                finally:
                    _sink_lock.release()
            return False
    return True


def sink_status():
    """Fixed non-secret lifecycle counters; performs no I/O; never waits.

    Nonblocking: when the state lock is contended the same fixed shape
    is returned with busy=True, reading current globals without
    blocking (momentarily stale values are acceptable for this
    diagnostic). busy_dropped distinguishes the fixed busy-lock flag
    from the serialized exact dropped counter.
    """
    busy = not _sink_lock.acquire(False)
    if not busy:
        _sink_lock.release()
    return {
        "started": _sink_started,
        "stopping": _sink_stopping,
        "worker_alive": (_sink_worker is not None
                         and _sink_worker.is_alive()),
        "busy": busy,
        "queued": len(_sink_queue),
        "in_flight": _sink_in_flight,
        "queue_max": _SINK_QUEUE_MAX,
        "admitted": _sink_admitted,
        "written": _sink_written,
        "failed": _sink_failed,
        "dropped": _sink_dropped,
        "discarded": _sink_discarded,
        "busy_dropped": _sink_busy_drop,
        "pre_drop": _sink_pre_drop,
        "start_failed": _sink_start_failed,
    }

# ---------------------------------------------------------------------------
# T004: refusal-category vocabulary (spec rev 3, research D2, FR-002/SC-008).
# Fixed enum of sanitized decision categories, each with a stable LGV-NNN
# log code and a fixed sanitized message template. Zero argument/path/PID
# leakage; every template is byte-identical for all refusals in a category.
# INTEGRITY_REF_INVALID / LGV-032 is the reconciliation choice for FR-015's
# distinct integrity-reference failure category (spec pins existence, not
# the name) — flagged in the wave report.
# ---------------------------------------------------------------------------

CATEGORIES = {
    "VETO": "LGV-001",
    "BOUND_SIZE": "LGV-010",
    "BOUND_DEPTH": "LGV-011",
    "BOUND_COUNT": "LGV-012",
    "SHAPE_ENVELOPE": "LGV-020",
    "POLICY_INVALID": "LGV-030",
    "HOOK_MISMATCH": "LGV-031",
    "INTEGRITY_REF_INVALID": "LGV-032",
}

DECISION_MESSAGES = {
    "VETO": None,  # assigned below: the veto template IS BLOCK_MESSAGE
    "BOUND_SIZE": (
        "Blocked by the LiteLLM proxy lifecycle guard: the input is too "
        "large to prove safe; reduce the input size and resubmit."
    ),
    "BOUND_DEPTH": (
        "Blocked by the LiteLLM proxy lifecycle guard: the call nesting is "
        "too deep to prove safe; reduce the call nesting and resubmit."
    ),
    "BOUND_COUNT": (
        "Blocked by the LiteLLM proxy lifecycle guard: the batch is "
        "too large to prove safe; reduce the batch size and resubmit."
    ),
    "SHAPE_ENVELOPE": (
        "Blocked by the LiteLLM proxy lifecycle guard: the call could not "
        "be proven safe; restructure it."
    ),
    "POLICY_INVALID": (
        "Blocked by the LiteLLM proxy lifecycle guard: the guard's policy "
        "must be repaired by the owner — do not modify guard files."
    ),
    "HOOK_MISMATCH": (
        "Blocked by the LiteLLM proxy lifecycle guard: the call could not "
        "be attributed to a known tool; contact the owner."
    ),
    "INTEGRITY_REF_INVALID": (
        "Blocked by the LiteLLM proxy lifecycle guard: the guard's "
        "integrity reference is missing or unreadable and must be "
        "re-established by the owner — do not modify guard files."
    ),
}

LOG_CODES = dict(CATEGORIES)


def log_decision(category, tool_name=''):
    valid = (type(category) is str and len(category) <= 128
             and category in CATEGORIES)
    safe_category = category if valid else 'UNKNOWN'
    code = CATEGORIES[category] if valid else 'LGV-000'
    safe_tool = _sink_tool(tool_name)
    try:
        logger.info('lifecycle-guard decision: category=%s code=%s tool=%s',
                    safe_category, code, safe_tool)
    except Exception:
        pass
    if valid:
        try:
            _sink_admit(category, code, safe_tool)
        except Exception:
            pass

# Fixed, sanitized block message. Deliberately identical for every block
# reason: no arguments, paths, PIDs, or exception details ever leak.
BLOCK_MESSAGE = (
    "Blocked by the LiteLLM proxy lifecycle guard: this request could stop "
    "the protected proxy, or its safety could not be established. Use the "
    "owner-reviewed controlled restart workflow; do not substitute another "
    "kill command or execution path."
)

DECISION_MESSAGES["VETO"] = BLOCK_MESSAGE

EXECUTION_TOOLS = frozenset({"terminal", "execute_code", "browser_exec", "tool_call"})
PROCESS_MUTATING_ACTIONS = frozenset({"kill", "interrupt", "write", "submit", "close", "handoff"})

LAUNCHCTL_MUTATORS = frozenset({
    "bootout", "bootstrap", "kickstart", "kill", "unload", "load",
    "stop", "start", "remove", "enable", "disable",
})
BREW_SERVICE_BLOCK_VERBS = frozenset({"stop", "restart", "kill"})

# Broad selectors that could match the live proxy interpreter or its server.
BROAD_SELECTORS = (
    "python", "python2", "python3", "uv", "uvicorn", "gunicorn",
    "node", "sh", "bash", "zsh", "ruby", "perl", "java",
)

WRAPPER_VERBS = frozenset({"sudo", "env", "nohup", "nice", "time", "timeout", "stdbuf",
                           "command", "builtin", "exec"})

LSOF_CANDIDATES = ("/usr/sbin/lsof", "/sbin/lsof", "/usr/bin/lsof")
PS_CANDIDATES = ("/bin/ps", "/usr/bin/ps")

# Bounds: keep every evaluation fast and bounded under the hook budget.
# Legacy module constants remain for backwards compatibility of direct
# helper calls; the rev-3 bounds live in the policy (bounds object) and
# DEFAULT_BOUNDS below.
MAX_TEXT_CHARS = 65536
MAX_CHUNKS = 200
MAX_STAGES = 40
MAX_TOKENS = 64
MAX_TOOL_CALL_DEPTH = 8
DISCOVERY_TIMEOUT_S = 3.0
MAX_PIDS = 50

_SIGNAL_OPT_RE = re.compile(r"-[A-Za-z]\w*")
_SIGNED_INT_RE = re.compile(r"[+-]?\d+")
_OS_KILL_RE = re.compile(r"\bos\s*\.\s*kill(?:pg)?\s*\(")
_PSUTIL_KILL_RE = re.compile(r"\.\s*(?:kill|terminate)\s*\(")
_DOMAIN_ROOT_RE = re.compile(r"(?:gui|user)/\d+\s*$")
_DOMAIN_SUBST_RE = re.compile(r"(?:gui|user)/\$\(")
_SHELL_KEYWORDS = ("kill", "pkill", "killall", "lsof", "fuser", "launchctl",
                   "brew services", "xargs")

# FR-007 bound decision table support: over-bound inputs get a bounded
# head-scan; provably intent-free benign shapes pass (row a), unprovable
# shapes refuse with the bound's category (row c).
HEAD_SCAN_CHARS = 8192
# A batch larger than this cannot be analyzed within the hook budget at
# all: the uninspectable remainder makes it unprovable (FR-007 row c).
HARD_BATCH_CAP = 2048

BENIGN_VERBS = frozenset({
    "printf", "echo", "cat", "ls", "git", "pip", "grep", "awk", "sed",
    "head", "tail", "wc", "sort", "uniq", "tr", "cut", "find", "cp",
    "mv", "touch", "mkdir", "date", "true", "false", "test", "curl",
    "ps", "which", "whoami", "id", "pwd", "env", "export", "set",
})

# Shell control-flow keywords stripped from segment heads so wrapped
# kill-family verbs are still analyzed (F-001/F-006).
CONTROL_KEYWORDS = frozenset({
    "then", "do", "else", "{", "}", "!", "in", ";;", "fi", "done",
    "esac", "if", "while", "until", "for", "case",
})

# F-017: the guard's own deployed files are owner-immutable; mutation
# verbs targeting the plugin directory veto.
PLUGIN_DIR_MARKER = "plugins/proxy-lifecycle-guard"
MUTATING_VERBS = frozenset({
    "rm", "mv", "cp", "tee", "ln", "install", "chmod", "chown", "sed",
    "rsync", "dd", "shred", "truncate", "red", "write",
})


class _Block(Exception):
    """Internal: raise anywhere analysis determines the call must be blocked."""


class _BoundRefuse(_Block):
    """Internal: over-bound and unprovable — refuse with a bound category.

    Carries the FR-007 bound category (BOUND_SIZE / BOUND_DEPTH /
    BOUND_COUNT) so the entry point returns that category's sanitized
    message and LGV code instead of the generic veto.
    """

    def __init__(self, category):
        super().__init__(category)
        self.category = category


class PolicyError(Exception):
    """Policy config is missing, unreadable, tampered, or fails its strict schema."""


class IntegrityRefError(Exception):
    """The owner-established integrity reference is missing, unreadable,
    or unparseable (FR-015): its own fail-closed category, never a skip."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_integrity_ref(policy_path):
    """Load the owner-established integrity reference (FR-015/D3).

    The reference lives beside the policy as ``integrity.sha256`` and is
    owner-established / owner-updated only; the guarded agent can never
    author it. Missing, unreadable, or unparseable raises
    IntegrityRefError — never treated as a skip.
    """
    ref_path = Path(policy_path).with_name("integrity.sha256")
    try:
        raw = ref_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        raise IntegrityRefError("unreadable integrity reference") from exc
    if (not isinstance(data, dict)
            or not isinstance(data.get("policy_digest"), str)
            or not isinstance(data.get("source_digests"), dict)
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in data["source_digests"].items())):
        raise IntegrityRefError("integrity reference schema mismatch")
    return data


def verify_policy_integrity(policy_path, ref):
    """FR-015: the policy bytes must match the owner-recorded digest."""
    digest = sha256_bytes(Path(policy_path).read_bytes())
    return digest == ref["policy_digest"]


def verify_source_integrity(policy_path, ref):
    """FR-015/F-019: deployed source must match owner-recorded digests.

    Self-contained (D3): compares the deployed source files beside the
    policy against the digests the owner recorded at install — no runtime
    dependency on the kit checkout. Byte-for-byte; any mismatch is a
    tampered deployment.
    """
    directory = Path(policy_path).parent
    for name, expected in ref["source_digests"].items():
        source = directory / name
        try:
            if sha256_bytes(source.read_bytes()) != expected:
                return False
        except Exception:
            return False
    return True


@dataclass(frozen=True)
class Policy:
    port: int
    labels: frozenset
    protected_sessions: frozenset
    bounds: "dict | None" = None  # rev-3 bounds object (data-model.md)

    def __post_init__(self):
        if self.bounds is None:
            object.__setattr__(self, "bounds", dict(DEFAULT_BOUNDS))


# Rev-3 bounds with schema caps (contracts/policy-schema.json). The caps
# ARE the load-time hook-budget guard (FR-007/AR-008): a schema-valid
# policy cannot configure the guard past its runtime budget.
SCHEMA_BOUND_CAPS = {
    "input_text_bytes": (1024, 262144),
    "max_chunks": (1, 512),
    "max_depth": (1, 8),
    "discovery_timeout_ms": (100, 2000),
    "max_snapshot_pids": (1, 256),
}
DEFAULT_BOUNDS = {
    "input_text_bytes": 262144,
    "max_chunks": 512,
    "max_depth": 8,
    "discovery_timeout_ms": 2000,
    "max_snapshot_pids": 256,
}


def _check_owner_only_permissions(path, label):
    """FR-009/T006: owner-only file permissions on policy AND reference."""
    mode = path.stat().st_mode & 0o777
    if mode & 0o022:  # group-writable or world-writable
        raise PolicyError(f"{label} not owner-only")


def load_policy(policy_path):
    """Strictly parse and authenticate the policy JSON (rev-3, FR-008).

    Digest-first ordering (T006): the integrity reference is read and the
    policy digest verified BEFORE the schema is trusted. Re-read from disk
    on every evaluation — no caching (D5).
    """
    path = Path(policy_path)
    # 0. missing/unreadable policy is a POLICY_INVALID-class failure first
    #    (FR-005): when the policy itself is absent, the policy failure
    #    dominates — the integrity-ref category applies to a present
    #    policy whose reference is missing/broken (FR-015)
    try:
        path.stat()
    except OSError as exc:
        raise PolicyError("unreadable policy") from exc
    # 1. integrity reference (own category, FR-015)
    ref = read_integrity_ref(path)
    # 2. owner-only permissions on policy AND reference (FR-009)
    _check_owner_only_permissions(path, "policy")
    _check_owner_only_permissions(path.with_name("integrity.sha256"),
                                  "integrity reference")
    # 3. policy digest must match the owner-recorded digest (FR-015)
    if not verify_policy_integrity(path, ref):
        raise PolicyError("tampered policy")
    # 3b. deployed source digests (F-019/T005): the reference MUST
    # carry digests for both deployed source files; an empty or partial
    # source_digests map would skip verification entirely (codex review
    # P2) — a divergent deployed copy could pass as untampered. Fail
    # closed unless the reference is complete.
    REQUIRED_SOURCE_DIGESTS = ("guard.py", "__init__.py")
    digests = ref.get("source_digests")
    if (not isinstance(digests, dict)
            or not set(REQUIRED_SOURCE_DIGESTS) <= set(digests)):
        raise PolicyError("incomplete integrity reference")
    if not verify_source_integrity(path, ref):
        raise PolicyError("tampered deployed source")
    # 4. strict schema (FR-008)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:  # missing, unreadable, or invalid JSON
        raise PolicyError("unreadable policy") from exc
    if not isinstance(data, dict):
        raise PolicyError("policy must be an object")
    expected = {"schema_version", "protected_port", "protected_labels",
                "protected_session_handles", "bounds",
                "refusal_vocabulary_version"}
    if set(data.keys()) != expected:
        raise PolicyError("policy schema mismatch")
    version = data["schema_version"]
    if isinstance(version, bool) or version != 1:
        raise PolicyError("unsupported policy version")
    vocab = data["refusal_vocabulary_version"]
    if isinstance(vocab, bool) or not isinstance(vocab, int) or vocab < 1:
        raise PolicyError("unsupported refusal vocabulary version")
    port = data["protected_port"]
    if (isinstance(port, bool) or not isinstance(port, int)
            or not (1 <= port <= 65535)):
        raise PolicyError("invalid port")
    labels = data["protected_labels"]
    sessions = data["protected_session_handles"]
    for seq in (labels, sessions):
        if not isinstance(seq, list) or not all(
            isinstance(item, str) and item for item in seq
        ):
            raise PolicyError("invalid labels/sessions")
    bounds = data["bounds"]
    if not isinstance(bounds, dict) or set(bounds) != set(SCHEMA_BOUND_CAPS):
        raise PolicyError("invalid bounds")
    for key, (lo, hi) in SCHEMA_BOUND_CAPS.items():
        val = bounds[key]
        if (isinstance(val, bool) or not isinstance(val, int)
                or not (lo <= val <= hi)):
            raise PolicyError("bound out of budget range")
    return Policy(
        port=port,
        labels=frozenset(label.lower() for label in labels),
        protected_sessions=frozenset(sessions),
        bounds=dict(bounds),
    )


@dataclass(frozen=True)
class Snapshot:
    """Read-only process discovery result. Empty sets are a determinate
    'nothing is listening' answer; None (from discover_snapshot) means the
    discovery itself failed and callers must refuse conservatively."""
    pids: frozenset = frozenset()
    pgids: frozenset = frozenset()
    comms: frozenset = frozenset()
    args: tuple = ()


def _block(category="VETO", tool_name=""):
    """One Evaluation Decision (data-model.md): block with category + code.

    Sanitized fixed template per category (FR-002); the plugin emits its
    own log line so distinguishability does not depend on runtime
    surfacing (hook-contract rule 6).
    """
    message = DECISION_MESSAGES.get(category, BLOCK_MESSAGE)
    log_decision(category, tool_name)
    return {"action": "block", "message": message,
            "category": category, "log_code": LOG_CODES[category]}





def _find_binary(candidates):
    for cand in candidates:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def discover_snapshot(port, timeout=DISCOVERY_TIMEOUT_S):
    """Read-only discovery of the process listening on ``port``.

    Returns a Snapshot (possibly empty — a determinate answer), or None when
    discovery itself failed (caller must refuse conservatively). Never
    prints discovery output.
    """
    lsof = _find_binary(LSOF_CANDIDATES)
    ps = _find_binary(PS_CANDIDATES)
    if lsof is None or ps is None:
        return None
    try:
        probe = subprocess.run(
            [lsof, "-t", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None
    if probe.returncode not in (0, 1):  # 1 == nothing listening (determinate)
        return None
    raw_pids = probe.stdout.split()
    if (len(raw_pids) > MAX_PIDS or any(not p.isdigit() for p in raw_pids)
            or (probe.returncode == 0 and not raw_pids)
            or (probe.returncode == 1 and (raw_pids or probe.stderr.strip()))):
        return None
    pids = sorted(set(map(int, raw_pids)))
    snap_pids, snap_pgids, snap_comms, snap_args = set(), set(), set(), []
    if pids:
        try:
            detail = subprocess.run(
                [ps, "-p", ",".join(str(p) for p in pids),
                 "-o", "pid=,pgid=,comm=,args="],
                capture_output=True, text=True, timeout=timeout,
            )
        except Exception:
            return None
        if detail.returncode != 0:
            return None
        for line in detail.stdout.splitlines()[:MAX_PIDS]:
            parts = line.split(None, 3)
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                snap_pids.add(int(parts[0]))
                snap_pgids.add(int(parts[1]))
                if len(parts) >= 3:
                    snap_comms.add(parts[2].lower())
                if len(parts) == 4:
                    snap_args.append(parts[3])
    if set(pids) != snap_pids:
        return None  # incomplete/malformed ps output is not an empty listener
    return Snapshot(pids=frozenset(snap_pids), pgids=frozenset(snap_pgids),
                    comms=frozenset(snap_comms), args=tuple(snap_args))


# ---------------------------------------------------------------------------
# Bounded shell-text scanning (pure helpers; injectable via evaluate kwargs)
# ---------------------------------------------------------------------------

_CHUNK_SEPS = ("&&", "||", ";", "\n", "\r", "&")


def _split_segments(text, separators, limit=MAX_CHUNKS):
    """Quote-aware split on shell separators. Bounded by ``limit``."""
    parts, buf = [], []
    quote = None
    i, n = 0, len(text)
    seps = sorted(separators, key=len, reverse=True)
    while i < n:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\":
            buf.append(ch)
            if i + 1 < n:
                buf.append(text[i + 1])
            i += 2
            continue
        matched = None
        for sep in seps:
            if text.startswith(sep, i):
                if sep == "&":
                    nxt = text[i + 1] if i + 1 < n else " "
                    prv = text[i - 1] if i > 0 else " "
                    # bare background '&', not '2>&1' and not '&&'
                    if (nxt.isspace() or nxt in (";", "\n", "\r")) and prv not in "<>0123456789":
                        matched = sep
                else:
                    matched = sep
                break
        if matched:
            parts.append("".join(buf))
            buf = []
            i += len(matched)
            if len(parts) >= limit:
                parts.append(text[i:])
                return parts
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _tokenize(segment, limit=MAX_TOKENS):
    """Decode ordinary shell quoting without executing expansions."""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        raise _Block() from None
    return tokens[:limit], len(tokens) >= limit


def _strip_wrappers(tokens):
    """Peel sudo/env/nohup/timeout-style wrappers (bounded)."""
    for _ in range(8):
        if tokens and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[0]):
            tokens = tokens[1:]
            continue
        if not tokens:
            return tokens
        head = tokens[0]
        base = head.rsplit("/", 1)[-1]
        if base not in WRAPPER_VERBS:
            return tokens
        tokens = tokens[1:]
        if base == "env":
            for _ in range(MAX_TOKENS):
                if not tokens:
                    break
                token = tokens[0]
                if token in ('-u', '--unset', '-C', '--chdir'):
                    if len(tokens) < 2:
                        raise _Block()
                    tokens = tokens[2:]
                elif token in ('-i', '--ignore-environment', '--') or re.fullmatch(
                        r"[A-Za-z_]\w*=.*", token) or token.startswith(('--unset=', '--chdir=')):
                    tokens = tokens[1:]
                elif token.startswith('-'):
                    raise _Block()  # unsupported wrapper option, not a safe command
                else:
                    break
        elif base == "timeout":
            if tokens and re.fullmatch(r"[\d.]+[smh]?", tokens[0]):
                tokens = tokens[1:]
            elif tokens and tokens[0].startswith('-'):
                raise _Block()
        elif base in ("sudo", "nice", "stdbuf", "command", "builtin", "exec"):
            operands = {'sudo': {'-u', '-g', '-h', '-p', '-C', '--user', '--group'},
                        'nice': {'-n'}, 'exec': {'-a'}}.get(base, set())
            for _ in range(MAX_TOKENS):
                if not tokens or not tokens[0].startswith('-'):
                    break
                if tokens[0] in operands:
                    if len(tokens) < 2:
                        raise _Block()
                    tokens = tokens[2:]
                else:
                    tokens = tokens[1:]
    return tokens


def _mentions_target(low_text, policy):
    if "litellm" in low_text:
        return True
    if any(label in low_text for label in policy.labels):
        return True
    if re.search(r"(?<!\d)%d(?!\d)" % policy.port, low_text):
        return True
    return False


def _analyze_pattern_kill(verb, tokens, policy, get_snapshot):
    """pkill/killall: block litellm/label/broad-selector/uid-sweeper patterns."""
    if any(tok in ("-u", "-U", "--uid", "--user") for tok in tokens):
        raise _Block()  # user-wide sweep could kill the proxy user's processes
    pattern_toks = [t for t in tokens if not t.startswith("-")] or tokens
    pattern = " ".join(pattern_toks).strip("'\"").lower()
    if not pattern:
        return
    # Identity match against the configured proxy identity. Token match,
    # NOT substring: "audit-not-litellm" is its own word and does not
    # hit the label "example.litellm" (F-012/F-013 over-blocking fix).
    # A pattern containing regex metacharacters whose LITERAL projection
    # (dots→any-char) still matches the proxy identity is a match: a dot
    # in "litellm.*proxy" is literal-adjacent to the real identity.
    snap = get_snapshot()
    if snap is None:
        raise _Block()  # conservative refusal on discovery failure
    identity_pieces = set()
    for label in policy.labels:
        identity_pieces.add(label)
        identity_pieces.update(label.split("."))
    identity_pieces.add("litellm")
    pattern_words = set(re.split(r"[^a-z0-9._-]+", pattern))
    for piece in identity_pieces:
        if piece in pattern_words:
            raise _Block()
    # dot-as-any-char projection, bounded: pattern-word dots act as
    # single-char wildcards, so "l.tellm" hits "litellm" (F-013: only
    # pattern-side dots project; identity pieces match literally)
    for word in pattern_words:
        if "." not in word:
            continue
        rx = re.compile("".join(
            re.escape(c) if c != "." else "." for c in word))
        for piece in identity_pieces:
            if piece and rx.fullmatch(piece):
                raise _Block()
    # literal substring against the LIVE snapshot args/comms: the
    # pattern matching the running proxy's argv/comm is a direct hit
    for argline in (*snap.args, *snap.comms):
        if pattern in argline.lower():
            raise _Block()
    # The python family needs version-suffix handling (python, python3, 3.15):
    # a broad interpreter selector could kill the live proxy interpreter.
    if re.search(r"(?<![\w./-])python[23]?(?:\.\d+)?(?![\w.-])", pattern):
        raise _Block()
    for selector in BROAD_SELECTORS:
        if selector == "python":
            continue
        if re.search(r"(?<![\w./-])%s(?![\w.-])" % re.escape(selector), pattern):
            raise _Block()
    # Refuse regex selectors rather than evaluate attacker-controlled
    # regexes against process data (overbroad matches, regex CPU burn).
    # A lone dot is NOT refused here (F-013: dotted module paths are
    # common literal patterns); the identity check above already ran.
    if any(c in pattern for c in "*+?[](){}|^$\\"):
        raise _Block()


def _analyze_kill_stage(stage_text, tokens, policy, get_snapshot):
    sig = None
    targets = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            targets.extend(tokens[i + 1:])
            break
        if tok in ("-s", "-n") and i + 1 < len(tokens):
            sig = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("-") and len(tok) > 1 and tok[1:].isdigit():
            if sig is None:
                sig = tok[1:]
            else:
                targets.append(tok)  # negative PID == process group
            i += 1
            continue
        if sig is None and _SIGNAL_OPT_RE.fullmatch(tok):
            sig = tok[1:]
            i += 1
            continue
        targets.append(tok)
        i += 1
    if sig is not None and str(sig).lstrip("-").upper() in ("0", "SIGZERO", "ZERO"):
        return True  # read-only liveness probe
    if not targets:
        return  # bare kill in a pipeline; handled by the pipeline cross-check
    for target in targets:
        if _SIGNED_INT_RE.fullmatch(target):
            pid = int(target)
            if pid in (0, -1):
                raise _Block()  # current group / every permitted process
            snap = get_snapshot()
            if snap is None:
                raise _Block()  # conservative refusal on discovery failure
            if pid < 0:
                if -pid in snap.pgids:
                    raise _Block()
            elif pid in snap.pids:
                raise _Block()
            continue
        if target.startswith("$(") or target.startswith("`"):
            # Another port or a number in lsof/ps text does not prove that the
            # resulting PID is unrelated. Never execute a target substitution.
            raise _Block()
        if target.startswith("$") or target.startswith("${"):
            raise _Block()  # shell variable target: unresolvable, refuse
        # job specs (%1) and words: not PID-shaped, ignore


def _analyze_launchctl(tokens, policy):
    if not tokens:
        return
    verb = tokens[0].lower()
    if verb not in LAUNCHCTL_MUTATORS:
        return  # print/list/blame/get and friends are read-only
    rest = " ".join(tokens[1:]).lower().strip()
    if "litellm" in rest or any(label in rest for label in policy.labels):
        raise _Block()
    # FR-006: variable or substitution operands are unresolved — refuse
    # rather than guess (launchctl bootout gui/501/$LABEL etc.)
    for tok in tokens[1:]:
        if "$" in tok or "`" in tok:
            raise _Block()
    if _DOMAIN_ROOT_RE.fullmatch(rest):
        raise _Block()  # whole GUI/user domain bootout/bootstrap
    if _DOMAIN_SUBST_RE.search(rest):
        raise _Block()  # gui/$(id -u) style whole-domain targeting


def _analyze_brew(tokens, policy):
    if "services" not in tokens:
        return
    idx = tokens.index("services")
    sub_verb = tokens[idx + 1].lower() if idx + 1 < len(tokens) else ""
    if sub_verb not in BREW_SERVICE_BLOCK_VERBS:
        return
    rest = " ".join(tokens[idx + 2:]).lower()
    if "--all" in tokens or "litellm" in rest:
        raise _Block()
    # FR-006: unresolved service-name operands refuse (brew services
    # stop $SVC — the operand cannot be proven unrelated)
    for tok in tokens[idx + 2:]:
        if "$" in tok or "`" in tok:
            raise _Block()


# Control-flow keywords that become chunk separators so wrapped bodies
# are analyzed (F-001/F-006). Quote-aware splitting downstream keeps
# quoted text safe from false segmentation.
_CF_SUB_RE = re.compile(r"\b(?:then|do|else|done|fi|esac)\b|;;")


def _normalize_control_flow(text):
    """Replace shell control words, preserving quoted/escaped data verbatim.

    Quoted shell/Python payloads are inspected recursively after tokenization;
    changing their bytes here corrupts both benign code and threat analysis.
    A keyword is normalized only as a whole unquoted word at a real shell
    word boundary: quotes, escapes (incl. escaped whitespace), and command
    substitutions extend the current word, so ``print\\(\\"else\\"\\)`` or
    ``then\\ x`` stay byte-identical while real ``if/then``, ``while/do``,
    ``case/esac`` bodies still split. ``;;`` is a case separator, not a
    word, so it stays normalized even when adjacent to a word.
    """
    parts = []
    quote = None
    in_word = False
    i = 0
    substitutions = iter(_command_substitutions(text, with_spans=True))
    substitution = next(substitutions, None)
    while i < len(text):
        if substitution is not None and i == substitution[0]:
            # The existing substitution scanner gives nested shell programs
            # their own quote context. Their bodies are inspected recursively
            # before normalization, not rewritten as outer-shell data. The
            # span joins the current word rather than starting a new token.
            end = substitution[1]
            parts.append(text[i:end])
            i = end
            in_word = True
            substitution = next(substitutions, None)
            continue
        char = text[i]
        if quote is not None:
            if char == "\\" and quote == '"' and i + 1 < len(text):
                parts.append(text[i:i + 2])
                i += 2
                continue
            parts.append(char)
            if char == quote:
                quote = None
            i += 1
            continue
        if char == "\\" and i + 1 < len(text) and text[i + 1] == "\n":
            # Backslash-newline is elided by the shell: it neither starts
            # nor ends a word, so the current word state carries over.
            parts.append(text[i:i + 2])
            i += 2
            continue
        if char == "\\":
            # An escaped character (incl. escaped whitespace) joins the
            # current word, so a keyword adjacent to it is inert data.
            in_word = True
            parts.append(text[i:i + 2])
            i += 2
            continue
        if char in ("'", '"'):
            quote = char
            in_word = True  # a quoted segment belongs to the current word
            parts.append(char)
            i += 1
            continue
        match = _CF_SUB_RE.match(text, i)
        normalize = False
        if match is not None:
            if match.group() == ";;":
                normalize = True  # case separator, valid adjacent to a word
            else:
                # Whole-word check: skip backslash-newline continuations
                # (the shell elides them, so ``then\\<newline>;`` still
                # ends the keyword), then require whitespace, an operator,
                # or end-of-text so ``then;`` normalizes while ``then\\ x``,
                # ``then$(x)``, and ``then"x"`` stay verbatim (each is one
                # shell word).
                j = match.end()
                while (j + 1 < len(text) and text[j] == "\\"
                        and text[j + 1] == "\n"):
                    j += 2
                normalize = (not in_word
                             and (j >= len(text)
                                  or text[j] in " \t\r\n;|&()<>"))
            if normalize:
                parts.append(";")
                i = match.end()
                in_word = False
                continue
        if char in " \t\r\n;|&()<>":
            in_word = False  # whitespace/operators end the current word
        elif not in_word:
            in_word = True
        parts.append(char)
        i += 1
    return "".join(parts)


_CASE_WORD_RE = re.compile(r"\w+\)")


def _strip_control_flow(tokens):
    """Strip shell control-flow residue so wrapped verbs are analyzed.

    Single mutation point for the Constitution III control-flow scan
    branch (T013): disabling this function (and the _CF_SUB_RE
    substitution) must demonstrably flip the control-flow-wrapped veto
    outcomes to allow — proving the battery measures the real guard.
    """
    while tokens:
        t0 = tokens[0].lower()
        if t0 == "case":
            try:
                tokens = tokens[tokens.index("in") + 1:]
            except ValueError:
                tokens = []
            continue
        if (t0 in CONTROL_KEYWORDS or t0 == "in"
                or _CASE_WORD_RE.fullmatch(tokens[0])):
            tokens = tokens[1:]
            continue
        break
    # "time -p kill ..." / "time kill ...": time is a wrapper verb
    if tokens and tokens[0].lower() == "time":
        tokens = tokens[1:]
        while tokens and tokens[0].startswith("-"):
            tokens = tokens[1:]
    return tokens


def _applescript_dss_payloads(text):
    """Extract `do shell script` operands (AppleScript quote dialect).

    Returns (payloads, unextractable). AppleScript string literals are
    double-quoted with backslash escapes; nested shell quotes inside the
    literal are preserved verbatim for the shell analyzer. An occurrence
    whose operand is not a plain literal (variable, concatenation, or a
    non-``with`` trailing clause) is unprovable — unextractable is set
    and the caller refuses (FR-006: unresolved operands are never
    guessed at).
    """
    lowered = text.lower()
    payloads = []
    unextractable = False
    count = 0
    for m in re.finditer(r"do\s+shell\s+script", lowered):
        i = m.end()
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text) or text[i] != '"':
            unextractable = True
            continue
        i += 1
        parts = []
        while i < len(text):
            ch = text[i]
            if ch == "\\" and i + 1 < len(text):
                parts.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                break
            parts.append(ch)
            i += 1
        if i >= len(text):
            unextractable = True
            continue
        i += 1  # closing quote
        # trailing clause: blank or a `with ...` clause is inert; any
        # other trailing text (concatenation, semicolons) is unprovable
        nxt = lowered.find("do shell script", i)
        stop = nxt if nxt != -1 else len(text)
        trailer = text[i:stop].strip()
        if trailer and not trailer.startswith("with "):
            unextractable = True
            continue
        count += 1
        if count > 16:
            raise _Block()
        payloads.append("".join(parts))
    return payloads, unextractable


_JXA_SHELL_RE = re.compile(
    r"\$\.\s*(?:system|shell)\s*\(|do\s*shell\s*script\s*\(", re.I)


def _jxa_shell_payloads(text):
    """Extract JXA shell-execution arguments ($.system/.shell/doShellScript).

    Returns (payloads, unextractable). JS string arguments may be single-
    or double-quoted with backslash escapes; non-literal arguments
    (variables, template literals, concatenations) are unprovable.
    """
    payloads = []
    unextractable = False
    count = 0
    for m in _JXA_SHELL_RE.finditer(text):
        i = m.end()
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text) or text[i] not in "\"'":
            unextractable = True
            continue
        quote = text[i]
        i += 1
        parts = []
        while i < len(text):
            ch = text[i]
            if ch == "\\" and i + 1 < len(text):
                parts.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                break
            parts.append(ch)
            i += 1
        if i >= len(text):
            unextractable = True
            continue
        i += 1
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i < len(text) and text[i] not in ");,&":
            unextractable = True
            continue
        count += 1
        if count > 16:
            raise _Block()
        payloads.append("".join(parts))
    return payloads, unextractable


_STDIN_CONSUMER_VERBS = frozenset(
    {"sh", "bash", "zsh", "python", "python2", "python3"})


def _stdin_consumer(stage, verb, stages):
    """True when this stage consumes a producer's data as programs.

    Only for a NON-first pipeline stage whose argv carries no -c/-e
    script operand (then it is argv-driven, analyzed elsewhere): the
    interpreter reads stdin from the upstream stage.
    """
    if len(stages) < 2:
        return False
    if stage != stages[-1] and stage not in stages[:-1]:
        return False
    try:
        if stage.split()[0].rstrip("0123456789.") not in (
                "sh", "bash", "zsh", "python", "python2", "python3"):
            return False
    except IndexError:
        return False
    tokens = stage.split()
    for tok in tokens[1:]:
        if tok in ("-c", "-e") or tok.startswith(("--command=",
                                                  "--eval=")):
            return False
    return True


def _pipe_payload_literals(stage_text):
    """Bounded extraction of quoted data payloads in a pipeline stage.

    Yields the quoted segments (with quotes intact) whose CONTENT
    starts with a common data-producer shape. Bounded: single pass,
    small-capped segments, no regex burn.
    """
    out = []
    i, n = 0, len(stage_text)
    while i < n and len(out) < 8:
        if stage_text[i] not in ("'", '"'):
            i += 1
            continue
        quote = stage_text[i]
        j = i + 1
        while j < n and stage_text[j] != quote:
            j += 1
        if j > n:
            break
        segment = stage_text[i + 1:j]
        if 0 < len(segment) <= 512:
            low = segment.lower()
            if low.startswith(("kill", "pkill", "killall", "shutdown",
                               "reboot", "launchctl", "brew services",
                               "os.kill", "fuser -k")) or "kill" in low:
                out.append(quote + segment)
        i = j + 1
    return out


def _strip_shell_comments(text):
    """Remove shell comments (prose) before tokenization (lpkit-knp).

    '#' starts a comment only at a real word boundary (start of text,
    after whitespace, or after a separator). '#' inside quotes and
    mid-word forms (${'s}) are literal. Apostrophes in comments were
    previously read as opening quotes, leaving the chunk unparseable
    and failing closed — stripping comments fixes the class without
    loosening refusal for genuinely unparseable commands.
    """
    out = []
    i, n = 0, len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == "#" and (i == 0 or text[i - 1] in " \t\n\r;|&("):
            # comment: drop to end of line
            j = text.find("\n", i)
            if j == -1:
                break
            out.append("\n")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _collect_literal_vars(text):
    """Map NAME -> simple literal value for visible assignments.

    Only simple single/double-quoted or bare-word literal values are
    collected (NAME="literal", NAME='literal', NAME=word). Any
    substitution, concatenation, or interpolation in the value makes
    the variable UNRESOLVED (excluded) — resolution must be analysis,
    not a hole (lpkit-5ix). Bounded to 32 entries.
    """
    env = {}
    for m in re.finditer(
            r"(?:^|[\n;|&])([A-Za-z_]\w*)=(\"[^\"]*\"|'[^']*'"
            r"|[A-Za-z0-9_./@-]+)", text):
        if len(env) >= 32:
            break
        name, raw = m.group(1), m.group(2)
        if raw[:1] in ('"', "'"):
            value = raw[1:-1]
            if "$" in value or "`" in value or "\\" in value:
                continue  # interpolation: unresolvable
        else:
            if not re.fullmatch(r"[A-Za-z0-9_./@-]+", raw):
                continue
            value = raw
        env[name] = value
    return env


def _redirect_writes_plugin_dir(chunk):
    """True when any redirection in chunk targets the plugin directory.

    Quote-aware, bounded single pass. A redirect whose destination is
    the plugin dir (or anything beneath it) is write intent; stderr
    suppression (2>/dev/null, 2>&1) and redirects to unrelated paths
    are not.
    """
    quote = None
    i, n = 0, len(chunk)
    while i < n:
        ch = chunk[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if ch == ">":
            # classify this redirect; ALWAYS advance past the '>'
            # operators so the scan cannot stall on one spot
            j = i
            fd = ""
            while j > 0 and chunk[j - 1].isdigit():
                j -= 1
                fd = chunk[j] + fd
            k = i
            while k < n and chunk[k] == ">":
                k += 1
            if k < n and chunk[k] == "&":
                i = k + 1
                continue  # >& dup: no file destination
            while k < n and chunk[k].isspace():
                k += 1
            dest = []
            in_dq = False
            while k < n:
                c2 = chunk[k]
                if in_dq:
                    if c2 == '"':
                        in_dq = False
                    else:
                        dest.append(c2)
                    k += 1
                    continue
                if c2 == '"':
                    in_dq = True
                    k += 1
                    continue
                if c2.isspace() or c2 in ";|&<()":
                    break
                if c2 == "\\" and k + 1 < n:
                    dest.append(chunk[k + 1])
                    k += 2
                    continue
                dest.append(c2)
                k += 1
            raw_dest = "".join(dest)
            i = max(k, i + 1)
            if not raw_dest:
                continue
            expanded = raw_dest
            if expanded.startswith("~"):
                home = os.environ.get("HOME") or ""
                if home and len(home) < 4096:
                    expanded = home + expanded[1:]
            if "proxy-lifecycle-guard" in expanded.lower():
                return True
        else:
            i += 1
    return False


def _analyze_chunk(chunk, policy, get_snapshot, depth=0, env=None):
    """Analyze one command chunk (may be a pipeline). Hazard anywhere blocks."""
    if depth > MAX_TOOL_CALL_DEPTH:
        raise _Block()
    chunk = chunk.strip()
    if chunk.startswith("(") and chunk.endswith(")"):
        _analyze_shell_text(chunk[1:-1], policy, get_snapshot, depth + 1)
        return
    # lpkit-5ix: command-position variable indirection resolves
    # against simple literal assignments visible in the same payload
    # (the adapter pattern). Resolution is ANALYSIS, not a hole: the
    # substituted text is re-analyzed with the full pipeline, so a
    # resolved kill command still vetoes. Unresolvable indirection
    # stays refused (FR-006).
    if env:
        stage_list = _split_segments(chunk, ("|",), limit=MAX_STAGES)
        if stage_list:
            head_words = stage_list[0].strip().split()
            if head_words:
                raw = head_words[0]
                # bare and quoted forms: $X, "$X", '${X}'
                if raw.startswith('"') and raw.endswith('"'):
                    var = raw[1:-1]
                elif raw.startswith("'") and raw.endswith("'"):
                    var = raw[1:-1]
                elif raw.startswith("$") and len(raw) > 1:
                    var = raw
                else:
                    var = None
                if var:
                    var = var.lstrip("$").lstrip("{").rstrip("}")
                    if var in env:
                        resolved = " ".join(
                            [env[var]] + head_words[1:])
                        _analyze_shell_text(
                            resolved, policy, get_snapshot, depth + 1)
                        for stage in stage_list[1:]:
                            if stage.strip():
                                _analyze_chunk(
                                    stage.strip(), policy, get_snapshot,
                                    depth + 1, env)
                        return
    # F-017: mutation of the guard's own deployed plugin directory (or
    # self-disable/uninstall through the runtime CLI) vetoes
    low_chunk = chunk.lower()
    if PLUGIN_DIR_MARKER in low_chunk:
        words = low_chunk.split()
        first_word = words[0] if words else ""
        if (first_word in MUTATING_VERBS
                or any("plugin" in w for w in words[:1])):
            raise _Block()
        # A redirect vetoes only when its file destination resolves
        # inside the plugin dir. Inert noise control on reads
        # (2>/dev/null, 2>&1) is not a write (lpkit-z4b finding A).
        if ">" in chunk and _redirect_writes_plugin_dir(chunk):
            raise _Block()
    # control-flow bodies: turn keyword boundaries into chunk separators
    chunk = _normalize_control_flow(chunk)
    stages = [s for s in _split_segments(chunk, ("|",), limit=MAX_STAGES) if s.strip()]
    if len(stages) >= MAX_STAGES:
        raise _Block()
    kill_like = False
    stage_texts = []
    for stage in stages:
        stage_texts.append(stage)
        # each pipeline stage may itself contain control-flow segments
        for sub in _split_segments(stage, _CHUNK_SEPS):
            if not sub.strip():
                continue
            tokens, truncated = _tokenize(sub)
            if truncated:
                # FR-007: over the token bound — the veto is never lost
                # (intent scan), and intent-free truncation allows the
                # bounded head analysis
                if _full_text_kill_intent(sub, policy):
                    raise _Block()
            # strip control-flow residue so wrapped verbs get analyzed
            tokens = _strip_control_flow(tokens)
            # variable command indirection is unresolvable — refuse (FR-006)
            if tokens and (tokens[0].startswith("$") or tokens[0].startswith("`")):
                raise _Block()
            # assignment whose value carries kill-family intent
            if tokens and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[0]):
                if _full_text_kill_intent(tokens[0].split("=", 1)[1], policy):
                    raise _Block()
            tokens = _strip_wrappers(tokens)
            if not tokens:
                continue
            verb = tokens[0].rsplit("/", 1)[-1].lower()
            rest = tokens[1:]
            if verb == "eval":
                raise _Block()  # FR-003: eval payloads are never evaluated
            # lpkit-z4b finding B: stdin-fed interpreter stages inherit
            # the producer stage's data payload analysis (F-007 xargs
            # precedent). A pipeline feeding sh/bash/zsh/python whose
            # producer data carries kill-family intent is a kill
            # pipeline; benign pipe-to-sh data stays allowed.
            if verb in ("sh", "bash", "zsh", "python", "python2",
                        "python3") and _stdin_consumer(
                    stage, verb, stages):
                for stage_text in stage_texts:
                    for lit in _pipe_payload_literals(stage_text):
                        low_l = lit.lower()
                        if low_l.startswith(("echo ", "printf ",
                                             "tee ", "cat ", "yes ",
                                             "seq ")):
                            body = lit.split(None, 1)
                            payload = body[1] if len(body) > 1 else ""
                            payload = payload.strip("'\"")
                        else:
                            payload = lit
                        if payload and _mentions_target(
                                payload, policy):
                            raise _Block()
                        if payload and _full_text_kill_intent(
                                payload, policy):
                            raise _Block()
                    # unquoted producer argv: echo/printf data fed
                    # to a stdin consumer carries the same risk
                    # (compute inside try, RAISE outside: _Block
                    # subclasses Exception and must not be swallowed)
                    try:
                        ptoks = stage_text.split()
                        pdata = None
                        if (len(ptoks) > 1
                                and ptoks[0] in ("echo", "printf",
                                                 "yes", "tee")):
                            pdata = " ".join(
                                t for t in ptoks[1:]
                                if not t.startswith("-"))
                    except Exception:
                        pdata = None
                    if pdata and (
                            _mentions_target(pdata, policy)
                            or _full_text_kill_intent(pdata, policy)):
                        raise _Block()
            if (verb == "hermes"
                    and any("plugin" in t.lower() for t in rest)):
                sub_l = [t.lower() for t in rest]
                if ("disable" in sub_l or "uninstall" in sub_l) and \
                        "proxy-lifecycle-guard" in " ".join(sub_l):
                    raise _Block()  # F-017 self-disable through the CLI
            if verb in ("sh", "bash", "zsh") and any(
                    t.startswith("-") and "c" in t for t in rest):
                idx = next(i for i, t in enumerate(rest)
                           if t.startswith("-") and "c" in t)
                if idx + 1 >= len(rest):
                    raise _Block()
                _analyze_shell_text(rest[idx + 1], policy, get_snapshot, depth + 1)
            elif re.fullmatch(r"python(?:[23](?:\.\d+)?)?", verb) and "-c" in rest:
                idx = rest.index("-c")
                if idx + 1 >= len(rest):
                    raise _Block()
                _analyze_code_text(rest[idx + 1], policy, get_snapshot)
            elif verb == "kill":
                probe_only = _analyze_kill_stage(sub, rest, policy, get_snapshot)
                kill_like = kill_like or not probe_only
            elif verb in ("pkill", "killall"):
                _analyze_pattern_kill(verb, rest, policy, get_snapshot)
                kill_like = True
            elif verb == "xargs":
                if any(t.rsplit('/', 1)[-1] in ("kill", "pkill", "killall")
                       for t in rest):
                    raise _Block()  # stdin targets unresolved; no PID proof
                # F-007: xargs spawning a shell with a payload is a kill
                # pipeline shape (listener discovery feeding a signal)
                if any(t.rsplit('/', 1)[-1].lower() in
                       ("sh", "bash", "zsh") for t in rest) and \
                        any(t == "-c" or t.startswith("-c") or t.startswith("-I")
                            for t in rest):
                    raise _Block()
                if any(re.fullmatch(r"python[23]?", t.rsplit('/', 1)[-1].lower())
                       for t in rest):
                    raise _Block()
            elif verb == "awk":
                # lpkit-z4b B: awk's system()/command pipeline builtins
                # execute text. A kill-family call in the program text
                # is unprovable (FR-006) and vetoes; benign awk
                # programs (patterns, print) pass untouched.
                prog = " ".join(t for t in rest
                                if not t.startswith("-")
                                and not t.startswith("$"))
                if ("system(" in prog or "cmd |" in prog
                        or "| getline" in prog):
                    low_prog = prog.lower()
                    if _full_text_kill_intent(low_prog, policy):
                        raise _Block()
            elif verb == "find":
                # lpkit-z4b B: -exec/-execdir execute their argument
                # program. Kill-family executors veto; benign
                # executors (wc, ls, grep...) pass.
                for idx, t in enumerate(rest):
                    if t in ("-exec", "-execdir"):
                        execverb = (rest[idx + 1].rsplit('/', 1)[-1]
                                    .lower() if idx + 1 < len(rest)
                                    else "")
                        if execverb in ("kill", "pkill", "killall",
                                        "xargs", "sh", "bash", "zsh"):
                            raise _Block()
            elif verb == "docker":
                # lpkit-z4b B: container lifecycle control of
                # litellm-named containers, per the protected-labels
                # vocabulary. Read-only docker (ps, logs, inspect)
                # passes.
                if rest and rest[0].lower() in ("kill", "stop",
                                                "rm", "restart",
                                                "pause"):
                    names = " ".join(rest[1:]).lower()
                    if _mentions_target(names, policy):
                        raise _Block()
            elif verb in ("node", "ruby", "perl", "php"):
                # lpkit-z4b B-08/B-09/B-10: inline interpreter
                # programs (-e / -c) are argv payloads. Kill-family
                # intent in the program text (process.kill, system
                # "kill ...", perl kill) blocks; benign inline
                # programs pass untouched.
                for idx, t in enumerate(rest):
                    if t in ("-e", "-c", "--eval") or t.startswith(
                            ("--eval=", "--execute=")):
                        operand = (t.split("=", 1)[1]
                                   if "=" in t and t.startswith("--")
                                   else (rest[idx + 1]
                                         if idx + 1 < len(rest)
                                         else ""))
                        if operand and _full_text_kill_intent(
                                operand, policy):
                            raise _Block()
            elif verb == "osascript":
                # (unchanged behavior; branch order only)
                # AppleScript/JXA is a first-class macOS execution surface.
                # `do shell script` / JXA system()-class calls execute shell
                # text through osascript's own interpreter. Extract the
                # operand and analyze it as shell on its own terms so the
                # NORMAL target-aware decision applies (unrelated `pkill
                # nginx` through osascript stays allowed, exactly as it is
                # on the direct surface). A shell-execution form whose
                # operand cannot be extracted (variable, concatenation,
                # template) is unprovable (FR-006) and vetoes; kill-family
                # vocabulary in an unextractable operand reaches the veto
                # via that rule, not via a blanket keyword match. Benign
                # AppleScripts (display dialog, tell app...) contain no
                # shell-execution form and pass untouched.
                joined = " ".join(rest)
                low_j = joined.lower()
                shell_forms = ("do shell script" in low_j
                               or "$.system(" in low_j
                               or "$.shell(" in low_j
                               or "doshellscript(" in low_j.replace(" ", "")
                               or _full_text_kill_intent(joined, policy))
                if shell_forms:
                    dss, dss_unx = _applescript_dss_payloads(joined)
                    jxa, jxa_unx = _jxa_shell_payloads(joined)
                    if (dss_unx or jxa_unx):
                        raise _Block()  # FR-006: unextractable operand
                    if dss or jxa:
                        for payload in dss + jxa:
                            _analyze_shell_text(payload, policy,
                                                get_snapshot, depth + 1)
                    else:
                        # shell_forms triggered by kill-family vocabulary,
                        # but no shell-execution form was extractable. The
                        # operand could be any osascript language/dialect —
                        # carrying signal intent it cannot be proven safe
                        # (FR-006), same rule as unresolved substitutions.
                        raise _Block()
            elif verb == "fuser":
                if any(t == "-k" or t.startswith("-k") for t in rest):
                    for tok in rest:
                        if tok.split("/")[0] == str(policy.port):
                            raise _Block()
            elif verb == "launchctl":
                _analyze_launchctl(rest, policy)
            elif verb == "brew":
                _analyze_brew(rest, policy)
    if kill_like and len(stages) > 1:
        # The kill stage's targets may arrive via stdin from another
        # lsof/ps/grep stage: block when any stage references the
        # configured port or the proxy name/label. Single-stage kills
        # were already decided by their own target/pattern analysis —
        # the cross-check must not re-veto on the pattern's own text.
        joined = " ".join(stage_texts).lower()
        if _mentions_target(joined, policy):
            raise _Block()


def _command_substitutions(text, *, with_spans=False):
    """Yield executable bodies (or positional spans), excluding inert text."""
    quote = None
    i = count = 0
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c == "'" and quote is None:
            quote = c
            i += 1
            continue
        if c == '"':
            quote = None if quote == '"' else '"'
            i += 1
            continue
        tick = c == '`'
        if tick or text.startswith('$(', i) or (
                quote is None and text[i:i + 2] in ('<(', '>(')):
            start = i + (1 if tick else 2)
            j, depth, inner_quote = start, 1, None
            while j < len(text):
                ch = text[j]
                if ch == "\\" and inner_quote != "'":
                    j += 2
                    continue
                if tick and ch == '`':
                    break
                if inner_quote:
                    if ch == inner_quote:
                        inner_quote = None
                elif ch in ("'", '"'):
                    inner_quote = ch
                elif ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= len(text):
                raise _Block()
            count += 1
            if count > MAX_CHUNKS:
                raise _Block()
            yield (i, j + 1, text[start:j]) if with_spans else text[start:j]
            i = j + 1
            continue
        i += 1


_HEAD_INTENT_RESIDUE_RE = re.compile(
    r"\b(?:kill|pkill|killall|fuser|launchctl|brew|sudo|sh|bash|zsh|eval)\b")


def _analyze_chunk_scanonly(head_text, policy, get_snapshot, depth):
    """FR-007 rows (b)/(c): bounded head-scan of an over-bound input.

    Kill-family intent in the head (row b) keeps the veto — the veto is
    never lost to the bound. An intent-free head that cannot be PARSED
    (unbalanced constructs like ${a[$((i++))]}) is unprovable (row c):
    refuse BOUND_SIZE instead of veto.
    """
    if _full_text_kill_intent(head_text, policy):
        _analyze_shell_text(head_text, policy, get_snapshot, depth + 1)
        return
    try:
        _analyze_shell_text(head_text, policy, get_snapshot, depth + 1)
    except _Block:
        raise _BoundRefuse("BOUND_SIZE")


# Row (b) full-text intent scan: word-boundary regex so shell
# whitespace variants (kill\t123, brew\tservices) cannot evade the
# literal-substring scan the way plain "kill " could (codex review P1).
_FULL_TEXT_INTENT_RE = re.compile(
    r"\b(?:pkill|killall|kill|fuser|launchctl|eval|xargs"
    r"|os\.kill|process_manage)\b"
    r"|\b(?:brew\s+services|sh\s+-c|kill\s+-|process\s+group)\b"
)


def _full_text_kill_intent(text, policy):
    """Row (b) fallback: whole-text keyword scan for kill-family intent.

    Bounded by a single regex pass, immune to regex burn, and tolerant
    of ANY shell whitespace between a verb and its operands. If any
    kill-family marker appears anywhere in the over-bound input, the
    veto is never lost to the bound.
    """
    return _FULL_TEXT_INTENT_RE.search(text.lower()) is not None


def _analyze_shell_text(text, policy, get_snapshot, depth=0):
    if depth > MAX_TOOL_CALL_DEPTH:
        raise _Block()
    if not isinstance(text, str):
        raise _Block()
    # FR-007 bound decision table: over input_text_bytes, run a bounded
    # head-scan. (a) provably intent-free → allow; (b) kill-family
    # intent in the inspectable head → veto (never lost to the bound);
    # (c) head unprovable → refuse BOUND_SIZE.
    size_bound = policy.bounds["input_text_bytes"] if policy and policy.bounds else MAX_TEXT_CHARS
    if len(text.encode("utf-8")) > size_bound:
        head = text[:HEAD_SCAN_CHARS]
        # (b) intent found in the inspectable head → the veto fires
        _analyze_chunk_scanonly(head, policy, get_snapshot, depth)
        # (a) head provably intent-free AND full-text benign keyword scan
        if _full_text_kill_intent(text, policy):
            raise _Block()
        # (c) benign keywords everywhere BUT unresolved shell constructs
        # ($((...)) / ${...}) make the remainder unprovable → BOUND_SIZE;
        # fully-resolvable benign shapes are row (a) allows
        if "${" in head or "$((" in head or "$[" in head:
            raise _BoundRefuse("BOUND_SIZE")
        return
    if len(text) > MAX_TEXT_CHARS:
        raise _Block()
    # lpkit-z4b B + lpkit-knp: heredoc bodies. Interpreter-headed
    # heredocs (sh/bash/zsh/python before <<DELIM) route their bodies
    # to the matching analyzer; ALL heredoc bodies are then excised
    # from the text before further analysis. A non-interpreter head
    # (cat >> file) makes the body pure DATA — it never executes, and
    # prose apostrophes in data must not make the command unparseable.
    heredoc_cuts = []
    for m in re.finditer(r"<<-?\s*['\"]?([A-Za-z_]\w*)['\"]?", text):
        delim = m.group(1)
        head = text[:m.start()].strip()
        head_tokens = head.split()
        if not head_tokens:
            continue
        hverb = head_tokens[-1].rsplit("/", 1)[-1].lower()
        body_m = re.search(
            r"^[ \t]*" + re.escape(delim) + r"\b.*$",
            text[m.end():], re.MULTILINE)
        if body_m is None:
            raise _Block()  # unterminated heredoc: unprovable
        body = text[m.end():m.end() + body_m.start()]
        if re.fullmatch(r"sh|bash|zsh", hverb):
            _analyze_shell_text(body, policy, get_snapshot, depth + 1)
        elif re.fullmatch(r"python[23]?(?:\.\d+)?", hverb):
            _analyze_code_text(body, policy, get_snapshot)
        heredoc_cuts.append((m.start(), m.end() + body_m.end()))
    if heredoc_cuts:
        buf = []
        last = 0
        for s, e in heredoc_cuts:
            buf.append(text[last:s])
            last = e
        buf.append(text[last:])
        text = "".join(buf)
    # lpkit-knp: shell comments are prose — an apostrophe in a
    # comment must not read as an opening quote (which would leave
    # the chunk unparseable and fail closed). Strip comments before
    # tokenization; # inside quotes is literal; mid-word # (${#var})
    # is not a comment start.
    text = _strip_shell_comments(text)
    # lpkit-5ix: simple literal assignments visible in this payload
    # back command-position variable resolution; anything else stays
    # unresolvable (FR-006).
    env = _collect_literal_vars(text)
    for substitution in _command_substitutions(text):
        _analyze_shell_text(substitution, policy, get_snapshot, depth + 1)
    chunks = _split_segments(text, _CHUNK_SEPS)
    if len(chunks) >= MAX_CHUNKS:
        raise _Block()
    for chunk in chunks:
        if chunk.strip():
            _analyze_chunk(chunk, policy, get_snapshot, depth, env)


# ---------------------------------------------------------------------------
# Python code scanning (execute_code / browser_exec / inline subprocess)
# ---------------------------------------------------------------------------

def _iter_string_literals(text, limit=80):
    """Use Python's parser for multiline/escaped literals and literal argv.

    Unknown argv operands become unresolved shell operands, never evaluated.
    Imported/generated code remains outside this bounded static contract.
    """
    tree = ast.parse(text)
    count = 0
    for position, node in enumerate(ast.walk(tree)):
        if position > 10000:
            raise _Block()
        value = None
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            value = node.value.decode('utf-8') if isinstance(node.value, bytes) else node.value
        elif isinstance(node, ast.JoinedStr):
            value = ''.join(part.value if isinstance(part, ast.Constant) and
                            isinstance(part.value, str) else '$UNRESOLVED'
                            for part in node.values)
        elif isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                executable = first.value.rsplit('/', 1)[-1]
                if executable in WRAPPER_VERBS or executable in (
                        'sh', 'bash', 'zsh', 'kill', 'pkill', 'killall',
                        'fuser', 'launchctl', 'brew', 'osascript'):
                    values = [str(part.value) if isinstance(part, ast.Constant)
                              and isinstance(part.value, (str, int)) else '$UNRESOLVED'
                              for part in node.elts]
                    value = shlex.join(values)
        if value is not None:
            count += 1
            if count >= limit:
                raise _Block()
            yield value


_SIGNAL0_FORMS = frozenset({"0", "signal.SIG_ZERO", "SIG_ZERO", "signal.SIG_0"})


def _all_os_kills_signal_zero(text):
    """True only when EVERY os.kill/killpg call in text uses signal 0."""
    for m in _OS_KILL_RE.finditer(text):
        start = m.end()
        depth, i = 1, start
        while i < len(text) and depth:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        if depth:  # unbalanced — not provable
            return False
        inner = text[start:i - 1]
        parts, buf, d = [], [], 0
        for ch in inner:
            if ch in "([":
                d += 1
            elif ch in ")]":
                d -= 1
            if ch == "," and d == 0:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        parts.append("".join(buf))
        if len(parts) < 2:
            return False  # no visible signal operand — not provable
        sig = parts[1].strip()
        if sig not in _SIGNAL0_FORMS:
            return False
    return True


def _analyze_code_text(text, policy, get_snapshot):
    if not isinstance(text, str):
        raise _Block()
    if len(text) > MAX_TEXT_CHARS:
        raise _Block()
    # Syntactic validity gate: code that does not parse as Python cannot
    # be statically proven safe. A real newline inside a single-quoted
    # string (the git multi-paragraph -m convention pasted into code, as
    # in the byte-exact refusal fixture) is a SyntaxError, not a
    # lifecycle threat: refuse with the bound's corrective category
    # (BOUND_SIZE/LGV-010 — reduce/restructure and resubmit), never a
    # lifecycle VETO. Malformed input stays REFUSED (fail-closed); only
    # the category is corrected.
    try:
        ast.parse(text)
    except SyntaxError:
        raise _BoundRefuse("BOUND_SIZE") from None
    if _OS_KILL_RE.search(text):  # os.kill( / os.killpg(
        # F-014 carve-out: signal-0 liveness probes pass; any real
        # signal — or an unprovable call shape — vetoes
        if not _all_os_kills_signal_zero(text):
            raise _Block()
    if "psutil" in text and _PSUTIL_KILL_RE.search(text):
        raise _Block()
    # F-017: writing to the guard's own deployed files via Python veto.
    # READS pass: open() is not a write primitive. A mode operand that
    # can write ('w', 'a', '+', 'x' — including nested/qualified modes
    # and mode= keyword), or an explicit .write( call on the marker
    # path, is required to veto. Byte-level scan (bounded, no regex
    # burn): every 'open(' occurrence whose mode argument contains a
    # writing character vetoes when the marker is present anywhere in
    # the text (the parse gate above bounds the shape).
    low = text.lower()
    if PLUGIN_DIR_MARKER in low and _python_write_intent(text, low):
        raise _Block()
    for literal in _iter_string_literals(text):
        low = literal.lower()
        if any(keyword in low for keyword in _SHELL_KEYWORDS):
            _analyze_shell_text(literal, policy, get_snapshot)
        # lpkit-z4b B-06: an exec()-built program is a string literal
        # that parses as Python — route it to the code analyzer so
        # os.kill/exec/indirect calls inside it get the SAME analysis
        # as top-level code. Bounded by the literal count cap.
        if literal.lstrip()[:1] not in ("",) and (
                "import " in literal or "os.kill" in literal
                or "getattr(" in literal):
            try:
                ast.parse(literal)
            except SyntaxError:
                pass
            else:
                _analyze_code_text(literal, policy, get_snapshot)
    # lpkit-z4b B-07: getattr(os, 'kill') is os.kill by indirection —
    # the literal 'kill' alone is inert, the CALL SHAPE is the signal
    if re.search(r"\bgetattr\s*\(\s*os\s*,\s*['\"]kill", text, re.I):
        raise _Block()


def _python_write_intent(text, low):
    """True when text opens (or writes) the guarded directory with a
    writing-capable primitive.

    Write primitives: open() with a mode containing w/a/x/+; the
    pathlib write family (write_text/write_bytes) invoked on the
    marker path; explicit .write( on a handle alongside the marker.
    open() with a read mode (or the default 'r') is a READ and passes.
    """
    for m in re.finditer(r"\bopen\s*\(", text):
        start = m.end()
        depth = 1
        j = start
        while j < len(text) and depth:
            ch = text[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            j += 1
        if depth:
            continue
        inner = text[start:j - 1]
        parts, buf, d = [], [], 0
        quote = None
        for ch in inner:
            if quote:
                buf.append(ch)
                if ch == quote:
                    quote = None
                continue
            if ch in "'\"":
                quote = ch
                buf.append(ch)
                continue
            if ch in "([":
                d += 1
            elif ch in ")]":
                d -= 1
            if ch == "," and d == 0:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        parts.append("".join(buf))
        mode = None
        for part in parts[1:]:
            p = part.strip()
            if p.startswith("mode"):
                p = p.split("=", 1)[-1].strip()
            if len(p) >= 2 and p[0] in "'\"" and p[-1] == p[0]:
                mode = p[1:-1]
                break
        if mode and any(c in mode for c in "wax+"):
            return True
        if mode is None and (len(parts) < 2 or "mode" not in inner):
            # default mode 'r' — a read
            continue
        if "write" in low and ".write(" in text:
            # explicit write call present alongside open() with the
            # marker: conservative veto (write-on-opened-handle intent)
            return True
    if ".write_text(" in low or ".write_bytes(" in low:
        # pathlib write family invoked with the marker present
        return True
    if ".write(" in text and "open(" not in text:
        # write onto an already-open handle (e.g. Path.open_text chains)
        # with the marker present — still write intent
        return True
    return False


# ---------------------------------------------------------------------------
# Tool-call recursion and process_manage policy
# ---------------------------------------------------------------------------

def _check_process_manage(arguments, policy):
    if not isinstance(arguments, dict):
        raise _Block()
    action = str(arguments.get("action", "")).lower()
    if action not in PROCESS_MUTATING_ACTIONS:
        return  # poll/status/wait/list are read-only
    session = arguments.get("session_id", arguments.get("session", ""))
    if not session:
        raise _Block()  # mutating action without a determinate session
    candidate = str(session)
    # FR-006: unresolved operands (variables/substitutions) can never be
    # proven unrelated to the protected session — refuse, never guess
    if "$" in candidate or "`" in candidate:
        raise _Block()
    if any(full.startswith(candidate) or
           full.removeprefix('proc_').startswith(candidate.removeprefix('proc_'))
           for full in policy.protected_sessions):
        raise _Block()


def _analyze_tool_call(arguments, policy, get_snapshot, depth=0):
    if depth > MAX_TOOL_CALL_DEPTH:
        raise _BoundRefuse("BOUND_DEPTH")
    if not isinstance(arguments, dict):
        raise _Block()
    calls = arguments.get("calls")
    if calls is None and str(arguments.get('name') or '').strip():
        calls = [{'name': arguments['name'], 'arguments': arguments.get('arguments')}]
    # F-009/US1-S7: JSON-string-encoded call lists unwrap iteratively
    # (bounded); a non-decoding string is an unprovable shape (guard-hardening:
    # SHAPE_ENVELOPE, never a VETO — a hand-typed envelope that fails
    # json.loads is a shape defect with corrective guidance, not a
    # lifecycle threat)
    for _ in range(4):
        if not isinstance(calls, str):
            break
        if len(calls) > MAX_TEXT_CHARS:
            raise _BoundRefuse("BOUND_SIZE")
        try:
            calls = json.loads(calls)
        except Exception:
            raise _BoundRefuse("SHAPE_ENVELOPE")
    if isinstance(calls, dict):
        calls = [calls]
    if not isinstance(calls, list) or not calls:
        raise _Block()
    # FR-007 bound decision table for count: over max_chunks, analyze the
    # inspectable head; intent-free benign batches pass (row a), intent
    # vetoes (row b), unprovable shapes refuse BOUND_COUNT (row c)
    max_chunks = (policy.bounds["max_chunks"] if policy and policy.bounds
                  else MAX_CHUNKS)
    if len(calls) > max_chunks:
        if len(calls) > HARD_BATCH_CAP:
            raise _BoundRefuse("BOUND_COUNT")
        head, tail = calls[:max_chunks], calls[max_chunks:]
        # analyze the head fully; a head hazard vetoes regardless of size
        _analyze_calls_list(head, policy, get_snapshot, depth)
        # every tail element must be independently provable: same shape
        # as the benign head element → row (a) allow; anything else is
        # an uninspectable remainder → row (c) refuse BOUND_COUNT
        tail_ok = all(_call_is_benign_shape(c) for c in tail)
        if not tail_ok:
            raise _BoundRefuse("BOUND_COUNT")
        return
    _analyze_calls_list(calls, policy, get_snapshot, depth)


def _call_is_benign_shape(call):
    """A provably intent-free call for FR-007 row (a) tail analysis.

    Amendment 3a: only the CANONICAL name/arguments key
    shape is eligible for benign-tail analysis. A non-canonical shape
    (alternate keys such as tool/args) is UNPROVABLE — absence of
    recognized keys is never evidence of absence of executable content —
    so the oversized batch falls to FR-007 row (c) BOUND_COUNT.
    """
    if not isinstance(call, dict):
        return False
    name = str(call.get("name") or "").strip().lower()
    args = call.get("arguments")
    if "arguments" not in call or not name:
        return False  # non-canonical key shape: never benign
    if not isinstance(args, dict):
        return False
    if _call_has_executable_content(call):
        return False  # unprovable extra keys: never benign (amendment 3a)
    action = str(args.get("action", "")).lower()
    if name == "process_manage":
        return action not in PROCESS_MUTATING_ACTIONS
    if name in EXECUTION_TOOLS:
        return False  # execution payloads are never assumed benign
    return action == "" and not _executable_looking(args)


def _inert_plain(value):
    """A plain (non-executable-looking) scalar/structure.

    Inert: None, bool, int, float, plain strings, and containers (dict/
    list/tuple) whose EVERY element is inert — arbitrary nesting. The
    recursion is bounded by the caller's depth/size envelope: it only
    runs over the values of one inner call's unknown keys, and the
    _executable_looking scan for dicts is the same heuristic used for
    canonical arguments.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return True
    if isinstance(value, dict):
        # key-sensitive: a nested dict carrying command/code/calls
        # (executable-looking keys) is NOT inert, same heuristic as the
        # top-level unknown-key branch (_executable_looking).
        if _executable_looking(value):
            return False
        return all(_inert_plain(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_inert_plain(v) for v in value)
    return False  # unknown type: unprovable


def _call_has_executable_content(call):
    """Amendment 3a: executable-looking content in ANY key
    beyond the canonical name/arguments/calls trio.

    An unrecognized key carrying executable-looking content makes the
    whole call unprovable — absence of recognized keys is never evidence
    of absence of executable content.

    guard-hardening (amended Edge Case): a list/tuple under an unknown key is
    inert when EVERY element is plain (string/number/inert dict,
    arbitrarily nested); only lists containing executable-looking
    content refuse. Previously any non-empty list refused — over-strict
    vs the amended Edge Case, which lets plain inert content pass.
    """
    for key, val in call.items():
        if key in ("name", "arguments", "calls"):
            continue  # canonical keys flow through their own analysis
        if isinstance(val, dict) and _executable_looking(val):
            return True
        if isinstance(val, str) and val.strip() and key in ("command", "code"):
            return True
        if isinstance(val, (list, tuple)) and val:
            # guard-hardening: inspect the elements — inert plain lists pass
            # (tags, ids, matrices); executable-looking content refuses.
            if not _inert_plain(val):
                return True
    return False


def _analyze_calls_list(calls, policy, get_snapshot, depth):
    """The per-call loop shared by the in-bound and over-bound paths."""
    for call in calls:
        if not isinstance(call, dict):
            raise _Block()
        name = str(call.get("name") or "").strip()
        call_args = call.get("arguments")
        if call_args is None or (isinstance(call_args, str) and not call_args.strip()):
            call_args = {}
        for _ in range(3):
            if not isinstance(call_args, str):
                break
            if len(call_args) > MAX_TEXT_CHARS:
                raise _BoundRefuse("BOUND_SIZE")
            try:
                call_args = json.loads(call_args)
            except Exception:
                # guard-hardening: a non-decoding arguments string is an
                # unprovable shape, not a lifecycle threat — corrective
                # SHAPE_ENVELOPE guidance, never the VETO message.
                raise _BoundRefuse("SHAPE_ENVELOPE")
        if not isinstance(call_args, dict):
            raise _Block()
        # Amendment 3a: keys beyond the canonical pair are
        # unprovable — an unrecognized key (tool/args, extra, ...) carrying
        # executable-looking content is a SHAPE_ENVELOPE refusal, never a
        # silent skip of argument inspection.
        if _call_has_executable_content(call):
            raise _BoundRefuse("SHAPE_ENVELOPE")
        # hook-contract rule 1: the name must be attributable to a known
        # tool. Case-variants normalize; unknown or empty names carrying
        # executable-looking arguments are HOOK_MISMATCH, never a pass.
        low_name = name.lower()
        if not name:
            # an envelope-shaped body ({"calls": ...}) recurses; rule 1
            # applies to the inner calls (F-009 double-wrap/nameless)
            if isinstance(call.get("calls"), (list, dict, str)):
                _analyze_tool_call(call, policy, get_snapshot, depth + 1)
                continue
            if _executable_looking(call_args):
                raise _HookMismatch()
            continue
        if low_name not in (EXECUTION_TOOLS | {"process_manage"}
                            | READONLY_TOOLS):
            if _executable_looking(call_args):
                raise _HookMismatch()
            continue  # unknown name, nothing executable-looking: no hazard
        if low_name in READONLY_TOOLS:
            if _executable_looking(call_args):
                raise _HookMismatch()  # case-variant read-only carrying payload
            continue
        _dispatch_execution_tool(low_name, call_args, policy, get_snapshot,
                                 depth + 1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Tools the runtime recognizes as read-only for hook-contract rule 1:
# a case-variant of one of these carrying executable-looking arguments is
# a HOOK_MISMATCH, while a case-variant of an EXECUTION_TOOL dispatches
# to the real analyzer (US1-S8, T010).
READONLY_TOOLS = frozenset({
    "read_file", "write_file", "patch", "search_files", "web_search",
    "web_extract", "gws_gmail_read", "gws_docs", "gws_sheets_read",
    "vision_analyze", "session_search", "skills_list", "skill_view",
})

def _executable_looking(args):
    """Arguments that could carry executable payload (rule 1 heuristic)."""
    if not isinstance(args, dict):
        return False
    for key in ("command", "code", "calls"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return True
        if val is not None and not isinstance(val, str):
            return True
    return False

def _dispatch_execution_tool(name, args, policy, get_snapshot, depth=0):
    """Case-insensitive dispatch of an execution-tool name (T010)."""
    low = name.lower()
    if name in EXECUTION_TOOLS:
        canonical = name
    elif low in EXECUTION_TOOLS:
        canonical = low  # case-variant of a known execution tool
    elif low == "process_manage":
        _check_process_manage(args, policy)
        return None
    else:
        # rule 1: unknown name carrying executable-looking arguments
        if _executable_looking(args):
            raise _HookMismatch()
        return None  # unknown name, nothing executable-looking
    if canonical == "terminal":
        _analyze_shell_text(args.get("command"), policy, get_snapshot)
    elif canonical in ("execute_code", "browser_exec"):
        _analyze_code_text(args.get("code"), policy, get_snapshot)
    elif canonical == "process_manage":
        _check_process_manage(args, policy)
    else:  # tool_call
        _analyze_tool_call(args, policy, get_snapshot, depth)
    return None


class _HookMismatch(Exception):
    """Hook-contract rule 1: unattributable tool name, fail closed."""


class _Refuse(Exception):
    """Internal: refuse with a bound/shape category (FR-007)."""

    def __init__(self, category):
        super().__init__(category)
        self.category = category


def evaluate(tool_name, args, policy_path, *, discover=None):
    """Return None to allow, or a block decision dict with category+code.

    ``discover`` is injectable for tests (defaults to the real read-only
    lsof/ps discovery). Non-execution tools (read_file, search_files, ...)
    are never blocked, even with a missing or bad policy file (FR-005
    split). Policy is re-read and re-authenticated from disk on EVERY
    evaluation — no caching (FR-008/D5).
    """
    try:
        name = str(tool_name)
    except Exception:
        return _block("HOOK_MISMATCH", "")
    if name not in EXECUTION_TOOLS and name.lower() != "process_manage":
        # top-level unknown/empty name carrying executable-looking
        # arguments is a hook-contract mismatch and fails closed (rule 1,
        # Edge Cases, F-005); recognized read-only names pass unmodified
        if _executable_looking(args) and (
                not name or name.lower() not in READONLY_TOOLS):
            return _block("HOOK_MISMATCH", name)
        return None
    name = name.lower()
    try:
        if not isinstance(args, dict):
            raise _Block()
        policy = load_policy(policy_path)
    except IntegrityRefError:
        # FR-015: its own distinct fail-closed category + owner repair
        if name == "process_manage":
            action = str(args.get("action", "")).lower()
            if action in PROCESS_MUTATING_ACTIONS:
                return _block("INTEGRITY_REF_INVALID", name)
            return None
        return _block("INTEGRITY_REF_INVALID", name)
    except _HookMismatch:
        return _block("HOOK_MISMATCH", name)
    except _Block:
        return _block("VETO", name)
    except Exception:
        # Missing/bad config: block execution tools with the sanitized
        # category message. process_manage read-only actions still pass.
        if name == "process_manage":
            action = str(args.get("action", "")).lower() if isinstance(args, dict) else ""
            if action in PROCESS_MUTATING_ACTIONS:
                return _block("POLICY_INVALID", name)
            return None
        return _block("POLICY_INVALID", name)
    try:
        if name == "process_manage":
            _check_process_manage(args, policy)
            return None
        snapshot_box = []

        def get_snapshot():
            if not snapshot_box:
                snapshot_box.append(
                    discover(policy.port) if discover is not None
                    else discover_snapshot(policy.port)
                )
            return snapshot_box[0]

        _dispatch_execution_tool(name, args, policy, get_snapshot)
        return None
    except _HookMismatch:
        return _block("HOOK_MISMATCH", name)
    except _BoundRefuse as refuse:
        return _block(refuse.category, name)
    except _Block:
        return _block("VETO", name)
    except Exception:
        return _block("VETO", name)  # never fail open for execution tools