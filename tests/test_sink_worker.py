"""Offline behavioral tests for the bounded asynchronous decision-sink worker.

NO production contact in this file: no subprocesses, no signals, no real
process discovery, no network, no real tool execution, and no real persist
writes — setUp installs a per-module _sink_persist spy for every test that
preserves call tuples and asserts worker-thread ownership. Every test loads
a FRESH instance of the guard module under a unique sys.modules key with
XDG_STATE_HOME already pointing at a disposable temporary directory, so
module import starts nothing and no test can touch owner state.

Focus (decision-sink contract D4/D5/D6/D12):
- log_decision is immediate canonical-logger telemetry, independent from
  asynchronous admission (drops on busy/full/offline/wedged/startup-fail).
- Admission is nonblocking (try-lock), bounded (pinned queue max 64 plus
  one in-flight), and captures (state home, record, byte cap) at submit
  so later environment changes cannot redirect an already-queued job.
- sink_flush/sink_shutdown are deadline-bounded across lock acquisition,
  account for queued AND in-flight work, reject invalid timeouts, and
  never replace a stuck worker: at most one daemon generation.
- Startup Thread constructor/start failures are latched (spec D5): one
  start attempt per generation, reported False without propagation; a
  repeated sink_start stays False with no further worker construction.
- sink_status is a fixed-schema, non-secret counter surface: the required
  key set (plus any fixed additional flags) is stable across operations,
  with exact bool/int values and counters bounded by (1<<63)-1.

Teardown discipline (LIFO): test-body helper threads release and join
first; then artificial stalls release, a bounded sink_shutdown runs, and
the worker is proven stopped; then fresh sys.modules keys are removed and
XDG_STATE_HOME restored; tracked temporary directories are deleted only
LAST — and retained (never auto-finalized) if the worker was not proven
stopped. Cleanup failures are re-raised after termination checks, never
swallowed.
Actual evaluate() refusal wiring is intentionally not asserted here (see
author report): the blocked-call arg shape lives outside the authorized
source region, and inventing it would test fiction.
"""
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from pathlib import Path
from typing import Any, cast

MODULE_PATH = (Path(__file__).resolve().parents[1] /
               "integrations/hermes/proxy-lifecycle-guard/guard.py")

# Pinned contract constants (published values, not derived per-run).
SINK_QUEUE_MAX_PINNED = 64
SINK_THREAD_NAME = "proxy-lifecycle-guard-sink"
CANONICAL_LOGGER_NAME = "proxy_lifecycle_guard"
CANONICAL_LOG_MSG = ("lifecycle-guard decision: "
                     "category=%s code=%s tool=%s")
# Required subset: core fixes may add fixed flags, never remove these.
STATUS_REQUIRED_KEYS = frozenset({
    "started", "stopping", "worker_alive", "queued", "in_flight",
    "queue_max", "admitted", "written", "failed", "dropped", "discarded",
})
STATUS_LIFECYCLE_BOOLS = ("started", "stopping", "worker_alive")
STATUS_COUNTER_KEYS = frozenset({
    "queued", "in_flight", "queue_max", "admitted", "written",
    "failed", "dropped", "discarded",
})
COUNTER_MAX = (1 << 63) - 1

VALID_PAIR = ("VETO", "LGV-001")  # canonical category/code pair


def load_fresh_guard(keys):
    """Fresh module instance under a unique sys.modules key (the key is
    appended to `keys` so teardown removes it only after the bounded
    shutdown) so no test shares sink globals with any other generation."""
    key = "sink_guard_test_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(key, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load guard module for sink tests")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    keys.append(key)
    # Cast to a typed namespace: the module is loaded dynamically from a
    # path, so Pyright cannot know its attributes. Monkey-patching sink
    # collaborators (_sink_persist, logger, _sink_admit, threading) on
    # this handle is plain attribute assignment on a typed object.
    return cast("Any", mod)


def sink_thread_count():
    return sum(1 for t in threading.enumerate()
               if t.name == SINK_THREAD_NAME and t.is_alive())


