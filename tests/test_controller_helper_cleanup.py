import os

import pytest

from core.restart_control import engine, runtime as module
from core.restart_control.shutdown import Shutdown
from test_controller_runtime_shutdown import (
    helper_runtime as attached_runtime,
    helper_native_guards,
)


LEADER = {
    "pid": 424242,
    "uid": os.getuid(),
    "ppid": 424240,
    "pgid": 424242,
    "birth": [12345, 678],
}
JOURNAL_IDENTITY = {
    "pid": LEADER["pid"],
    "uid": LEADER["uid"],
    "pgid": LEADER["pgid"],
    "birth": list(LEADER["birth"]),
}
LEGACY_IDENTITY = {
    "pid": LEADER["pid"],
    "uid": LEADER["uid"],
    "birth": list(LEADER["birth"]),
}


@pytest.fixture
def cleanup_runtime(attached_runtime):
    return attached_runtime[0]


class FakeChild:
    def __init__(self, pid=LEADER["pid"], exited=False):
        self.pid = pid
        self.exited = exited
        self.wait_timeouts = []

    def poll(self):
        return 3 if self.exited else None

    def wait(self, timeout=None):
        assert timeout is not None
        assert 0 <= timeout <= 20
        self.wait_timeouts.append(timeout)
        if not self.exited:
            raise TimeoutError("fixture child is still running")
        return 3


def _kernel(**changes):
    fact = dict(LEADER)
    fact["birth"] = list(LEADER["birth"])
    fact.update(changes)
    return fact


def _legacy_projection(fact):
    return {
        "pid": fact["pid"],
        "uid": fact["uid"],
        "birth": list(fact["birth"]),
    }


def _install_safe_kernel(monkeypatch, fact, census):
    monkeypatch.setattr(module, "process_identity", lambda pid: dict(fact), raising=False)
    monkeypatch.setattr(module, "_identity", lambda pid: _legacy_projection(fact), raising=False)
    monkeypatch.setattr(os, "getpgid", lambda pid: fact["pgid"])

    def group_census(pgid):
        assert pgid == LEADER["pid"]
        return tuple(dict(item) for item in census())

    monkeypatch.setattr(module, "process_group_identities", group_census, raising=False)


def _prepare(runtime, child, expected):
    runtime._cleanup_blocked = False


def _owned_stop(runtime, child, expected):
    return runtime._owned_stop(child, expected)


def _assert_failed_closed(runtime, error):
    assert runtime._cleanup_blocked is True
    assert error.code == "candidate_cleanup_failed"


def _install_killpg(monkeypatch, callback):
    calls = []

    def recording_killpg(pgid, sig):
        calls.append((pgid, sig))
        assert pgid == LEADER["pid"]
        assert sig == module.signal.SIGTERM
        return callback(pgid, sig)

    monkeypatch.setattr(os, "killpg", recording_killpg)
    return calls


def test_owned_stop_exited_child_with_empty_group_reaps_and_succeeds(
    cleanup_runtime, monkeypatch, helper_native_guards
):
    child = FakeChild(exited=True)
    _prepare(cleanup_runtime, child, JOURNAL_IDENTITY)
    _install_safe_kernel(monkeypatch, _kernel(), lambda: ())
    signals = _install_killpg(monkeypatch, lambda pgid, sig: None)

    _owned_stop(cleanup_runtime, child, JOURNAL_IDENTITY)

    assert child.wait_timeouts
    assert all(0 <= timeout <= 20 for timeout in child.wait_timeouts)
    assert signals == []
    assert cleanup_runtime._cleanup_blocked is False


def test_owned_stop_exited_leader_with_surviving_group_fails_closed(
    cleanup_runtime, monkeypatch, helper_native_guards
):
    child = FakeChild(exited=True)
    descendant = _kernel(
        pid=424243,
        ppid=LEADER["pid"],
        birth=[12346, 1],
    )
    _prepare(cleanup_runtime, child, JOURNAL_IDENTITY)
    _install_safe_kernel(monkeypatch, _kernel(), lambda: (descendant,))
    signals = _install_killpg(monkeypatch, lambda pgid, sig: None)

    with pytest.raises(engine.Fault) as caught:
        _owned_stop(cleanup_runtime, child, JOURNAL_IDENTITY)

    _assert_failed_closed(cleanup_runtime, caught.value)
    assert signals == []
    assert child.wait_timeouts == []


