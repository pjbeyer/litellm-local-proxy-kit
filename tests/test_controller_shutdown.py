"""Shared shutdown latch/deadline primitive — offline simulated-clock fixtures.

No real signals, credentials, network, engine/runtime dependency or live
launchd/LiteLLM contact. Reentry and concurrency are exercised through
simulated in-handler calls and threads, never delivered OS signals.
"""

import importlib.util
import math
import threading
import time
from types import SimpleNamespace

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
    from core.restart_control import shutdown as module
    for name, value in (("SHUTDOWN_DEADLINE_SECONDS", 5),
                        ("SUPERVISOR_EXIT_SECONDS", 500),
                        ("OSLOG_RATE_LIMIT", "9999"),
                        ("SHUTDOWN_VERSION", 7)):
        monkeypatch.setenv(name, str(value))
    import importlib
    importlib.reload(module)
    assert module.SHUTDOWN_CONTRACT["version"] == 1
    assert module.SHUTDOWN_CONTRACT["supervisor_exit_seconds"] == 60
    assert module.SHUTDOWN_CONTRACT["shutdown_deadline_seconds"] == 50
    assert module.SHUTDOWN_CONTRACT["logging_environment"] == {"OSLogRateLimit": "64"}
    monkeypatch.delenv("SHUTDOWN_DEADLINE_SECONDS")
    monkeypatch.delenv("SUPERVISOR_EXIT_SECONDS")
    monkeypatch.delenv("OSLOG_RATE_LIMIT")
    monkeypatch.delenv("SHUTDOWN_VERSION")
    importlib.reload(module)


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


def test_commit_is_deferred_to_t002():
    from core.restart_control.shutdown import Shutdown
    latch = Shutdown(clock=lambda: 0.0)
    assert not hasattr(latch, "commit")
