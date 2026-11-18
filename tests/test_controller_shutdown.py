"""Shared shutdown latch/deadline primitive — offline simulated-clock fixtures.

No real signals, credentials, network, engine/runtime dependency or live
launchd/LiteLLM contact. Reentry and concurrency are exercised through
simulated in-handler calls and threads, never delivered OS signals.
"""

import importlib.util
import dataclasses
import math
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import weakref

import pytest

assert importlib.util.find_spec("core.restart_control.shutdown") is not None, \
    "shutdown API missing"


def shutdown_module():
    from core.restart_control import shutdown
    return shutdown


def bounded_wait(latch, interval, **kwargs):
    """Run a potentially blocking wait on a disposable thread."""
    results = []
    errors = []

    def run():
        try:
            results.append(latch.wait(interval, **kwargs))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(0.75)
    assert not worker.is_alive(), "wait exceeded the test timeout"
    if errors:
        raise errors[0]
    return results[0]


def test_constructor_rejects_unsupported_runtime(monkeypatch):
    from core.restart_control import shutdown as module
    monkeypatch.setattr(module, "sys", SimpleNamespace(
        implementation=SimpleNamespace(name="pypy")), raising=False)
    with pytest.raises(ValueError, match="^runtime_unsupported$"):
        module.Shutdown(clock=lambda: 0.0)


def test_first_shutdown_deadline_cannot_extend():
    from core.restart_control.shutdown import Shutdown
    now = [100.0]
    shutdown = Shutdown(clock=lambda: now[0])
    shutdown.request()
    first = shutdown.deadline
    assert first == 150.0
    now[0] = 120.0
    shutdown.request()
    assert shutdown.deadline == first


def test_requested_state_after_first_request():
    from core.restart_control.shutdown import Shutdown
    latch = Shutdown(clock=lambda: 0.0)
    assert latch.requested is False
    assert latch.deadline is None
    latch.request()
    assert latch.requested is True
    assert latch.deadline == 50.0


def test_contract_is_immutable_snapshot():
    from core.restart_control.shutdown import SHUTDOWN_CONTRACT
    assert SHUTDOWN_CONTRACT == {"version": 1, "supervisor_exit_seconds": 60,
                                 "shutdown_deadline_seconds": 50,
                                 "logging_environment": {"OSLogRateLimit": "64"}}
    snapshot = SHUTDOWN_CONTRACT.snapshot()
    assert snapshot == {"version": 1, "supervisor_exit_seconds": 60,
                        "shutdown_deadline_seconds": 50,
                        "logging_environment": {"OSLogRateLimit": "64"}}
    assert isinstance(snapshot, dict)
    nested = snapshot["logging_environment"]
    assert isinstance(nested, dict)
    import copy
    with pytest.raises(TypeError):
        SHUTDOWN_CONTRACT["version"] = 2
    with pytest.raises(TypeError):
        SHUTDOWN_CONTRACT["logging_environment"]["OSLogRateLimit"] = "128"
    with pytest.raises(TypeError):
        snapshot["version"] = 2
    with pytest.raises(TypeError):
        nested["OSLogRateLimit"] = "128"
    assert copy.deepcopy(snapshot) == snapshot


def test_production_constants_are_fixed_with_no_env_override(monkeypatch):
    import importlib.util
    import sys as _sys
    from core.restart_control import shutdown as module
    # Reload an ISOLATED copy under a private module name: a real reload would
    # replace the shared module's class objects in place, breaking every later
    # isinstance-based lifecycle admission in the same process.
    probe_name = "core.restart_control._shutdown_contract_probe"
    spec = importlib.util.spec_from_file_location(probe_name, module.__file__)
    assert spec is not None and spec.loader is not None

    def isolated_reload():
        isolated = importlib.util.module_from_spec(spec)
        loader = spec.loader
        assert loader is not None
        with monkeypatch.context() as patch:
            patch.setitem(_sys.modules, probe_name, isolated)
            loader.exec_module(isolated)
        return isolated

    for name, value in (("SHUTDOWN_DEADLINE_SECONDS", 5),
                        ("SUPERVISOR_EXIT_SECONDS", 500),
                        ("OSLOG_RATE_LIMIT", "9999"),
                        ("SHUTDOWN_VERSION", 7)):
        monkeypatch.setenv(name, str(value))
    probe = isolated_reload()
    assert probe.SHUTDOWN_CONTRACT["version"] == 1
    assert probe.SHUTDOWN_CONTRACT["supervisor_exit_seconds"] == 60
    assert probe.SHUTDOWN_CONTRACT["shutdown_deadline_seconds"] == 50
    assert probe.SHUTDOWN_CONTRACT["logging_environment"] == {"OSLogRateLimit": "64"}
    monkeypatch.delenv("SHUTDOWN_DEADLINE_SECONDS")
    monkeypatch.delenv("SUPERVISOR_EXIT_SECONDS")
    monkeypatch.delenv("OSLOG_RATE_LIMIT")
    monkeypatch.delenv("SHUTDOWN_VERSION")
    probe = isolated_reload()
    assert probe.SHUTDOWN_CONTRACT["version"] == 1