@pytest.mark.parametrize(
    "actual",
    [
        _kernel(uid=LEADER["uid"] + 1),
        _kernel(birth=[99999, 1]),
        _kernel(pgid=LEADER["pid"] + 1),
    ],
)
def test_owned_stop_live_leader_diverged_identity_fails_closed(
    cleanup_runtime, monkeypatch, helper_native_guards, actual
):
    child = FakeChild()
    _prepare(cleanup_runtime, child, LEGACY_IDENTITY)
    _install_safe_kernel(monkeypatch, actual, lambda: (actual,))
    signals = _install_killpg(monkeypatch, lambda pgid, sig: None)

    with pytest.raises(engine.Fault) as caught:
        _owned_stop(cleanup_runtime, child, LEGACY_IDENTITY)

    _assert_failed_closed(cleanup_runtime, caught.value)
    assert signals == []
    assert child.wait_timeouts == []


def test_owned_stop_live_leader_with_foreign_group_member_fails_closed(
    cleanup_runtime, monkeypatch, helper_native_guards
):
    child = FakeChild()
    foreign = _kernel(pid=424243, ppid=LEADER["pid"], uid=LEADER["uid"] + 1)
    _prepare(cleanup_runtime, child, JOURNAL_IDENTITY)
    _install_safe_kernel(monkeypatch, _kernel(), lambda: (LEADER, foreign))
    signals = _install_killpg(monkeypatch, lambda pgid, sig: None)

    with pytest.raises(engine.Fault) as caught:
        _owned_stop(cleanup_runtime, child, LEGACY_IDENTITY)

    _assert_failed_closed(cleanup_runtime, caught.value)
    assert signals == []
    assert child.wait_timeouts == []


def test_owned_stop_killpg_denied_fails_closed_without_retry(
    cleanup_runtime, monkeypatch, helper_native_guards
):
    child = FakeChild()
    _prepare(cleanup_runtime, child, JOURNAL_IDENTITY)
    _install_safe_kernel(monkeypatch, _kernel(), lambda: (LEADER,))

    def denied(pgid, sig):
        raise PermissionError("fixture")

    signals = _install_killpg(monkeypatch, denied)

    with pytest.raises(engine.Fault) as caught:
        _owned_stop(cleanup_runtime, child, JOURNAL_IDENTITY)

    _assert_failed_closed(cleanup_runtime, caught.value)
    assert signals == [(LEADER["pid"], module.signal.SIGTERM)]
    assert child.wait_timeouts == []


def test_owned_stop_after_global_shutdown_deadline_fails_closed(
    monkeypatch, helper_native_guards
):
    now = [1000]
    runtime = module.Runtime.__new__(module.Runtime)
    runtime._cleanup_blocked = False
    shutdown = Shutdown(clock=lambda: now[0])
    runtime.shutdown = shutdown
    runtime._expected_shutdown = shutdown
    shutdown.request()
    now[0] += 51
    child = FakeChild()
    _prepare(runtime, child, JOURNAL_IDENTITY)
    _install_safe_kernel(monkeypatch, _kernel(), lambda: (LEADER,))
    signals = _install_killpg(monkeypatch, lambda pgid, sig: None)

    with pytest.raises(engine.Fault) as caught:
        _owned_stop(runtime, child, LEGACY_IDENTITY)

    _assert_failed_closed(runtime, caught.value)
    assert signals == []
    assert child.wait_timeouts == []


@pytest.mark.parametrize(
    "expected",
    [JOURNAL_IDENTITY, LEGACY_IDENTITY],
    ids=["journal-four-key", "legacy-three-key"],
)
def test_owned_stop_live_leader_clean_group_signals_once_and_reaps(
    cleanup_runtime, monkeypatch, helper_native_guards, expected
):
    child = FakeChild()
    _prepare(cleanup_runtime, child, expected)
    _install_safe_kernel(
        monkeypatch,
        _kernel(),
        lambda: () if child.exited else (LEADER,),
    )

    def terminate(pgid, sig):
        assert pgid == LEADER["pid"]
        assert sig == module.signal.SIGTERM
        child.exited = True

    signals = _install_killpg(monkeypatch, terminate)
    _owned_stop(cleanup_runtime, child, expected)

    assert signals == [(LEADER["pid"], module.signal.SIGTERM)]
    assert child.wait_timeouts
    assert all(0 <= timeout <= 20 for timeout in child.wait_timeouts)
    assert cleanup_runtime._cleanup_blocked is False
