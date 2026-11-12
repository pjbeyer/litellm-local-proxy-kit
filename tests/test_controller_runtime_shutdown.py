import os

import pytest

from core.restart_control import engine, runtime as module
from core.restart_control.recovery import RecoveryStore
from core.restart_control.shutdown import Shutdown


Runtime = module.Runtime
Fault = engine.Fault


@pytest.fixture(autouse=True)
def helper_native_guards(monkeypatch):
    calls = {}

    def guard(name):
        def fail_if_reached(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            raise AssertionError(f"native operation reached: {name}")

        return fail_if_reached

    for name in ("process_identity", "_identity"):
        monkeypatch.setattr(module, name, guard(name))
    monkeypatch.setattr(module, "process_group_identities",
                        guard("process_group_identities"), raising=False)
    for name in ("getpgid", "killpg", "kill"):
        monkeypatch.setattr(module.os, name, guard(f"os.{name}"))
    monkeypatch.setattr(module.subprocess, "run", guard("subprocess.run"))
    monkeypatch.setattr(module.subprocess, "Popen", guard("subprocess.Popen"))
    yield calls
    assert calls == {}, "Forbidden native operation was attempted"


@pytest.fixture
def helper_runtime(tmp_path):
    state = engine.private_dir(tmp_path / "state")
    journal = RecoveryStore(
        state,
        read_json=engine._read_json,
        atomic_json=engine.atomic_json,
        private_path=engine._private_path,
        private_dir=engine.private_dir,
        sync_fd=engine._sync_fd,
        initialize=True,
    )
    binding = {
        "request_id": "a" * 32,
        "kind": "proxy",
        "controller_epoch": "b" * 32,
        "policy_digest": "c" * 64,
        "controller_digest": "d" * 64,
        "controller_plist_digest": "e" * 64,
        "asset_digest": None,
        "runtime_digest": None,
    }
    ticket = journal.begin(binding, "fixture")
    engine.atomic_json(
        state / "requests" / ("a" * 32 + ".json"),
        {
            "request_id": "a" * 32,
            "phase": "preparing",
            "controller_epoch": "b" * 32,
            "scope": "fixture",
            "shared_state_side_effects": False,
        },
    )

    latch = Shutdown()
    runtime = Runtime.__new__(Runtime)
    runtime.state = state
    runtime._expected_shutdown = latch
    runtime._lifecycle_bound = False
    runtime._active_operation = None
    runtime._candidate = None
    runtime._cleanup_blocked = False
    runtime._operation_started = None
    runtime.env = {}
    runtime.config = state
    runtime.bind_lifecycle(latch, journal)
    runtime.begin_operation(ticket)
    return runtime, journal, latch, ticket


def test_helper_shutdown_before_admission_creates_no_child(
    helper_runtime, helper_native_guards, monkeypatch
):
    runtime, journal, latch, _ = helper_runtime
    popen_calls = []

    def fail_if_reached(*args, **kwargs):
        popen_calls.append((args, kwargs))
        raise AssertionError("helper creation reached before admission")

    monkeypatch.setattr(module.subprocess, "Popen", fail_if_reached)
    latch.request()

    with pytest.raises(Fault) as caught:
        runtime._capture(["fixture-readonly-helper"], timeout=1)

    assert caught.value.code == "controller_stopping"
    assert not popen_calls
    assert journal.inventory().spawns == {}
    assert not journal.uncertain


def test_helper_intent_and_one_claim_precede_popen(
    helper_runtime, monkeypatch
):
    runtime, journal, latch, ticket = helper_runtime
    events = []
    original_intent = journal.spawn_intent
    original_commit = latch.commit
    original_claim = latch.claim

    def record_intent(*args, **kwargs):
        result = original_intent(*args, **kwargs)
        events.append("intent")
        return result

    def record_commit(*args, **kwargs):
        result = original_commit(*args, **kwargs)
        events.append("commit")
        return result

    def record_claim(*args, **kwargs):
        result = original_claim(*args, **kwargs)
        events.append("claim")
        return result

    journal.spawn_intent = record_intent
    latch.commit = record_commit
    latch.claim = record_claim

    class ProbeStop(Exception):
        pass

    probe_entered = False

    def probe_popen(*args, **kwargs):
        nonlocal probe_entered
        spawns = journal.inventory().spawns
        assert len(spawns) == 1, "durable spawn intent missing before Popen"
        row = next(iter(spawns.values()))
        assert row["state"] == "intent", "durable spawn intent missing before Popen"
        assert row["purpose"] == "diagnostic"
        assert row["identity"] is None
        assert events == ["intent", "commit", "claim"]
        probe_entered = True
        raise ProbeStop()

    monkeypatch.setattr(module.subprocess, "Popen", probe_popen)
    try:
        runtime._capture(["fixture-readonly-helper"], timeout=1)
    except ProbeStop:
        pass
    except Fault:
        pass
    else:
        pytest.fail("helper capture returned without reaching the Popen probe")

    assert probe_entered
    assert journal.uncertain
    spawns = journal.inventory().spawns
    assert len(spawns) == 1
    row = next(iter(spawns.values()))
    assert row["state"] == "uncertain"
    assert row["identity"] is None


def test_helper_shutdown_between_intent_and_commit_refuses_spawn(
    helper_runtime, monkeypatch
):
    runtime, journal, latch, _ = helper_runtime
    original_intent = journal.spawn_intent
    popen_calls = []

    def intent_then_shutdown(*args, **kwargs):
        handle = original_intent(*args, **kwargs)
        latch.request()
        return handle

    def fail_if_reached(*args, **kwargs):
        popen_calls.append((args, kwargs))
        raise AssertionError("helper creation reached without admitted durable intent")

    journal.spawn_intent = intent_then_shutdown
    monkeypatch.setattr(module.subprocess, "Popen", fail_if_reached)

    with pytest.raises(Fault) as caught:
        runtime._capture(["fixture-readonly-helper"], timeout=1)

    assert caught.value.code == "controller_stopping"
    assert not popen_calls
    assert journal.uncertain
    spawns = journal.inventory().spawns
    assert len(spawns) == 1
    row = next(iter(spawns.values()))
    assert row["state"] == "uncertain"
    assert row["identity"] is None


# T009 journal-lifecycle tests. These mock ONLY the OS I/O boundary that
# _capture touches (Popen, selector/read, process_identity) so the REAL
# RecoveryStore journal transitions run end to end. Full kernel-identity
# cleanup proof (_owned_stop against a live process group) is deliberately
# out of scope here and covered by separate forthcoming cleanup tests; no
# real pid, fd, signal, or process is created.

FIXTURE_PID = 424242
FIXTURE_IDENTITY = {"pid": FIXTURE_PID, "uid": os.getuid(),
                     "pgid": FIXTURE_PID, "birth": [12345, 678]}
FIXTURE_BYTES = b"journal-fixture-payload"


class FakeStream:
    def __init__(self, data, fd):
        self._data = data
        self._fd = fd
        self.closed = False

    def fileno(self):
        return self._fd  # sentinel only; every read is mocked

    def close(self):
        self.closed = True


class FakeChild:
    def __init__(self, data, *, alive=True):
        self.pid = FIXTURE_PID
        self.stdout = FakeStream(data, 0xFDF1)
        self.stderr = FakeStream(b"", 0xFDF2)
        self.alive = alive
        self.returncode = None if alive else 0
        self.waited = False

    def poll(self):
        return None if self.alive else self.returncode

    def wait(self, timeout=None):
        assert timeout is not None and timeout >= 0, "unbounded fake-child wait"
        assert not self.alive, "fake producer has not exited"
        self.returncode = 0 if self.returncode is None else self.returncode
        self.waited = True
        return self.returncode


class FakeSelector:
    """Standard selector keys and mapping, without touching real FDs."""

    def __init__(self):
        self._registered = {}
        self._context = False

    def __enter__(self):
        self._context = True
        return self

    def __exit__(self, *exc):
        self._context = False
        return False

    def register(self, fileobj, events, data=None):
        fd = fileobj.fileno()
        assert fd not in self._registered
        key = module.selectors.SelectorKey(fileobj, fd, events, data)
        self._registered[fd] = key
        return key

    def unregister(self, fileobj):
        fd = fileobj if type(fileobj) is int else fileobj.fileno()
        return self._registered.pop(fd)

    def get_map(self):
        return dict(self._registered)

    def select(self, timeout=None):
        assert self._context
        assert timeout is not None and timeout >= 0
        return [(key, module.selectors.EVENT_READ)
                for key in self._registered.values()]


def install_fake_io(monkeypatch, child, *, identity=FIXTURE_IDENTITY):
    """Mock OS boundaries only; journal decisions and records stay real."""
    remaining = {stream.fileno(): stream._data
                 for stream in (child.stdout, child.stderr)}

    def fake_read(fd, size):
        assert fd in remaining and type(size) is int and size > 0
        chunk, remaining[fd] = remaining[fd][:size], remaining[fd][size:]
        if fd == child.stdout.fileno() and not chunk:
            child.alive = False
            child.returncode = 0
        return chunk

    def fake_set_blocking(fd, blocking):
        assert fd in remaining

    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(module.selectors, "DefaultSelector", FakeSelector)
    monkeypatch.setattr(module.os, "read", fake_read)
    monkeypatch.setattr(module.os, "set_blocking", fake_set_blocking,
                        raising=False)
    if identity is not None:
        def kernel_fact(pid):
            assert pid == child.pid
            return {**identity, "birth": list(identity["birth"]), "ppid": 424240}
        monkeypatch.setattr(module, "process_identity", kernel_fact)
    return child


def test_helper_success_closes_owned_journal_before_return(
    helper_runtime, helper_native_guards, monkeypatch
):
    runtime, journal, latch, ticket = helper_runtime
    child = install_fake_io(monkeypatch, FakeChild(FIXTURE_BYTES))
    owned_rows = []

    def recording_stop(stopped, birth, grace=20):
        # This stipulates cleanup only to isolate real journal ordering;
        # actual ownership cleanup is exercised in the separate battery.
        assert stopped is child
        row = journal._spawn_row(spawn_handle["handle"])
        assert row["state"] == "owned"
        assert row["identity"] == FIXTURE_IDENTITY
        owned_rows.append(row["state"])
        stopped.alive = False
        stopped.wait(timeout=0)

    spawn_handle = {}
    original_intent = journal.spawn_intent

    def recording_intent(active, spawn_id, purpose):
        handle = original_intent(active, spawn_id, purpose)
        spawn_handle["handle"] = handle
        return handle

    journal.spawn_intent = recording_intent
    runtime._owned_stop = recording_stop

    # The cleanup callback above is a stub, not a kernel cleanup claim.

    result = runtime._capture(["fixture-readonly-helper"], timeout=1)

    assert result == FIXTURE_BYTES
    assert owned_rows == ["owned"]
    row = journal._spawn_row(spawn_handle["handle"])
    assert row["state"] == "closed"
    assert row["identity"] == FIXTURE_IDENTITY
    assert set(row["identity"]) == {"pid", "uid", "pgid", "birth"}
    assert child.stdout.closed and child.stderr.closed
    assert child.waited
    assert journal.inventory().spawns[
        spawn_handle["handle"].spawn_id]["state"] == "closed"
    journal.require_clear(ticket)
    assert not journal.uncertain


def test_helper_owned_write_failure_is_sticky_and_blocks_next(
    helper_runtime, helper_native_guards, monkeypatch
):
    runtime, journal, latch, ticket = helper_runtime
    child = install_fake_io(monkeypatch, FakeChild(FIXTURE_BYTES))
    original_owned = journal.spawn_owned

    def owned_then_failure(handle, identity):
        original_owned(handle, identity)
        raise OSError("fixture")

    journal.spawn_owned = owned_then_failure

    def cleanup_stub(stopped, birth, grace=20):
        assert stopped is child
        stopped.alive = False
        stopped.wait(timeout=0)

    runtime._owned_stop = cleanup_stub
    marked = []
    original_uncertain = journal.mark_uncertain

    def recording_uncertain(handle):
        marked.append(journal._spawn_row(handle)["state"])
        return original_uncertain(handle)

    journal.mark_uncertain = recording_uncertain

    with pytest.raises(Fault) as caught:
        runtime._capture(["fixture-readonly-helper"], timeout=1)

    assert caught.value.code in ("recovery_invalid", "candidate_cleanup_failed")
    assert not isinstance(caught.value, OSError)
    assert journal.uncertain
    assert marked == ["owned"]
    row = next(iter(journal.inventory().spawns.values()))
    assert row["state"] == "uncertain"
    assert row["identity"] == FIXTURE_IDENTITY
    assert runtime._cleanup_blocked is True
    assert child.stdout.closed and child.stderr.closed
    assert child.waited  # retained fake child is reaped, never signalled

    # Sticky: the next _capture must refuse to create any new child or
    # spawn intent while cleanup is blocked.
    before = dict(journal.inventory().spawns)
    popen_calls = []
    monkeypatch.setattr(module.subprocess, "Popen",
                        lambda *a, **k: popen_calls.append(1))
    original_intent = journal.spawn_intent

    def fail_intent(*a, **k):
        raise AssertionError("new spawn intent created while cleanup blocked")

    journal.spawn_intent = fail_intent
    with pytest.raises(Fault) as next_caught:
        runtime._capture(["fixture-readonly-helper"], timeout=1)
    assert next_caught.value.code in ("candidate_cleanup_failed", "recovery_invalid")
    assert not popen_calls
    assert journal.inventory().spawns == before


def test_helper_fast_exit_without_identity_never_closes(
    helper_runtime, helper_native_guards, monkeypatch
):
    runtime, journal, latch, ticket = helper_runtime
    # Child already exited before identity capture; the kernel identity
    # probe is unavailable. No fabricated birth/pgid may appear.
    child = install_fake_io(monkeypatch, FakeChild(FIXTURE_BYTES),
                            identity=None)
    child.alive = False
    child.returncode = 0

    def unavailable(pid):
        raise RuntimeError("fixture unavailable")

    monkeypatch.setattr(module, "process_identity", unavailable)

    # Stipulated cleanup callback: the group is empty (exited child),
    # but it is NOT a journal identity — closure must not be claimed.
    def empty_group_stop(stopped, birth, grace=20):
        assert birth is None
        assert stopped is child
        return

    runtime._owned_stop = empty_group_stop
    owned_calls = []
    original_owned = journal.spawn_owned

    def fail_owned(*a, **k):
        owned_calls.append(1)
        raise AssertionError("spawn_owned called without journal identity")

    journal.spawn_owned = fail_owned
    closed_calls = []

    def fail_closed(*a, **k):
        closed_calls.append(1)
        raise AssertionError("spawn_closed called without owned identity")

    journal.spawn_closed = fail_closed

    with pytest.raises(Fault) as caught:
        runtime._capture(["fixture-readonly-helper"], timeout=1)

    assert caught.value.code in ("recovery_invalid", "candidate_cleanup_failed")
    assert not isinstance(caught.value, RuntimeError)
    assert not owned_calls and not closed_calls
    assert journal.uncertain
    row = next(iter(journal.inventory().spawns.values()))
    assert row["state"] == "uncertain"
    assert row["identity"] is None
    assert runtime._cleanup_blocked is True
    assert child.stdout.closed and child.stderr.closed

    # Sticky refusal: no next child, no new intent.
    popen_calls = []
    monkeypatch.setattr(module.subprocess, "Popen",
                        lambda *a, **k: popen_calls.append(1))
    journal.spawn_owned = original_owned

    def fail_intent(*a, **k):
        raise AssertionError("new spawn intent created after identity loss")

    journal.spawn_intent = fail_intent
    with pytest.raises(Fault) as next_caught:
        runtime._capture(["fixture-readonly-helper"], timeout=1)
    assert next_caught.value.code in ("candidate_cleanup_failed", "recovery_invalid")
    assert not popen_calls