def test_checkpoint_admits_before_latch_and_refuses_after():
    from core.restart_control.shutdown import Shutdown, ShutdownRequested
    now = [10.0]
    latch = Shutdown(clock=lambda: now[0])
    assert latch.checkpoint() is None
    latch.request()
    now[0] = 30.0
    with pytest.raises(ShutdownRequested, match="controller_stopping"):
        latch.checkpoint()
    now[0] = 45.0
    with pytest.raises(ShutdownRequested, match="controller_stopping"):
        latch.checkpoint()
    assert latch.deadline == 60.0


def test_remaining_normal_cleanup_and_refusal_budget():
    from core.restart_control.shutdown import Shutdown, ShutdownRequested
    now = [0.0]
    latch = Shutdown(clock=lambda: now[0])
    # Before the latch the normal budget is unconstrained by shutdown.
    assert latch.remaining(5) == 5.0
    latch.request()
    # Normal action admission after the latch fails closed.
    with pytest.raises(ShutdownRequested):
        latch.remaining(10)
    now[0] = 45.0
    # Cleanup shares the remaining budget, capped by the limit...
    assert latch.remaining(10, cleanup=True) == 5.0
    # ...and refuses with 0.0 once the shared deadline has passed.
    now[0] = 55.0
    assert latch.remaining(10, cleanup=True) == 0.0
    now[0] = 65.0
    assert latch.remaining(10, cleanup=True) == 0.0


def test_wait_wakes_on_latch_and_cleanup_uses_interval():
    from core.restart_control.shutdown import Shutdown, ShutdownRequested
    now = [0.0]
    latch = Shutdown(clock=lambda: now[0])
    start = time.monotonic()
    assert bounded_wait(latch, 0.03) is False
    assert time.monotonic() - start >= 0.02
    timer = threading.Timer(0.01, latch.request)
    timer.daemon = True
    timer.start()
    assert bounded_wait(latch, 0.3) is True
    timer.join(0.75)
    assert not timer.is_alive(), "shutdown timer did not finish"
    with pytest.raises(ShutdownRequested):
        latch.wait(0.01)
    start = time.monotonic()
    assert bounded_wait(latch, 0.03, cleanup=True) is False
    elapsed = time.monotonic() - start
    assert 0.02 <= elapsed < 0.3
    now[0] = 50.0
    assert bounded_wait(latch, 0.03, cleanup=True) is True


def test_cleanup_wait_before_latch_rechecks_on_new_shutdown():
    from core.restart_control.shutdown import Shutdown
    now = [0.0]
    latch = Shutdown(clock=lambda: now[0])
    timer = threading.Timer(0.01, latch.request)
    timer.daemon = True
    timer.start()
    start = time.monotonic()
    assert bounded_wait(latch, 0.04, cleanup=True) is False
    timer.join(0.75)
    assert not timer.is_alive(), "shutdown timer did not finish"
    assert latch.requested
    assert time.monotonic() - start >= 0.025


def test_frozen_clock_cleanup_does_not_spin(monkeypatch):
    from core.restart_control import shutdown as module
    latch = module.Shutdown(clock=lambda: 0.0)
    latch.request()
    calls = [0]
    original_sleep = module.time.sleep
    original_event_wait = latch._wakeup.wait

    def counted_sleep(seconds):
        calls[0] += 1
        original_sleep(seconds)

    def counted_event_wait(seconds):
        calls[0] += 1
        return original_event_wait(seconds)

    monkeypatch.setattr(module.time, "sleep", counted_sleep)
    monkeypatch.setattr(latch._wakeup, "wait", counted_event_wait)
    start = time.monotonic()
    assert bounded_wait(latch, 0.04, cleanup=True) is False
    elapsed = time.monotonic() - start
    assert 0.025 <= elapsed < 0.3
    assert 1 <= calls[0] < 20