class SinkWorkerTest(unittest.TestCase):
    def setUp(self):
        self._tracked_dirs = []
        self._worker_stopped = False
        self._fresh_module_keys = []
        self._persist_ownership_errors = []
        self._worker_refs = {}
        # Registered FIRST => runs LAST (LIFO): tracked directories are
        # deleted only after stalls were released, helper threads joined,
        # and the worker proven stopped — retained otherwise.
        self.addCleanup(self._cleanup_tracked_dirs)
        # Registered SECOND => runs second-to-last: fresh module keys
        # leave sys.modules only after the bounded shutdown above them.
        self.addCleanup(self._cleanup_fresh_modules)
        self.state = types.SimpleNamespace(name=self._new_dir("sink_state_"))
        # XDG state is set BEFORE the fresh import: importing the module
        # must start nothing and touch no filesystem.
        self._old_state_home = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = self.state.name
        if self._old_state_home is None:
            self.addCleanup(os.environ.pop, "XDG_STATE_HOME", None)
        else:
            self.addCleanup(os.environ.__setitem__,
                            "XDG_STATE_HOME", self._old_state_home)
        self.mod = load_fresh_guard(self._fresh_module_keys)
        real_threading = self.mod.threading
        original_thread_factory = real_threading.Thread
        worker_refs = self._worker_refs

        class ThreadingProxy:
            def __getattr__(self, name):
                return getattr(real_threading, name)

            def Thread(self, *args, **kwargs):
                worker = original_thread_factory(*args, **kwargs)
                worker_refs[id(worker)] = worker
                return worker

        self.mod.threading = ThreadingProxy()
        self.stall = threading.Event()
        # Registered LAST => runs FIRST: release stalls, run a bounded
        # shutdown, assert termination — before the cleanups above.
        self.addCleanup(self._bounded_teardown)
        # Default persist spy: no test performs a real write, and every
        # persist call is proven to run on this module's worker thread.
        self.install_persist_spy()

    # -- teardown and shared helpers ---------------------------------------

    def _new_dir(self, prefix):
        path = tempfile.mkdtemp(prefix=prefix)
        self._tracked_dirs.append(path)
        return path

    def _cleanup_tracked_dirs(self):
        if not self._worker_stopped:
            return  # retain fixtures: worker never proven stopped
        for path in self._tracked_dirs:
            shutil.rmtree(path, ignore_errors=True)

    def _cleanup_fresh_modules(self):
        if not self._worker_stopped:
            return
        for key in self._fresh_module_keys:
            sys.modules.pop(key, None)

    def _bounded_teardown(self):
        m = self.mod

        def remember_current():
            worker = getattr(m, "_sink_worker", None)
            if worker is not None:
                self._worker_refs[id(worker)] = worker

        self.stall.set()  # release any artificial writer stall FIRST
        remember_current()
        shutdown_error = None
        try:
            m.sink_shutdown(5.0)
        except Exception as exc:
            shutdown_error = exc  # re-raised after termination checks
        remember_current()

        deadline = time.monotonic() + 2.0
        liveness_errors = []
        join_errors = []
        for worker in tuple(self._worker_refs.values()):
            try:
                alive = worker.is_alive()
            except Exception as exc:
                liveness_errors.append((worker, exc))
                continue
            # A failed-start fake or never-started thread is not joinable.
            if alive:
                try:
                    worker.join(max(0.0, deadline - time.monotonic()))
                except Exception as exc:
                    join_errors.append((worker, exc))

        still_live = []
        for worker in tuple(self._worker_refs.values()):
            try:
                if worker.is_alive():
                    still_live.append(worker)
            except Exception as exc:
                liveness_errors.append((worker, exc))
        if still_live or liveness_errors:
            self.fail("sink worker still alive or unverified after bounded "
                      "teardown shutdown (alive=%d, unknown=%d)"
                      % (len(still_live), len(liveness_errors)))
        self._worker_stopped = True
        self.assertFalse(
            self._persist_ownership_errors,
            "persist spy ran outside the worker thread: %r"
            % (self._persist_ownership_errors,))
        if shutdown_error is not None:
            raise shutdown_error
        if join_errors:
            raise join_errors[0][1]

    def status(self):
        return self.mod.sink_status()

    def assert_status_shape(self, st):
        """Required keys present (fixed extras allowed), exact bool/int
        types, and every counter a nonnegative int within (1<<63)-1."""
        keys = set(st)
        self.assertTrue(STATUS_REQUIRED_KEYS <= keys,
                        "status missing required keys: %s"
                        % sorted(STATUS_REQUIRED_KEYS - keys))
        for key in STATUS_LIFECYCLE_BOOLS:
            self.assertIsInstance(st[key], bool, key)
        for key in STATUS_COUNTER_KEYS:
            value = st[key]
            self.assertIsInstance(value, int, key)
            self.assertNotIsInstance(value, bool, key)
        for key, value in st.items():
            self.assertIsInstance(value, (bool, int), key)
            self.assertNotIsInstance(value, float, key)
            if not isinstance(value, bool):
                self.assertGreaterEqual(value, 0, key)
                self.assertLessEqual(value, COUNTER_MAX, key)

    def install_persist_spy(self, stall=False, result=True, raise_exc=None,
                            entered=None):
        """Replace _sink_persist on this fresh module instance only. The
        fake preserves call tuples, records any call made off the worker
        thread (asserted empty in teardown), optionally signals `entered`
        before an optional stall wedges the writer on the per-test event,
        then returns `result` or raises `raise_exc` like real persist."""
        m = self.mod
        calls = []

        def fake(home, record, cap):
            calls.append((home, record, cap))
            if threading.current_thread() is not getattr(
                    m, "_sink_worker", None):
                self._persist_ownership_errors.append(
                    threading.current_thread().name)
            if entered is not None:
                entered.set()
            if stall:
                self.stall.wait(10.0)  # bounded even if never released
            if raise_exc is not None:
                raise raise_exc
            return result

        m._sink_persist = fake
        return calls

    def admit_until(self, tool, attempts=50, delay=0.01):
        """Positive-path admission with a brief fixture-level retry: a
        freshly started worker may transiently hold the sink lock between
        queue checks, and a positive setup admit must not misread that
        transient as a capacity drop. Negative checks never use this."""
        m = self.mod
        for _ in range(attempts):
            if m._sink_admit(*VALID_PAIR, tool):
                return True
            time.sleep(delay)
        return False

    def start_and_stall_in_flight(self):
        """Start the worker and wedge it on exactly one in-flight job,
        proven by a bounded spy-entered event (not racy status polling).
        Returns the spy call list."""
        m = self.mod
        entered = threading.Event()
        calls = self.install_persist_spy(stall=True, entered=entered)
        self.assertTrue(m.sink_start())
        self.assertTrue(self.admit_until("terminal"))
        self.assertTrue(entered.wait(5.0),
                        "worker never entered the first in-flight persist")
        return calls

    def hold_sink_lock(self, hold_timeout=5.0):
        """Hold the sink lock in a helper thread. The entered Event is
        set INSIDE the acquired lock, proving this holder owns it —
        polling lock.locked() can observe a transient wrong owner.
        Registered cleanup releases and joins the holder on every path
        (join only if alive). Returns (holder, release)."""
        m = self.mod
        release = threading.Event()
        entered = threading.Event()

        def hold():
            with m._sink_lock:
                entered.set()
                release.wait(hold_timeout)

        holder = threading.Thread(target=hold, name="sink-test-lock-holder")
        holder.start()

        def _release_holder():
            release.set()
            if holder.is_alive():
                holder.join(5.0)

        self.addCleanup(_release_holder)
        self.assertTrue(entered.wait(5.0),
                        "helper never acquired the sink lock")
        return holder, release

    def _failing_thread_shim(self, real, thread_cls):
        class ThreadingShim:
            def __init__(self, real_threading, cls):
                self._real = real_threading
                self._cls = cls

            def __getattr__(self, name):
                return getattr(self._real, name)

            def Thread(self, *a, **k):
                return self._cls(*a, **k)

        return ThreadingShim(real, thread_cls)

    def _seed_counter(self, name, value):
        """Park a status counter at `value` via whichever mutable store
        the module exposes (a dict keyed by the status names, or a
        module-level _sink_<name> int). Returns True if seeded."""
        m = self.mod
        for attr in vars(m).values():
            if (isinstance(attr, dict) and name in attr
                    and isinstance(attr[name], int)
                    and not isinstance(attr[name], bool)):
                attr[name] = value
                return True
        alt = getattr(m, "_sink_" + name, None)
        if isinstance(alt, int) and not isinstance(alt, bool):
            setattr(m, "_sink_" + name, value)
            return True
        return False

    # -- 1. import inertness ----------------------------------------------

    def test_import_is_inert_and_admission_never_spawns(self):
        m = self.mod
        st = m.sink_status()
        self.assert_status_shape(st)
        self.assertFalse(st["started"])
        self.assertFalse(st["stopping"])
        self.assertFalse(st["worker_alive"])
        self.assertEqual(st["queued"], 0)
        self.assertEqual(st["in_flight"], 0)
        self.assertEqual(st["admitted"], 0)
        self.assertEqual(sink_thread_count(), 0)
        # No state I/O happened at import: nothing under the state home.
        self.assertFalse(any(Path(self.state.name).rglob("*")))
        # log_decision before start: immediate, no exception, no spawn.
        t0 = time.monotonic()
        self.assertIsNone(m.log_decision("VETO", "terminal"))
        self.assertLess(time.monotonic() - t0, 1.0)
        st = self.status()
        self.assertFalse(st["started"])
        self.assertFalse(st["worker_alive"])
        self.assertEqual(st["admitted"], 0)
        self.assertEqual(sink_thread_count(), 0)
        self.assertFalse(any(Path(self.state.name).rglob("*")))

    # -- 2-4. log_decision surface ----------------------------------------

    def test_log_decision_immediate_canonical_logger_independent_of_admission(self):
        m = self.mod
        self.assertEqual(m.logger.name, CANONICAL_LOGGER_NAME)
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Capture()
        lg = logging.getLogger(CANONICAL_LOGGER_NAME)
        old_level = lg.level  # restored below: no global level leak
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
        try:
            t0 = time.monotonic()
            # Qualified tool name is sanitized to its canonical terminal.
            self.assertIsNone(m.log_decision("VETO", "tools.terminal"))
            self.assertLess(time.monotonic() - t0, 1.0)
        finally:
            lg.removeHandler(handler)
            lg.setLevel(old_level)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec.name, CANONICAL_LOGGER_NAME)
        self.assertEqual(rec.levelno, logging.INFO)
        self.assertEqual(rec.msg, CANONICAL_LOG_MSG)
        self.assertEqual(rec.args, ("VETO", "LGV-001", "terminal"))
        # Telemetry was emitted even though admission was dropped
        # (worker never started): logging is independent of the sink.
        self.assertEqual(self.status()["admitted"], 0)

    def test_log_decision_invalid_inputs_sanitized_never_raise(self):
        m = self.mod
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Capture()
        lg = logging.getLogger(CANONICAL_LOGGER_NAME)
        old_level = lg.level  # restored below: no global level leak
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
        try:
            for bad in (None, 123, b"VETO", "NOPE", "V" * 129):
                t0 = time.monotonic()
                self.assertIsNone(m.log_decision(bad, "terminal"))
                self.assertLess(time.monotonic() - t0, 1.0)
            # Valid category with a non-string tool is sanitized too.
            self.assertIsNone(m.log_decision("VETO", None))
        finally:
            lg.removeHandler(handler)
            lg.setLevel(old_level)
        sanitized = [r for r in records if r.args[0] == "UNKNOWN"]
        self.assertEqual(len(sanitized), 5)
        for rec in sanitized:
            self.assertEqual(rec.msg, CANONICAL_LOG_MSG)
            self.assertEqual(rec.args, ("UNKNOWN", "LGV-000", "terminal"))
        valid = [r for r in records if r.args[0] == "VETO"]
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0].args, ("VETO", "LGV-001", "other"))
        # Nothing invalid ever reached admission.
        self.assertEqual(self.status()["admitted"], 0)

    def test_logger_admission_and_worker_exceptions_never_escape(self):
        m = self.mod
        # A raising logger is contained by log_decision.
        class BoomLogger:
            def info(self, *a, **k):
                raise RuntimeError("logger boom")

        original_logger = m.logger
        m.logger = BoomLogger()
        try:
            self.assertIsNone(m.log_decision("VETO", "terminal"))
        finally:
            m.logger = original_logger
        # A raising admission path is contained too.
        original_admit = m._sink_admit

        def boom_admit(*a, **k):
            raise RuntimeError("admit boom")

        m._sink_admit = boom_admit
        try:
            self.assertIsNone(m.log_decision("VETO", "terminal"))
        finally:
            m._sink_admit = original_admit
        # A raising worker persist is contained: counted failed, drained.
        calls = self.install_persist_spy(raise_exc=ValueError("persist boom"))
        self.assertTrue(m.sink_start())
        self.assertTrue(self.admit_until("terminal"))
        self.assertTrue(m.sink_flush(5.0))
        st = self.status()
        self.assert_status_shape(st)
        self.assertEqual(st["failed"], 1)
        self.assertEqual(st["written"], 0)
        self.assertEqual(len(calls), 1)

    # -- 5-6. admission gating and nonblocking ----------------------------

    def test_admission_gated_on_started_worker(self):
        m = self.mod
        self.assertFalse(m._sink_admit(*VALID_PAIR, "terminal"))  # single attempt
        st = self.status()
        self.assertFalse(st["started"])
        self.assertFalse(st["worker_alive"])
        self.assertGreaterEqual(st["dropped"], 1)
        self.assertEqual(sink_thread_count(), 0)
        self.assertTrue(m.sink_start())
        self.assertTrue(self.admit_until("terminal"))
        st = self.status()
        self.assertTrue(st["started"])
        self.assertTrue(st["worker_alive"])
        self.assertEqual(st["admitted"], 1)
        self.assertEqual(sink_thread_count(), 1)
        self.assertTrue(m.sink_flush(5.0))
        self.assertEqual(self.status()["queued"], 0)

    def test_admission_nonblocking_while_lock_held(self):
        m = self.mod
        self.assertTrue(m.sink_start())
        pre = self.status()  # prebusy snapshot; status is never called
        self.assert_status_shape(pre)  # while the lock is held below
        holder, release = self.hold_sink_lock()
        try:
            t0 = time.monotonic()
            self.assertFalse(m._sink_admit(*VALID_PAIR, "terminal"))
            self.assertLess(time.monotonic() - t0, 0.5)
            t0 = time.monotonic()
            self.assertIsNone(m.log_decision("VETO", "terminal"))
            self.assertLess(time.monotonic() - t0, 0.5)
        finally:
            release.set()
            holder.join(5.0)
            self.assertFalse(holder.is_alive())
        post = self.status()
        self.assert_status_shape(post)
        self.assertEqual(post["admitted"], 0)
        # Busy-lock drops surface either as dropped increments or as a
        # latched boolean degradation flag (sticky diagnostics allowed).
        degraded = [key for key, value in post.items()
                    if key not in STATUS_LIFECYCLE_BOOLS
                    and isinstance(value, bool) and value
                    and isinstance(pre.get(key), bool) and not pre[key]]
        self.assertTrue(post["dropped"] >= pre["dropped"] + 2 or degraded,
                        "busy-lock drops must surface via dropped or a "
                        "degradation flag")

    # -- 7-8. queue bound and capture-before-queue ------------------------

    def test_queue_bound_is_64_with_one_in_flight(self):
        m = self.mod
        calls = self.start_and_stall_in_flight()
        # Fill the pending queue exactly to the pinned bound.
        for _ in range(SINK_QUEUE_MAX_PINNED):
            self.assertTrue(self.admit_until("terminal"))
        st = self.status()
        self.assert_status_shape(st)
        self.assertEqual(st["queued"], SINK_QUEUE_MAX_PINNED)
        self.assertEqual(st["in_flight"], 1)
        self.assertEqual(st["queue_max"], SINK_QUEUE_MAX_PINNED)
        # One more is dropped (single attempt), not queued; 64 is the
        # pending ceiling.
        self.assertFalse(m._sink_admit(*VALID_PAIR, "terminal"))
        st = self.status()
        self.assertEqual(st["queued"], SINK_QUEUE_MAX_PINNED)
        self.assertGreaterEqual(st["dropped"], 1)
        # The wedged worker processed only the in-flight job so far.
        self.assertEqual(len(calls), 1)

    def test_admission_captures_state_home_and_cap_per_job(self):
        m = self.mod
        m.SINK_MAX_BYTES = 512  # literal test cap for the first job
        calls = self.start_and_stall_in_flight()
        home_a = self.state.name
        other = types.SimpleNamespace(name=self._new_dir("sink_state_alt_"))
        # Redirect the environment AND the cap AFTER the first job was
        # admitted: neither may redirect the already-queued job.
        os.environ["XDG_STATE_HOME"] = other.name
        m.SINK_MAX_BYTES = 1024
        self.assertTrue(self.admit_until("execute_code"))
        self.assertEqual(self.status()["queued"], 1)
        self.stall.set()
        self.assertTrue(m.sink_flush(5.0))
        self.assertEqual(len(calls), 2)
        (home1, rec1, cap1), (home2, rec2, cap2) = calls
        # The old job kept its captured home and cap; only the new job
        # moved. Literal pins: both were frozen at submit time.
        self.assertEqual(home1, home_a)
        self.assertEqual(home2, other.name)
        self.assertEqual(cap1, 512)
        self.assertEqual(cap2, 1024)
        for record, tool in ((rec1, "terminal"), (rec2, "execute_code")):
            self.assertIsInstance(record, bytes)
            self.assertIn(b"VETO", record)
            self.assertIn(b"LGV-001", record)
            # Canonical 4-key JSON record (first line: JSONL-tolerant).
            doc = json.loads(record.strip().splitlines()[0])
            self.assertEqual(len(doc), 4)
            self.assertIn("VETO", doc.values())
            self.assertIn("LGV-001", doc.values())
            self.assertIn(tool, doc.values())

    # -- 9-11. flush/shutdown deadlines ------------------------------------

    def test_flush_counts_queued_and_in_flight(self):
        m = self.mod
        calls = self.start_and_stall_in_flight()
        for _ in range(2):
            self.assertTrue(self.admit_until("terminal"))
        st = self.status()  # deterministic: 1 wedged in-flight + 2 queued
        self.assertEqual(st["in_flight"], 1)
        self.assertEqual(st["queued"], 2)
        # Wedged writer: bounded flush reports processing-incomplete.
        self.assertFalse(m.sink_flush(0.3))
        self.stall.set()
        self.assertTrue(m.sink_flush(5.0))
        st = self.status()
        self.assertEqual(st["queued"], 0)
        self.assertEqual(st["in_flight"], 0)
        self.assertEqual(st["written"], 3)
        self.assertEqual(len(calls), 3)

    def test_flush_deadline_spans_lock_acquisition(self):
        m = self.mod
        self.start_and_stall_in_flight()
        holder, release = self.hold_sink_lock(hold_timeout=0.6)
        try:
            t0 = time.monotonic()
            # The deadline includes time spent acquiring the lock: flush
            # gives up within its 0.15s budget instead of waiting out the
            # 0.6s hold, and reports processing-incomplete.
            self.assertFalse(m.sink_flush(0.15))
            self.assertLess(time.monotonic() - t0, 0.4)
        finally:
            release.set()
            holder.join(5.0)
            self.assertFalse(holder.is_alive())

    def test_invalid_timeouts_rejected_without_state_change(self):
        m = self.mod
        bad = (-1, -0.001, float("nan"), float("inf"), 1e300, "5",
               None, True, 61)
        for value in bad:
            self.assertFalse(m.sink_flush(value), repr(value))
            self.assertFalse(m.sink_shutdown(value), repr(value))
        self.assertFalse(self.status()["stopping"],
                         "invalid shutdown must not stop admission")
        # Valid input still works afterwards.
        self.assertTrue(m.sink_start())
        self.assertTrue(self.admit_until("terminal"))
        self.assertTrue(m.sink_flush(5.0))

    # -- 12-14. single generation and startup failures ---------------------

    def test_single_generation_stuck_writer_never_replaced(self):
        m = self.mod
        self.start_and_stall_in_flight()
        first = m._sink_worker
        self.assertTrue(first.daemon)
        # Repeated starts never spawn or replace the worker.
        self.assertTrue(m.sink_start())
        self.assertIs(m._sink_worker, first)
        self.assertEqual(sink_thread_count(), 1)
        # The first job is active (proven by the spy-entered event);
        # exactly two pending jobs now sit behind it.
        for _ in range(2):
            self.assertTrue(self.admit_until("terminal"))
        st = self.status()
        self.assertEqual(st["admitted"], 3)
        self.assertEqual(st["queued"], 2)
        self.assertEqual(st["in_flight"], 1)
        # Stuck writer: bounded shutdown fails truthfully within ~its
        # 0.2s budget (not seconds).
        t0 = time.monotonic()
        self.assertFalse(m.sink_shutdown(0.2))
        self.assertLess(time.monotonic() - t0, 0.5)
        st = self.status()
        self.assertTrue(st["stopping"])
        self.assertTrue(st["worker_alive"])
        self.assertEqual(st["admitted"], 3)
        self.assertEqual(st["in_flight"], 1)
        self.assertGreaterEqual(st["queued"], 0)
        self.assertLessEqual(st["queued"], 2)
        self.assertGreaterEqual(st["discarded"], 0)
        self.assertLessEqual(st["discarded"], 2)
        self.assertEqual(st["queued"] + st["discarded"], 2)
        # No replacement while stopping: repeated start refuses.
        self.assertFalse(m.sink_start())
        self.assertIs(m._sink_worker, first)
        self.assertEqual(sink_thread_count(), 1)
        # Release the writer: bounded shutdown completes, worker ends.
        self.stall.set()
        self.assertTrue(m.sink_shutdown(5.0))
        st = self.status()
        self.assertEqual(st["admitted"], 3)
        self.assertEqual(st["queued"], 0)
        self.assertEqual(st["in_flight"], 0)
        self.assertEqual(
            st["written"] + st["failed"] + st["discarded"],
            st["admitted"])
        self.assertFalse(st["worker_alive"])
        self.assertFalse(first.is_alive())
        self.assertEqual(sink_thread_count(), 0)

    def test_startup_constructor_failure_latched_without_propagation(self):
        m = self.mod
        real_threading = m.threading
        constructed = []

        class BoomThread:
            def __init__(self, *a, **k):
                constructed.append(a)
                raise RuntimeError("thread constructor boom")

        m.threading = self._failing_thread_shim(real_threading, BoomThread)
        try:
            # One latched attempt (spec D5): the fixed core reports False
            # and never lets the constructor failure escape sink_start.
            self.assertIs(m.sink_start(), False)
            self.assertEqual(len(constructed), 1)
            # Canonical logger still works offline; the async sink drop
            # is counted, never admitted.
            self.assertIsNone(m.log_decision("VETO", "terminal"))
            st = self.status()
            self.assert_status_shape(st)
            self.assertFalse(st["started"])
            self.assertFalse(st["worker_alive"])
            self.assertEqual(st["admitted"], 0)
            self.assertGreaterEqual(st["dropped"], 1)
            self.assertEqual(sink_thread_count(), 0)
            self.assertFalse(m._sink_lock.locked(),
                             "constructor failure must not wedge the lock")
            # Latched for the generation: a repeated attempt neither
            # constructs again nor succeeds once threading is restored.
            self.assertIs(m.sink_start(), False)
            self.assertEqual(len(constructed), 1)
            m.threading = real_threading
            self.assertIs(m.sink_start(), False)
            self.assertEqual(len(constructed), 1)
            st = self.status()
            self.assertFalse(st["started"])
            self.assertFalse(st["worker_alive"])
            self.assertEqual(sink_thread_count(), 0)
        finally:
            m.threading = real_threading

    def test_startup_start_failure_latched_single_attempt(self):
        m = self.mod
        real_threading = m.threading
        constructed = []

        class StartBoomThread:
            def __init__(self, *a, **k):
                constructed.append(a)

            def start(self):
                raise RuntimeError("thread start boom")

            def is_alive(self):
                return False

        m.threading = self._failing_thread_shim(real_threading,
                                                StartBoomThread)
        try:
            # Reported as False, module consistent, no propagation.
            self.assertIs(m.sink_start(), False)
            self.assertEqual(len(constructed), 1)
            self.assertIsNone(m.log_decision("VETO", "terminal"))
            st = self.status()
            self.assert_status_shape(st)
            self.assertFalse(st["started"])
            self.assertFalse(st["worker_alive"])
            self.assertEqual(st["admitted"], 0)
            self.assertGreaterEqual(st["dropped"], 1)
            self.assertEqual(sink_thread_count(), 0)
            self.assertFalse(m._sink_lock.locked(),
                             "start failure must not wedge the lock")
            # Latched for the generation: no second attempt, with the
            # shim or with real threading (the fake is never joinable,
            # so teardown joins nothing here).
            self.assertIs(m.sink_start(), False)
            self.assertEqual(len(constructed), 1)
            m.threading = real_threading
            self.assertIs(m.sink_start(), False)
            self.assertEqual(len(constructed), 1)
            st = self.status()
            self.assertFalse(st["started"])
            self.assertFalse(st["worker_alive"])
            self.assertEqual(sink_thread_count(), 0)
        finally:
            m.threading = real_threading

    # -- 15-16. status surface and counter bound ---------------------------

    def test_status_fixed_nonsecret_counters(self):
        m = self.mod
        keysets = []

        def observe():
            st = self.status()
            self.assert_status_shape(st)
            keysets.append(frozenset(st))
            return st

        st = observe()  # before start
        self.assertFalse(st["started"])
        self.assertTrue(m.sink_start())
        st = observe()  # after start
        self.assertTrue(st["worker_alive"])
        for tool in ("terminal", "execute_code"):
            self.assertTrue(self.admit_until(tool))
        observe()  # with work in the system (values intentionally not
        # asserted here: draining is asynchronous without a stall)
        self.assertTrue(m.sink_flush(5.0))
        st = observe()  # after flush
        self.assertEqual(st["queue_max"], SINK_QUEUE_MAX_PINNED)
        self.assertEqual(st["admitted"], 2)
        self.assertEqual(st["written"], 2)
        self.assertEqual(st["failed"], 0)
        self.assertEqual(st["queued"], 0)
        self.assertEqual(st["in_flight"], 0)
        self.assertTrue(st["started"])
        self.assertFalse(st["stopping"])
        self.assertTrue(st["worker_alive"])
        # Fixed schema: identical key set across every operation above.
        self.assertEqual(len(set(keysets)), 1,
                         "status keys changed across operations")
        # No secret values: neither the state-home path nor any record
        # material appears on the status surface.
        dump = json.dumps({"sink_status": st})
        self.assertNotIn(self.state.name, dump)
        self.assertNotIn("VETO", dump)
        self.assertNotIn("LGV-001", dump)

    def test_drop_counter_saturates_at_bound_without_wrap(self):
        # A counter parked at the published bound must saturate, not
        # wrap or exceed it, when one more drop arrives (the original
        # core's unbounded increment is already proven red upstream).
        m = self.mod  # never started: every admission attempt is a drop
        self.assertTrue(
            self._seed_counter("dropped", COUNTER_MAX),
            "no mutable dropped-counter store found (status-keyed dict "
            "or _sink_dropped attribute); cannot seed saturation bound")
        self.assertEqual(self.status()["dropped"], COUNTER_MAX,
                         "seeded store is not reflected in sink_status")
        self.assertFalse(m._sink_admit(*VALID_PAIR, "terminal"))  # 1 attempt
        st = self.status()
        self.assert_status_shape(st)
        self.assertEqual(st["dropped"], COUNTER_MAX,
                         "dropped exceeded/wrapped the (1<<63)-1 bound")


if __name__ == "__main__":
    unittest.main()