def test_sequential_requests_cannot_extend_deadline():
    from core.restart_control.shutdown import Shutdown
    now = [100.0]
    outer = []
    latch = Shutdown(clock=lambda: now[0])
    calls = []

    def handler():
        calls.append("first")
        now[0] = 110.0
        latch.request()  # a later request after the first call returned
        calls.append("second")
        now[0] = 130.0
        latch.request()

    outer.append(handler)
    latch.request()
    handler()
    assert latch.deadline == 150.0
    assert latch.requested is True
    now[0] = 140.0
    latch.request()
    assert latch.deadline == 150.0
    assert calls == ["first", "second"]


def test_request_reentry_at_publication_keeps_first_deadline():
    from core.restart_control.shutdown import Shutdown
    now = [100.0]

    class PublicationProbe(dict):
        def setdefault(self, key, value):
            result = super().setdefault(key, value)
            now[0] = 110.0
            latch.request()  # signal immediately after the atomic insert
            return result

    latch = Shutdown(clock=lambda: now[0])
    latch._request_state = PublicationProbe()
    latch.request()
    assert latch.requested is True
    assert latch.deadline == 150.0


def test_request_reentry_while_wakeup_condition_held_is_bounded():
    from core.restart_control.shutdown import Shutdown
    latch = Shutdown(clock=lambda: 0.0)
    errors = []

    def request_with_condition_held():
        try:
            condition = getattr(latch._wakeup, "_condition", None)
            if condition is None:
                condition = latch._wakeup._cond
            with condition:
                latch.request()
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=request_with_condition_held, daemon=True)
    worker.start()
    worker.join(0.3)
    assert not worker.is_alive(), "request deadlocked inside wakeup condition"
    assert errors == []
    assert latch.deadline == 50.0


def test_same_thread_request_while_state_lock_held_is_bounded():
    from core.restart_control.shutdown import Shutdown
    latch = Shutdown(clock=lambda: -10.0)
    results = []

    def nested_handler():
        with latch._lock:
            latch.request()
            results.append((latch.requested, latch.deadline))
            latch.request()

    worker = threading.Thread(target=nested_handler, daemon=True)
    worker.start()
    worker.join(0.3)
    assert not worker.is_alive(), "same-thread reentry deadlocked"
    assert results == [(True, 40.0)]


def test_reentry_during_wakeup_preserves_coherent_state(monkeypatch):
    from core.restart_control.shutdown import Shutdown
    latch = Shutdown(clock=lambda: 2.0)
    observed = []
    original_set = latch._wakeup.set

    def nested_set():
        observed.append((latch.requested, latch.deadline))
        latch.request()
        original_set()

    monkeypatch.setattr(latch._wakeup, "set", nested_set)
    latch.request()
    assert observed == [(True, 52.0)]


def test_concurrent_first_request_fixes_one_deadline():
    from core.restart_control.shutdown import Shutdown
    # One gate serializes clock reads: whichever request reads the clock
    # first wins the latch, and every later concurrent request (even one
    # that would compute a later deadline) can never extend it.
    now = [100.0]
    lock = threading.Lock()

    def requester(delta):
        try:
            barrier.wait(timeout=0.75)
            if delta is not None:
                with lock:
                    now[0] += delta
            latch.request()
        except Exception as exc:  # pragma: no cover - only on defects
            errors.append(exc)

    reads = []
    latch_holder = []

    def recording_clock():
        with lock:
            # Only count reads once the latch exists: __init__ probes the
            # clock once for fail-closed validation, which is not a request.
            if latch_holder:
                reads.append(now[0])
            return now[0]

    latch = Shutdown(clock=recording_clock)
    latch_holder.append(latch)
    barrier = threading.Barrier(9)
    errors = []

    threads = [threading.Thread(target=requester, args=(i,), daemon=True)
               for i in range(9)]
    for thread in threads:
        thread.start()
    join_deadline = time.monotonic() + 0.75
    for thread in threads:
        thread.join(max(0, join_deadline - time.monotonic()))
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert latch.requested is True
    assert reads, "clock was never read"
    first = latch.deadline
    assert first in {sample + 50.0 for sample in reads}
    now[0] += 100
    latch.request()
    assert latch.deadline == first


def test_negative_clock_origin_and_backwards_clock_fail_closed():
    from core.restart_control.shutdown import Shutdown
    now = [-100.0]
    latch = Shutdown(clock=lambda: now[0])
    assert latch.checkpoint() is None
    latch.request()
    assert latch.deadline == -50.0
    now[0] = -90.0
    assert latch.remaining(100, cleanup=True) == 40.0
    now[0] = -95.0
    with pytest.raises(ValueError, match="clock_invalid"):
        latch.remaining(100, cleanup=True)
    now[0] = -80.0
    with pytest.raises(ValueError, match="clock_invalid"):
        latch.remaining(100, cleanup=True)


def test_clock_corruption_before_latch_cannot_reopen_normal_admission():
    from core.restart_control.shutdown import Shutdown
    now = [10.0]
    latch = Shutdown(clock=lambda: now[0])
    now[0] = 9.0
    with pytest.raises(ValueError, match="clock_invalid"):
        latch.checkpoint()
    now[0] = 11.0
    with pytest.raises(ValueError, match="clock_invalid"):
        latch.checkpoint()


@pytest.mark.parametrize("method", ["checkpoint", "remaining"])
def test_clock_callback_reentry_closes_normal_admission(method):
    from core.restart_control.shutdown import Shutdown, ShutdownRequested
    holder = []
    fired = [False]

    def clock():
        if holder and not fired[0]:
            fired[0] = True
            holder[0].request()
        return 0.0

    latch = Shutdown(clock=clock)
    holder.append(latch)
    with pytest.raises(ShutdownRequested, match="controller_stopping"):
        if method == "checkpoint":
            latch.checkpoint()
        else:
            latch.remaining(1)
    assert latch.deadline == 50.0


def test_invalid_clock_and_values_fail_closed():
    from core.restart_control.shutdown import Shutdown, ShutdownRequested
    with pytest.raises(ValueError):
        Shutdown(clock=lambda: True)
    with pytest.raises(ValueError):
        Shutdown(clock=lambda: math.nan)
    with pytest.raises(ValueError):
        Shutdown(clock=lambda: math.inf)
    latch = Shutdown(clock=lambda: 0.0)
    latch.request()
    # Normal admission after the latch fails closed...
    with pytest.raises(ShutdownRequested):
        latch.remaining(5)
    with pytest.raises(ShutdownRequested):
        latch.wait(1.0)
    # ...while cleanup keeps using the remaining shared budget.
    assert latch.remaining(10, cleanup=True) == 10.0


def test_remaining_limit_validation_fails_closed():
    from core.restart_control.shutdown import Shutdown
    latch = Shutdown(clock=lambda: 0.0)
    latch.request()
    for bad in (True, -1.0, math.nan, math.inf):
        with pytest.raises(ValueError):
            latch.remaining(bad, cleanup=True)
    with pytest.raises(ValueError):
        latch.wait(-1.0)
    with pytest.raises(ValueError):
        latch.wait(math.inf)
    for bad in (1, None, "yes"):
        with pytest.raises(ValueError, match="cleanup_invalid"):
            latch.remaining(1, cleanup=bad)
        with pytest.raises(ValueError, match="cleanup_invalid"):
            latch.wait(0.01, cleanup=bad)
    for bad in (True, 0, math.nan, -math.inf):
        with pytest.raises(ValueError, match="interval_invalid"):
            latch.wait(bad)


def test_commitment_permit_is_bound_immutable_and_one_use():
    from core.restart_control.shutdown import Shutdown
    latch = Shutdown(clock=lambda: 0.0)
    assert callable(getattr(latch, "commit", None)), "commit API missing"
    permit = latch.commit("candidate-create")
    assert permit.purpose == "candidate-create"
    with pytest.raises((AttributeError, TypeError)):
        permit.purpose = "primary-stop"
    with pytest.raises((AttributeError, TypeError)):
        permit.extra = "action"
    other = Shutdown(clock=lambda: 0.0)
    with pytest.raises(ValueError, match="permit_invalid"):
        other.claim(permit)
    assert latch.claim(permit) == "candidate-create"
    with pytest.raises(ValueError, match="permit_consumed"):
        latch.claim(permit)


def test_permit_import_and_claim_with_python310_dataclass_signature(monkeypatch):
    """The isolated decorator shim accepts exactly Python 3.10 keywords."""
    from core.restart_control import shutdown as current_module

    real_dataclass = dataclasses.dataclass

    def python310_dataclass(cls=None, /, *, init=True, repr=True, eq=True,
                            order=False, unsafe_hash=False, frozen=False,
                            match_args=True, kw_only=False, slots=False):
        return real_dataclass(cls, init=init, repr=repr, eq=eq, order=order,
                              unsafe_hash=unsafe_hash, frozen=frozen,
                              match_args=match_args, kw_only=kw_only, slots=slots)

    name = "core.restart_control._shutdown_python310_probe"
    spec = importlib.util.spec_from_file_location(name, current_module.__file__)
    assert spec is not None and spec.loader is not None
    isolated = importlib.util.module_from_spec(spec)
    import_error = None
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, name, isolated)
        patch.setattr(dataclasses, "dataclass", python310_dataclass)
        try:
            spec.loader.exec_module(isolated)
        except TypeError as exc:
            import_error = exc
    assert import_error is None, f"Python 3.10 dataclass import rejected: {import_error}"

    latch = isolated.Shutdown(clock=lambda: 0.0)
    permit = latch.commit("primary-stop")
    assert weakref.ref(permit)() is permit
    latch.request()
    assert latch.claim(permit) == "primary-stop"
    with pytest.raises(ValueError, match="permit_consumed"):
        latch.claim(permit)


def test_request_during_permit_allocation_keeps_commitment_claimable(monkeypatch):
    from core.restart_control import shutdown as module
    latch = module.Shutdown(clock=lambda: 0.0)
    permit_type = module._ActionPermit

    def request_during_allocation(purpose, issuer):
        latch.request()  # immediately after commit's final shutdown-open read
        return permit_type(purpose, issuer)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_ActionPermit", request_during_allocation)
        permit = latch.commit("primary-stop")

    assert latch.requested
    assert latch.claim(permit) == "primary-stop"
    with pytest.raises(module.ShutdownRequested, match="controller_stopping"):
        latch.commit("primary-start")


def test_concurrent_claim_of_one_permit_has_exactly_one_winner():
    from core.restart_control.shutdown import Shutdown
    latch = Shutdown(clock=lambda: 0.0)
    permit = latch.commit("primary-stop")
    barrier = threading.Barrier(9)
    outcomes = []
    unexpected = []

    def claim_once():
        try:
            barrier.wait(timeout=0.75)
            outcomes.append(("success", latch.claim(permit)))
        except ValueError as exc:
            outcomes.append(("closed", str(exc)))
        except BaseException as exc:
            unexpected.append(exc)

    threads = [threading.Thread(target=claim_once, daemon=True) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=0.75)
    deadline = time.monotonic() + 0.75
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))

    assert all(not thread.is_alive() for thread in threads), "claim race did not finish"
    assert unexpected == []
    assert outcomes.count(("success", "primary-stop")) == 1
    assert outcomes.count(("closed", "permit_consumed")) == 7


def test_commitment_latch_first_refuses_without_action():
    from core.restart_control.shutdown import Shutdown, ShutdownRequested
    latch = Shutdown(clock=lambda: 0.0)
    actions = []
    latch.request()
    with pytest.raises(ShutdownRequested, match="controller_stopping"):
        permit = latch.commit("primary-stop")
        actions.append(latch.claim(permit))
    assert actions == []


def test_commitment_before_latch_allows_only_its_action_to_finish():
    from core.restart_control.shutdown import Shutdown, ShutdownRequested
    latch = Shutdown(clock=lambda: 0.0)
    committed = threading.Event()
    finish = threading.Event()
    actions = []
    errors = []

    def submit():
        try:
            permit = latch.commit("primary-stop")
            committed.set()
            assert finish.wait(0.75), "test submission release timed out"
            actions.append(latch.claim(permit))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=submit, daemon=True)
    worker.start()
    try:
        assert committed.wait(0.75), "commitment did not complete"
        latch.request()
        with pytest.raises(ShutdownRequested, match="controller_stopping"):
            latch.commit("primary-start")
    finally:
        finish.set()
        worker.join(0.75)
    assert not worker.is_alive(), "submission stand-in did not finish"
    assert errors == []
    assert actions == ["primary-stop"]


def test_commitment_request_does_not_wait_for_submission_gate():
    from core.restart_control.shutdown import Shutdown, ShutdownRequested
    latch = Shutdown(clock=lambda: 0.0)
    errors = []

    def stop():
        try:
            latch.request()
        except BaseException as exc:
            errors.append(exc)

    assert hasattr(latch, "_submission_gate"), "submission gate missing"
    with latch._submission_gate:
        worker = threading.Thread(target=stop, daemon=True)
        worker.start()
        worker.join(0.3)
        assert not worker.is_alive(), "request waited for submission gate"
    assert errors == []
    assert latch.requested
    with pytest.raises(ShutdownRequested):
        latch.commit("primary-stop")


def test_commitment_waiting_on_gate_refuses_when_latch_wins():
    from core.restart_control.shutdown import Shutdown, ShutdownRequested
    latch = Shutdown(clock=lambda: 0.0)
    entered = threading.Event()
    outcomes = []

    def attempt():
        entered.set()
        try:
            outcomes.append(latch.commit("primary-stop"))
        except ShutdownRequested:
            outcomes.append("stopped")

    with latch._submission_gate:
        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        assert entered.wait(0.75)
        latch.request()
    worker.join(0.75)
    assert not worker.is_alive(), "commitment did not resolve"
    assert outcomes == ["stopped"]


def test_commitment_rejects_nonprimitive_or_unbounded_purpose():
    from core.restart_control.shutdown import Shutdown
    latch = Shutdown(clock=lambda: 0.0)
    for bad in (None, True, 1, b"primary-stop", "", "A", "has space",
                "a/b", "a\ncommand", "a" * 65):
        with pytest.raises(ValueError, match="purpose_invalid"):
            latch.commit(bad)
    assert latch.commit("a" * 64).purpose == "a" * 64


def test_commitment_clock_reentry_cannot_admit_after_latch():
    from core.restart_control.shutdown import Shutdown, ShutdownRequested
    holder = []
    fired = [False]

    def clock():
        if holder and not fired[0]:
            fired[0] = True
            holder[0].request()
        return 0.0

    latch = Shutdown(clock=clock)
    holder.append(latch)
    with pytest.raises(ShutdownRequested, match="controller_stopping"):
        latch.commit("primary-stop")
    assert latch.requested


# -- T013: wire/CLI bounded drain over real Unix sockets ----------------------

import tempfile


class _LatchView:
    """Event shape exposing the shared shutdown budget like the CLI view."""

    def __init__(self, shutdown):
        self.shutdown = shutdown

    def is_set(self):
        return self.shutdown.requested

    def remaining(self, limit, *, cleanup):
        return self.shutdown.remaining(limit, cleanup=cleanup)


def _wait_bound(path, thread, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        assert thread.is_alive(), "server exited before binding"
        time.sleep(0.01)
    raise AssertionError("socket never bound")


def _serve_root():
    """Private 0700 parent like the real state dir; wire refuses looser.

    Created under TMPDIR like every other suite: the wire validator walks
    every ancestor and refuses symlinked paths, which pytest's default
    basetemp (/var -> /private/var) contains.
    """
    root = Path(tempfile.mkdtemp(prefix="t013-", dir=os.environ.get("TMPDIR")
                                 or tempfile.gettempdir()))/"state"
    root.mkdir(mode=0o700)
    return root


def test_wire_drain_joins_under_one_shared_budget(tmp_path):
    """Stuck workers share ONE deadline from the latch, not per-worker."""
    import socket
    from core.restart_control.shutdown import Shutdown
    from core.restart_control.wire import serve, client

    class StuckController:
        uid = os.getuid()

        def __init__(self):
            self.entered = threading.Event()

        def handle(self, request):
            self.entered.set()
            time.sleep(60)  # stuck far beyond any drain budget

    controller = StuckController()
    now = [0.0]
    shutdown = Shutdown(clock=lambda: now[0])
    view = _LatchView(shutdown)
    path = _serve_root()/"controller.sock"
    thread = threading.Thread(target=serve, args=(path, controller, view), daemon=True)
    thread.start()
    _wait_bound(path, thread)
    try:
        client(path, {"op": "status"}, timeout=5)
    except Exception:
        pass
    assert controller.entered.wait(5)
    # Latch, then run the shared clock past the fixed deadline so the
    # drain's cleanup budget is exhausted before it starts.
    shutdown.request()
    now[0] = 60.0  # past the 50s deadline fixed at request time
    started = time.monotonic()
    thread.join(10)
    elapsed = time.monotonic() - started
    # The stuck worker never finishes; the drain must give up at the
    # exhausted shared budget, not wait 50s per worker or forever.
    assert not thread.is_alive(), "serve did not return after shared budget"
    assert elapsed < 5, f"drain waited {elapsed:.1f}s; budget was exhausted"
    assert not path.exists(), "socket not unlinked after drain"


def test_wire_drain_uses_fixed_budget_without_latch_view(tmp_path):
    """A plain Event stop source falls back to the fixed contract budget."""
    from core.restart_control.wire import serve, client

    class SlowController:
        uid = os.getuid()

        def handle(self, request):
            time.sleep(1.5)
            return {"status": "ok"}

    controller = SlowController()
    stop = threading.Event()
    path = _serve_root()/"controller.sock"
    thread = threading.Thread(target=serve, args=(path, controller, stop), daemon=True)
    thread.start()
    _wait_bound(path, thread)
    client(path, {"op": "status"}, timeout=5)
    stop.set()
    started = time.monotonic()
    thread.join(30)
    elapsed = time.monotonic() - started
    assert not thread.is_alive()
    # The slow worker finishes inside the 50s fixed budget; the drain waits
    # for it (join is bounded) and the socket is cleaned up.
    assert elapsed < 10
    assert not path.exists()


def test_wire_drain_repeated_signals_and_lost_clients(tmp_path):
    """Repeated shutdown requests, queued connections and lost clients."""
    import socket
    from core.restart_control.shutdown import Shutdown
    from core.restart_control.wire import serve

    class Controller:
        uid = os.getuid()

        def handle(self, request):
            return {"status": "ok"}

    controller = Controller()
    shutdown = Shutdown()
    view = _LatchView(shutdown)
    path = _serve_root()/"controller.sock"
    thread = threading.Thread(target=serve, args=(path, controller, view), daemon=True)
    thread.start()
    _wait_bound(path, thread)
    # Lost client: connect and vanish without sending anything.
    lost = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lost.connect(str(path))
    lost.close()
    # A queued connection the listener never accepted stays in the backlog.
    queued = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    queued.connect(str(path))
    queued.sendall(b'{"op":"status"}\n')
    queued.recv(1)  # server may answer; irrelevant to the drain
    queued.close()
    # Repeated signals: the first fixes the deadline, later ones no-op.
    for _ in range(5):
        shutdown.request()
    thread.join(30)
    assert not thread.is_alive(), "repeated signals stalled the drain"
    assert not path.exists(), "socket not cleaned after repeated signals"


def test_wire_drain_inodesafe_cleanup_never_unlinks_replacement(tmp_path):
    """A replaced socket file at the same path is never unlinked."""
    from core.restart_control.shutdown import Shutdown
    from core.restart_control.wire import serve

    class Controller:
        uid = os.getuid()

        def handle(self, request):
            return {"status": "ok"}

    controller = Controller()
    view = _LatchView(Shutdown())
    path = _serve_root()/"controller.sock"
    thread = threading.Thread(target=serve, args=(path, controller, view), daemon=True)
    thread.start()
    _wait_bound(path, thread)
    bound_inode = path.lstat().st_ino
    # Replace the socket with a different file before the drain cleans up.
    path.unlink()
    replacement = path
    replacement.write_text("replacement")
    view.shutdown.request()
    thread.join(30)
    assert not thread.is_alive()
    # The replacement survived: only the exact bound inode is ever unlinked.
    assert replacement.read_text() == "replacement"
    assert replacement.lstat().st_ino != bound_inode


def test_cli_view_exposes_shared_budget():
    """The CLI's ShutdownView forwards remaining() to the shared latch."""
    from core.restart_controller import ShutdownView
    from core.restart_control.shutdown import Shutdown

    latch = Shutdown(clock=lambda: 0.0)
    view = ShutdownView(latch)
    assert view.is_set() is False
    assert view.remaining(50, cleanup=True) == 50.0
    latch.request()
    assert view.is_set() is True
    # After the latch the cleanup budget is the remaining shared deadline
    # (50s contract from t=0 means the deadline is 50.0; clock is frozen).
    assert view.remaining(50, cleanup=True) == 50.0
    with pytest.raises(Exception):
        view.remaining(50, cleanup=False)  # normal admission fails closed
