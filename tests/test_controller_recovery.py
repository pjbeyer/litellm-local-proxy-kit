"""Closed recovery records and bounded, private on-disk inventory."""

import copy
import importlib.util
import json
import os
import fcntl
from pathlib import Path
import threading
import time

import pytest

from core.restart_control import engine


RID = "a" * 32
SID = "b" * 32
DIGEST = "c" * 64


def binding(kind="proxy", frozen=True):
    return {"request_id": RID, "kind": kind, "controller_epoch": SID,
            "policy_digest": DIGEST, "controller_digest": DIGEST,
            "controller_plist_digest": DIGEST,
            "asset_digest": DIGEST if frozen else None,
            "runtime_digest": DIGEST if frozen else None}


def operation():
    return {"version": 1, "binding": binding(), "scope": "fixture",
            "phase": "preparing", "outcome": None,
            "primary_control_committed": False}


def spawn():
    return {"version": 1, "spawn_id": SID, "binding": binding(),
            "purpose": "candidate", "state": "owned",
            "identity": {"pid": 12, "uid": os.getuid(), "pgid": 12, "birth": [1, 0]}}


def fence():
    return {"version": 1, "binding": binding(), "phase": "stopping", "state": "armed"}


def store(tmp_path):
    from core.restart_control.recovery import RecoveryStore
    state = tmp_path / "state"
    engine.private_dir(state)
    return RecoveryStore(state, read_json=engine._read_json,
                         atomic_json=engine.atomic_json,
                         private_path=engine._private_path,
                         private_dir=engine.private_dir, sync_fd=engine._sync_fd,
                         initialize=True)


def load(state, *, initialize=False, private_dir=engine.private_dir):
    from core.restart_control.recovery import RecoveryStore
    options = {"initialize": initialize} if initialize is not False else {}
    return RecoveryStore(state, read_json=engine._read_json,
                         atomic_json=engine.atomic_json,
                         private_path=engine._private_path,
                         private_dir=private_dir, sync_fd=engine._sync_fd,
                         **options)


def tree_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


def _private_fixture_json(path, value):
    """Build valid large histories without a durable write for every fixture row."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _fill_closed_history(journal, state, target_count):
    remaining = target_count - journal.inventory().entry_count
    assert remaining >= 2
    for index in range(remaining // 2):
        request_id = f"{index + 1:032x}"
        closed_binding = {**binding(frozen=False), "request_id": request_id}
        row = {**operation(), "binding": closed_binding,
               "phase": "blocked", "outcome": "blocked"}
        plan = {"request_id": request_id, "phase": "blocked",
                "controller_epoch": SID, "scope": "fixture",
                "shared_state_side_effects": False, "code": "candidate_failed"}
        _private_fixture_json(state / "recovery/operations" / (request_id + ".json"), row)
        _private_fixture_json(state / "requests" / (request_id + ".json"), plan)
    if remaining % 2:
        child = {**spawn(), "spawn_id": "f" * 32,
                 "binding": {**binding(frozen=False), "request_id": f"{1:032x}"},
                 "purpose": "runtime-proof", "state": "closed"}
        _private_fixture_json(state / "recovery/spawns" / ("f" * 32 + ".json"), child)
    assert journal.inventory().entry_count == target_count


@pytest.mark.parametrize("creator", ["begin", "spawn_intent", "arm_mutation"])
@pytest.mark.parametrize("free_slot", [False, True])
def test_aggregate_capacity_guards_each_journal_creation(tmp_path, creator, free_slot):
    from core.restart_control.recovery import RecoveryError

    state = tmp_path / "state"
    if creator == "begin":
        journal = store(tmp_path)
        ticket = None
    elif creator == "spawn_intent":
        journal = store(tmp_path)
        ticket = journal.begin(binding(frozen=False), "fixture")
        _minimal_request(state)
    else:
        journal, state, prepared = _mutation_ready(tmp_path)
        ticket = journal.reopen_restart(prepared)
    target = 1024 - int(free_slot)
    _fill_closed_history(journal, state, target)
    assert journal.require_clear(ticket) is None
    before = tree_bytes(state)

    def create():
        if creator == "begin":
            return journal.begin({**binding(frozen=False), "request_id": "e" * 32},
                                 "fixture")
        if creator == "spawn_intent":
            return journal.spawn_intent(ticket, "e" * 32, "runtime-proof")
        return journal.arm_mutation(ticket)

    if free_slot:
        create()
        assert journal.inventory().entry_count == 1024
    else:
        with pytest.raises(RecoveryError, match="recovery_invalid"):
            create()
        assert tree_bytes(state) == before
        assert journal.inventory().entry_count == target


@pytest.mark.parametrize("value", [[], {}])
@pytest.mark.parametrize("stage", ["preparing", "fenced"])
def test_unhashable_request_phase_has_closed_error_contract(tmp_path, value, stage):
    from core.restart_control.recovery import RecoveryError

    if stage == "preparing":
        journal = store(tmp_path)
        state = tmp_path / "state"
        ticket = journal.begin(binding(frozen=False), "fixture")
        _minimal_request(state)
    else:
        journal, state, ticket = _manual_failure_ready(tmp_path, "starting")
    _request_phase(state, value)
    before = tree_bytes(state)
    for action in (lambda: journal._require_clear_view(journal.inventory(), ticket),
                   lambda: journal.require_clear(ticket),
                   lambda: journal.spawn_intent(ticket, "e" * 32, "candidate")):
        with pytest.raises(RecoveryError, match="recovery_invalid"):
            action()
        assert tree_bytes(state) == before


def _minimal_request(state, *, request_id=RID, controller_epoch=SID):
    plan = {"request_id": request_id, "phase": "preparing",
            "controller_epoch": controller_epoch, "scope": "fixture",
            "shared_state_side_effects": False}
    engine.atomic_json(state / "requests" / (request_id + ".json"), plan)
    return plan


def _begin_and_freeze(journal, state, binding_value=None):
    value = binding(frozen=False) if binding_value is None else binding_value
    ticket = journal.begin(value, "fixture")
    _minimal_request(state, request_id=value["request_id"],
                     controller_epoch=value["controller_epoch"])
    return journal.bind_frozen(ticket, DIGEST, DIGEST)


def test_initial_missing_request_is_only_a_child_free_unfrozen_window(tmp_path):
    from core.restart_control.recovery import RecoveryError

    journal = store(tmp_path)
    state = tmp_path / "state"
    ticket = journal.begin(binding(frozen=False), "fixture")
    assert journal.require_clear(ticket) is None
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(ticket, SID, "runtime-proof")
    with pytest.raises(RecoveryError):
        journal.bind_frozen(ticket, DIGEST, DIGEST)
    assert tree_bytes(state) == before

    _minimal_request(state)
    proof = journal.spawn_intent(ticket, SID, "runtime-proof")
    identity = spawn()["identity"]
    journal.spawn_owned(proof, identity)
    journal.spawn_closed(proof, identity, cleanup_proven=True)
    frozen = journal.bind_frozen(ticket, DIGEST, DIGEST)
    assert journal.require_clear(frozen) is None
    (state / "requests" / (RID + ".json")).unlink()
    with pytest.raises(RecoveryError):
        journal.require_clear(frozen)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(frozen, "d" * 32, "candidate")


@pytest.mark.parametrize("defect", ["missing", "wrong_epoch", "wrong_scope",
                                    "wrong_phase", "extra_key", "wrong_effects"])
def test_preparing_child_and_freeze_require_valid_minimal_request(tmp_path, defect):
    from core.restart_control.recovery import RecoveryError

    journal = store(tmp_path)
    state = tmp_path / "state"
    ticket = journal.begin(binding(frozen=False), "fixture")
    plan = _minimal_request(state)
    if defect == "missing":
        (state / "requests" / (RID + ".json")).unlink()
    else:
        plan = dict(plan)
        if defect == "wrong_epoch":
            plan["controller_epoch"] = "d" * 32
        elif defect == "wrong_scope":
            plan["scope"] = "production"
        elif defect == "wrong_phase":
            plan["phase"] = "blocked"
        elif defect == "extra_key":
            plan["extra"] = True
        else:
            plan["shared_state_side_effects"] = True
        engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(ticket, SID, "runtime-proof")
    with pytest.raises(RecoveryError):
        journal.bind_frozen(ticket, DIGEST, DIGEST)
    assert tree_bytes(state) == before


def test_deleted_request_after_issued_child_refuses_even_before_freeze(tmp_path):
    from core.restart_control.recovery import RecoveryError

    journal = store(tmp_path)
    state = tmp_path / "state"
    ticket = journal.begin(binding(frozen=False), "fixture")
    _minimal_request(state)
    handle = journal.spawn_intent(ticket, SID, "runtime-proof")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    journal.spawn_closed(handle, identity, cleanup_proven=True)
    (state / "requests" / (RID + ".json")).unlink()
    with pytest.raises(RecoveryError):
        journal.require_clear(ticket)
    with pytest.raises(RecoveryError):
        journal.bind_frozen(ticket, DIGEST, DIGEST)


def test_barrier_begin_owns_only_its_exact_active_ticket(tmp_path):
    from dataclasses import replace
    from core.restart_control.recovery import RecoveryError

    journal = store(tmp_path)
    active = journal.begin(binding(frozen=False), "fixture")
    assert journal.require_clear(active) is None
    for ticket in (journal.operation_ticket(RID), replace(active),
                   load(tmp_path / "state").operation_ticket(RID)):
        with pytest.raises(RecoveryError):
            journal.require_clear(ticket)
    with pytest.raises(RecoveryError):
        load(tmp_path / "state").require_clear()


def test_refreshed_ticket_for_unrelated_owned_row_preserves_exact_active(tmp_path):
    from core.restart_control.recovery import RecoveryError

    journal = store(tmp_path)
    active = journal.begin(binding(frozen=False), "fixture")
    other_id = "d" * 32
    other = operation()
    other["binding"]["request_id"] = other_id
    other["binding"]["asset_digest"] = None
    other["binding"]["runtime_digest"] = None
    journal._write_operation(other)

    refreshed = journal._refreshed_ticket(other)
    duplicate = journal.operation_ticket(RID)
    assert refreshed.request_id == other_id
    assert journal._active_ticket is active
    assert journal._check_active(active)["binding"] == binding(frozen=False)
    for ticket in (refreshed, duplicate,
                   load(tmp_path / "state").operation_ticket(RID)):
        with pytest.raises(RecoveryError):
            journal._check_active(ticket)
    with pytest.raises(RecoveryError):
        journal.require_clear(active)  # The unrelated unresolved row remains visible.


def test_barrier_freeze_and_prepared_stage_become_harmless_history(tmp_path):
    journal = store(tmp_path)
    active = journal.begin(binding(frozen=False), "fixture")
    _minimal_request(tmp_path / "state")
    active = journal.bind_frozen(active, DIGEST, DIGEST)
    assert active.binding == tuple(sorted(binding().items()))
    _prepared_records(tmp_path / "state")
    journal.finish(active)
    assert load(tmp_path / "state").require_clear() is None


def test_barrier_begin_requires_unfrozen_proxy_binding(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    with pytest.raises(RecoveryError):
        journal.begin(binding(), "fixture")
    assert journal.inventory().operations == {}


def test_barrier_stage_finish_checks_unrelated_unresolved_operation(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    active = _begin_and_freeze(journal, tmp_path / "state")
    state = tmp_path / "state"
    _prepared_records(state)
    orphan = operation()
    orphan["binding"]["request_id"] = "d" * 32
    engine.atomic_json(state / "recovery/operations" / ("d" * 32 + ".json"), orphan)
    with pytest.raises(RecoveryError):
        journal.finish(active)
    assert load(state).inventory().operations[RID]["phase"] == "preparing"


@pytest.mark.parametrize("defect", ["missing_request", "consumed_approval",
                                    "orphan_child", "uncertain_child", "temp_file",
                                    "cross_scope", "wrong_epoch"])
def test_barrier_prepared_history_refuses_inconsistent_records(tmp_path, defect):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    active = _begin_and_freeze(journal, tmp_path / "state")
    state = tmp_path / "state"
    _prepared_records(state)
    journal.finish(active)
    request = state / "requests" / (RID + ".json")
    if defect == "missing_request":
        request.unlink()
    elif defect == "consumed_approval":
        _consume_approval(state)
    elif defect in ("orphan_child", "uncertain_child"):
        child = spawn()
        child["state"] = "uncertain" if defect == "uncertain_child" else "closed"
        if defect == "orphan_child":
            child["binding"]["request_id"] = "d" * 32
        engine.atomic_json(state / "recovery/spawns" / (SID + ".json"), child)
    elif defect == "temp_file":
        (state / "recovery/operations" / ".atomic-leftover").write_bytes(b"x")
    elif defect == "cross_scope":
        row = engine._read_json(state / "recovery/operations" / (RID + ".json"))
        row["scope"] = "production"
        engine.atomic_json(state / "recovery/operations" / (RID + ".json"), row)
    else:
        row = engine._read_json(state / "recovery/operations" / (RID + ".json"))
        row["binding"]["controller_epoch"] = "d" * 32
        engine.atomic_json(state / "recovery/operations" / (RID + ".json"), row)
    with pytest.raises(RecoveryError):
        load(state).require_clear()
    with pytest.raises(RecoveryError):
        journal.begin({**binding(frozen=False), "request_id": "e" * 32}, "fixture")


def test_barrier_legitimate_active_children_can_coexist(tmp_path):
    journal = store(tmp_path)
    active = journal.begin(binding(frozen=False), "fixture")
    _minimal_request(tmp_path / "state")
    first = journal.spawn_intent(active, SID, "runtime-proof")
    identity = spawn()["identity"]
    journal.spawn_owned(first, identity)
    second = journal.spawn_intent(active, "d" * 32, "diagnostic")
    journal.spawn_owned(second, {**identity, "pid": 13, "pgid": 13})
    assert journal.require_clear(active) is None
    journal.spawn_closed(first, identity, cleanup_proven=True)
    journal.spawn_closed(second, {**identity, "pid": 13, "pgid": 13}, cleanup_proven=True)
    frozen = journal.bind_frozen(active, DIGEST, DIGEST)
    assert journal.require_clear(frozen) is None
    assert all(row["binding"] == binding() for row in journal.inventory().spawns.values())


def test_barrier_unknown_active_intent_refuses_new_helper(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    unknown = spawn()
    unknown.update(state="intent", identity=None)
    engine.atomic_json(state / "recovery/spawns" / (SID + ".json"), unknown)
    with pytest.raises(RecoveryError):
        journal.require_clear(journal._active_ticket)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(journal._active_ticket, "d" * 32, "diagnostic")


def test_barrier_issued_intent_only_admits_its_ownership_transition(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    with pytest.raises(RecoveryError):
        journal.require_clear(journal._active_ticket)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(journal._active_ticket, "d" * 32, "diagnostic")
    journal.spawn_owned(handle, spawn()["identity"])
    diagnostic = journal.spawn_intent(journal._active_ticket, "d" * 32, "diagnostic")
    assert diagnostic.purpose == "diagnostic"
    assert load(state).inventory().spawns[SID]["state"] == "owned"


@pytest.mark.parametrize("phase", ["prepared", "blocked"])
def test_barrier_request_first_preparation_finish_pair(tmp_path, phase):
    journal = store(tmp_path)
    active = _begin_and_freeze(journal, tmp_path / "state")
    state = tmp_path / "state"
    plan, _ = _prepared_records(state)
    if phase == "blocked":
        (state / "approvals" / (RID + ".json")).unlink()
        plan = {key: plan[key] for key in (
            "request_id", "controller_epoch", "scope", "shared_state_side_effects")}
        plan.update(phase="blocked", code="candidate_failed")
        engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    assert journal.require_clear(active) is None
    journal.finish(active)
    assert load(state).require_clear() is None


def test_barrier_engine_prepared_write_failure_publishes_full_blocked_stage(tmp_path,
                                                                           monkeypatch):
    from core.restart_control.shutdown import Shutdown as EngineShutdown

    class Backend:
        fixture = True
        label = "fixture.proxy"

        def bind_lifecycle(self, shutdown, recovery):
            pass  # bare barrier-engine fixture; journal path is exercised below

        def inspect(self):
            return {"identity": {"pid": 12, "uid": os.getuid(), "birth": [1, 0]},
                    "managed": True}

        def freeze(self):
            return dict.fromkeys(("asset_digest", "runtime_digest", "policy_digest",
                                  "controller_digest"), DIGEST)

        def check_gates(self):
            pass

        def accept_primary(self, identity):
            return {"accepted": False}

        def rehearse(self):
            return {}

        def snapshot_lkg(self, path):
            return {"asset_digest": DIGEST, "runtime_digest": DIGEST}

    controller = engine.Controller(tmp_path / "engine", Backend(),
                                   clock=lambda: 1.0, monotonic=lambda: 1.0,
                                   shutdown=EngineShutdown())
    real_write = engine.atomic_json
    writes = []

    def fail_prepared(path, plan):
        if path.parent == controller.requests:
            writes.append(plan["phase"])
            if plan["phase"] == "prepared":
                raise engine.Fault("state_io")
        return real_write(path, plan)

    monkeypatch.setattr(engine, "atomic_json", fail_prepared)
    result = controller._prepare()
    assert result["status"] == "blocked" and result["code"] == "state_io"
    assert writes == ["preparing", "prepared", "blocked"]
    plan = engine._read_json(controller.requests / (result["request_id"] + ".json"))
    assert plan["phase"] == "blocked" and plan["original_preserved"] is True
    assert plan["primary_checks"] == {"accepted": False}
    assert plan["candidate_checks"] == {}

    journal = store(tmp_path)
    state = tmp_path / "state"
    indexed = {**binding(frozen=False), "request_id": result["request_id"],
               "controller_epoch": controller.epoch}
    active = _begin_and_freeze(journal, state, indexed)
    real_write(state / "requests" / (result["request_id"] + ".json"), plan)
    assert journal.require_clear(active) is None
    journal.finish(active)
    fresh = load(state)
    assert fresh.require_clear() is None
    next_request = {**indexed, "request_id": "d" * 32}
    assert fresh.begin(next_request, "fixture").request_id == "d" * 32


@pytest.mark.parametrize("preserved", [None, False])
def test_barrier_full_blocked_history_validates_after_fresh_load(tmp_path, preserved):
    from core.restart_control.recovery import RecoveryError

    journal = store(tmp_path)
    active = _begin_and_freeze(journal, tmp_path / "state")
    state = tmp_path / "state"
    plan, _ = _prepared_records(state)
    (state / "approvals" / (RID + ".json")).unlink()
    plan.update(phase="blocked", code="state_io")
    if preserved is not None:
        plan["original_preserved"] = preserved
    request = state / "requests" / (RID + ".json")
    engine.atomic_json(request, plan)
    journal.finish(active)
    assert load(state).require_clear() is None

    plan["primary_checks"] = []
    engine.atomic_json(request, plan)
    with pytest.raises(RecoveryError):
        load(state).require_clear()


@pytest.mark.parametrize("defect", ["partial", "extra", "missing_code", "bad_code",
                                    "bad_preserved", "bad_uid", "bad_identity",
                                    "bad_binding", "bad_time", "bad_checks"])
def test_barrier_full_blocked_stage_rejects_bad_fields(tmp_path, defect):
    from core.restart_control.recovery import RecoveryError

    journal = store(tmp_path)
    active = _begin_and_freeze(journal, tmp_path / "state")
    state = tmp_path / "state"
    plan, _ = _prepared_records(state)
    (state / "approvals" / (RID + ".json")).unlink()
    plan.update(phase="blocked", code="state_io", original_preserved=False)
    if defect == "partial":
        del plan["candidate_checks"]
    elif defect == "extra":
        plan["unrecognized"] = True
    elif defect == "missing_code":
        del plan["code"]
    elif defect == "bad_code":
        plan["code"] = "unknown_failure"
    elif defect == "bad_preserved":
        plan["original_preserved"] = 1
    elif defect == "bad_uid":
        plan["uid"] = True
    elif defect == "bad_identity":
        plan["original_identity"]["uid"] = os.getuid() + 1
    elif defect == "bad_binding":
        plan["candidate_binding"]["asset_digest"] = "d" * 64
    elif defect == "bad_time":
        plan["expires_monotonic"] = plan["prepared_monotonic"]
    else:
        plan["candidate_checks"] = []
    engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    with pytest.raises(RecoveryError):
        journal.require_clear(active)
    with pytest.raises(RecoveryError):
        journal.finish(active)
    assert load(state).inventory().operations[RID]["phase"] == "preparing"


def test_barrier_full_blocked_stage_requires_frozen_binding(tmp_path):
    from core.restart_control.recovery import RecoveryError

    journal = store(tmp_path)
    active = journal.begin(binding(frozen=False), "fixture")
    state = tmp_path / "state"
    plan, _ = _prepared_records(state)
    (state / "approvals" / (RID + ".json")).unlink()
    plan.update(phase="blocked", code="state_io", asset_digest=None,
                runtime_digest=None, candidate_binding={"asset_digest": None,
                                                        "runtime_digest": None},
                lkg_binding={"asset_digest": None, "runtime_digest": None})
    engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    with pytest.raises(RecoveryError):
        journal.require_clear(active)


@pytest.mark.parametrize("phase", ["prepared", "blocked"])
def test_barrier_request_first_stage_cannot_admit_next_helper(tmp_path, phase):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    active = _begin_and_freeze(journal, tmp_path / "state")
    state = tmp_path / "state"
    plan, _ = _prepared_records(state)
    if phase == "blocked":
        (state / "approvals" / (RID + ".json")).unlink()
        plan = {key: plan[key] for key in (
            "request_id", "controller_epoch", "scope", "shared_state_side_effects")}
        plan.update(phase="blocked", code="candidate_failed")
        engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(active, SID, "candidate")
    assert journal.inventory().spawns == {}


def test_barrier_request_first_prepared_closes_existing_owned_children(tmp_path):
    from core.restart_control.recovery import RecoveryError

    journal, state = _spawn_ready(tmp_path)
    ticket = journal._active_ticket
    first = journal.spawn_intent(ticket, SID, "candidate")
    first_identity = spawn()["identity"]
    journal.spawn_owned(first, first_identity)
    second = journal.spawn_intent(ticket, "d" * 32, "diagnostic")
    second_identity = {**first_identity, "pid": 13, "pgid": 13}
    journal.spawn_owned(second, second_identity)
    _prepared_records(state)

    assert journal.require_clear(ticket) is None
    with pytest.raises(RecoveryError):
        journal.spawn_intent(ticket, "e" * 32, "diagnostic")
    with pytest.raises(RecoveryError):
        journal.finish(ticket)
    journal.spawn_closed(first, first_identity, cleanup_proven=True)
    assert load(state).inventory().spawns[SID]["state"] == "closed"
    with pytest.raises(RecoveryError):
        journal.finish(ticket)
    journal.spawn_closed(second, second_identity, cleanup_proven=True)
    journal.finish(ticket)
    assert load(state).require_clear() is None


def test_barrier_request_first_prepared_admits_only_issued_intent_ownership(tmp_path):
    from core.restart_control.recovery import RecoveryError

    journal, state = _spawn_ready(tmp_path)
    ticket = journal._active_ticket
    handle = journal.spawn_intent(ticket, SID, "candidate")
    _prepared_records(state)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(ticket, "d" * 32, "diagnostic")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    journal.spawn_closed(handle, identity, cleanup_proven=True)
    journal.finish(ticket)
    assert load(state).require_clear() is None


@pytest.mark.parametrize("defect", ["malformed", "mismatched"])
def test_barrier_request_first_prepared_bad_request_refuses_child_cleanup(tmp_path,
                                                                           defect):
    from core.restart_control.recovery import RecoveryError

    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    plan, _ = _prepared_records(state)
    if defect == "malformed":
        plan["candidate_checks"] = []
    else:
        plan["controller_epoch"] = "d" * 32
    engine.atomic_json(state / "requests" / (RID + ".json"), plan)

    with pytest.raises(RecoveryError):
        journal.require_clear(journal._active_ticket)
    with pytest.raises(RecoveryError):
        journal.spawn_closed(handle, identity, cleanup_proven=True)
    assert load(state).inventory().spawns[SID]["state"] == "owned"


def test_barrier_request_first_prepared_closes_only_exact_issued_handle(tmp_path):
    from dataclasses import replace
    from core.restart_control.recovery import RecoveryError

    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    _prepared_records(state)

    with pytest.raises(RecoveryError):
        journal.spawn_closed(replace(handle), identity, cleanup_proven=True)
    assert load(state).inventory().spawns[SID]["state"] == "owned"
    journal.spawn_closed(handle, identity, cleanup_proven=True)
    journal.finish(journal._active_ticket)


@pytest.mark.parametrize("extra", ["unexpected", "started"])
def test_barrier_active_restart_request_rejects_unplanned_fields(tmp_path, extra):
    from core.restart_control.recovery import RecoveryError
    journal, state, active = _postchecking(tmp_path)
    _request_phase(state, "postchecking")
    assert journal.require_clear(active) is None
    path = state / "requests" / (RID + ".json")
    plan = engine._read_json(path)
    plan[extra] = {"identity": {"pid": 13, "uid": os.getuid(), "birth": [2, 0]},
                   "checks": {}}
    engine.atomic_json(path, plan)
    with pytest.raises(RecoveryError):
        journal.require_clear(active)


def test_barrier_fenced_consumed_request_requires_consumed_approval(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state, ticket = _mutation_ready(tmp_path)
    ticket = journal.arm_mutation(journal.reopen_restart(ticket))
    _request_phase(state, "intent_consumed")
    with pytest.raises(RecoveryError):
        journal.require_clear(ticket)
    assert journal.inventory().approvals[RID][0] is False


@pytest.mark.parametrize("fenced,consumed,request_phase", [
    (False, False, "prepared"),
    (True, False, "prepared"),
    (True, True, "prepared"),
    (True, True, "intent_consumed"),
])
def test_barrier_pending_valid_consumption_boundaries(tmp_path, fenced, consumed,
                                                       request_phase):
    journal, state, ticket = _mutation_ready(tmp_path)
    ticket = journal.reopen_restart(ticket)
    if fenced:
        ticket = journal.arm_mutation(ticket)
    if consumed:
        _consume_approval(state)
    if request_phase != "prepared":
        _request_phase(state, request_phase)
    assert journal.require_clear(ticket) is None


def test_restart_pending_preconsumption_prepared_request_admits_intent(tmp_path):
    journal, state, prepared = _mutation_ready(tmp_path)
    pending = journal.reopen_restart(prepared)
    assert journal.require_clear(pending) is None
    handle = journal.spawn_intent(pending, SID, "candidate")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    journal.spawn_closed(handle, identity, cleanup_proven=True)
    assert load(state).inventory().spawns[SID]["state"] == "closed"


@pytest.mark.parametrize("journal_phase,consumed,admit", [
    ("preparing", False, False), ("preparing", True, False),
    ("prepared", False, False), ("prepared", True, False),
    ("restart_pending", False, True), ("restart_pending", True, False),
    ("intent_consumed", False, False), ("intent_consumed", True, False),
])
def test_prepared_request_child_admission_uses_joint_context(
        tmp_path, journal_phase, consumed, admit):
    from core.restart_control.recovery import RecoveryError

    if journal_phase == "preparing":
        journal, state = _spawn_ready(tmp_path)
        ticket = journal._active_ticket
        _prepared_records(state)
    else:
        journal, state, ticket = _mutation_ready(tmp_path)
        if journal_phase != "prepared":
            ticket = journal.reopen_restart(ticket)
            if journal_phase == "restart_pending" and consumed:
                ticket = journal.arm_mutation(ticket)
            if journal_phase == "intent_consumed":
                ticket = journal.arm_mutation(ticket)
                if consumed:
                    _consume_approval(state)
                    ticket = journal.phase(ticket, "intent_consumed")
                else:
                    row = engine._read_json(state / "recovery/operations" / (RID + ".json"))
                    row["phase"] = "intent_consumed"
                    engine.atomic_json(state / "recovery/operations" / (RID + ".json"), row)
                    fenced = engine._read_json(state / "recovery/mutation.json")
                    fenced["phase"] = "intent_consumed"
                    engine.atomic_json(state / "recovery/mutation.json", fenced)
                    ticket = journal._refreshed_ticket(row)
                    journal._active_ticket = ticket
    if consumed and journal_phase != "intent_consumed":
        _consume_approval(state)
    before = tree_bytes(state)
    if admit:
        assert journal.require_clear(ticket) is None
        handle = journal.spawn_intent(ticket, SID, "candidate")
        assert handle.purpose == "candidate"
        assert journal.inventory().spawns[SID]["state"] == "intent"
    else:
        if journal_phase in ("restart_pending", "intent_consumed") and consumed:
            assert journal.require_clear(ticket) is None
        with pytest.raises(RecoveryError):
            journal.spawn_intent(ticket, SID, "candidate")
        assert tree_bytes(state) == before


@pytest.mark.parametrize("phase", [
    "intent_consumed", "stopping", "starting", "postchecking",
    "rollback_stopping", "rollback_restoring", "rollback_starting",
    "rollback_postchecking",
])
def test_normal_active_request_phase_still_admits_owned_child(tmp_path, phase):
    journal, state, ticket = _manual_failure_ready(tmp_path, phase)
    handle = journal.spawn_intent(ticket, SID, "candidate")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    journal.spawn_closed(handle, identity, cleanup_proven=True)
    assert journal.inventory().spawns[SID]["state"] == "closed"


@pytest.mark.parametrize("terminal", ["restarted", "rolled_back"])
@pytest.mark.parametrize("defect", ["missing_payload", "missing_identity",
                                    "missing_checks", "wrong_uid", "original_identity",
                                    "identity_type", "checks_type", "payload_type"])
def test_barrier_request_first_terminal_requires_complete_accepted_result(
        tmp_path, terminal, defect):
    from core.restart_control.recovery import RecoveryError
    journal, state, ticket = _postchecking(tmp_path, terminal)
    plan, _ = _accepted_records(state, terminal)
    key = "started" if terminal == "restarted" else "rollback"
    if defect == "missing_payload":
        del plan[key]
    elif defect == "missing_identity":
        del plan[key]["identity"]
    elif defect == "missing_checks":
        del plan[key]["checks"]
    elif defect == "wrong_uid":
        plan[key]["identity"]["uid"] += 1
    elif defect == "original_identity":
        plan[key]["identity"] = copy.deepcopy(plan["original_identity"])
    elif defect == "identity_type":
        plan[key]["identity"] = []
    elif defect == "checks_type":
        plan[key]["checks"] = []
    else:
        plan[key] = []
    engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    with pytest.raises(RecoveryError):
        journal.require_clear(ticket)
    assert journal.inventory().fence is not None


@pytest.mark.parametrize("terminal", ["restarted", "rolled_back"])
@pytest.mark.parametrize("checks", [{}, {"accepted": False}])
def test_barrier_request_first_terminal_accepts_legacy_checks_dict(
        tmp_path, terminal, checks):
    journal, state, ticket = _postchecking(tmp_path, terminal)
    plan, _ = _accepted_records(state, terminal)
    plan["started" if terminal == "restarted" else "rollback"]["checks"] = checks
    engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    assert journal.require_clear(ticket) is None


@pytest.mark.parametrize("terminal", ["restarted", "rolled_back"])
def test_barrier_request_first_terminal_only_admits_existing_child_cleanup(
        tmp_path, terminal):
    from core.restart_control.recovery import RecoveryError
    journal, state, ticket = _postchecking(tmp_path, terminal)
    _request_phase(state, "postchecking" if terminal == "restarted" else
                   "rollback_postchecking")
    handle = journal.spawn_intent(ticket, SID, "cleanup-diagnostic")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    _accepted_records(state, terminal)
    assert journal.require_clear(ticket) is None
    with pytest.raises(RecoveryError):
        journal.spawn_intent(ticket, "d" * 32, "diagnostic")
    with pytest.raises(RecoveryError):
        journal.finish(ticket)
    assert journal.inventory().spawns[SID]["state"] == "owned"
    journal.spawn_closed(handle, identity, cleanup_proven=True)
    journal.finish(ticket)
    assert load(state).inventory().fence is None


def test_barrier_end_operation_retains_incomplete_record(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    active = journal.begin(binding(frozen=False), "fixture")
    journal.end_operation(active)
    with pytest.raises(RecoveryError):
        journal.require_clear()
    with pytest.raises(RecoveryError):
        journal.begin({**binding(frozen=False), "request_id": "d" * 32,
                       "controller_epoch": "e" * 32}, "fixture")


def test_barrier_stage_postpublication_fault_keeps_store_sticky_but_clean_history_loads(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    active = _begin_and_freeze(journal, tmp_path / "state")
    state = tmp_path / "state"
    _prepared_records(state)

    def fault_after_publish(path, value):
        engine.atomic_json(path, value)
        raise OSError("injected after durable replace")

    journal._atomic_json = fault_after_publish
    with pytest.raises(RecoveryError):
        journal.finish(active)
    assert journal.uncertain is True
    with pytest.raises(RecoveryError):
        journal.require_clear()
    fresh = load(state)
    assert fresh.require_clear() is None
    fresh.begin({**binding(frozen=False), "request_id": "d" * 32}, "fixture")


@pytest.mark.parametrize("fault_point,fresh_clear", [
    ("file_sync", False), ("directory_sync", True)])
def test_barrier_stage_atomic_sync_fault_uses_surviving_durable_facts(
        tmp_path, monkeypatch, fault_point, fresh_clear):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    active = _begin_and_freeze(journal, tmp_path / "state")
    state = tmp_path / "state"
    _prepared_records(state)
    real_sync = engine._sync_fd
    calls = 0

    def fault_sync(fd):
        nonlocal calls
        calls += 1
        if calls == (1 if fault_point == "file_sync" else 2):
            raise OSError("injected sync failure")
        real_sync(fd)

    monkeypatch.setattr(engine, "_sync_fd", fault_sync)
    with pytest.raises(RecoveryError):
        journal.finish(active)
    assert journal.uncertain is True
    with pytest.raises(RecoveryError):
        journal.require_clear()
    fresh = load(state)
    if fresh_clear:
        assert fresh.require_clear() is None
    else:
        with pytest.raises(RecoveryError):
            fresh.require_clear()


def test_initialization_explicit_then_default_load_keeps_tree_and_bytes(tmp_path):
    state = tmp_path / "state"
    engine.private_dir(state)
    load(state, initialize=True)
    write(state / "recovery/operations" / (RID + ".json"), operation())
    before = tree_bytes(state)
    assert load(state).inventory().operations[RID] == operation()
    assert tree_bytes(state) == before


def test_default_load_does_not_create_missing_journal_in_empty_root(tmp_path):
    from core.restart_control.recovery import RecoveryError
    state = tmp_path / "state"
    engine.private_dir(state)
    with pytest.raises(RecoveryError):
        load(state)
    assert tree_bytes(state) == {}


@pytest.mark.parametrize("missing", ["state", "recovery", "recovery/operations",
                                        "recovery/spawns", "requests", "approvals"])
def test_default_load_missing_required_view_refuses_without_creation(tmp_path, missing):
    from core.restart_control.recovery import RecoveryError
    state = tmp_path / "state"
    if missing != "state":
        engine.private_dir(state)
        load(state, initialize=True)
        if missing == "recovery":
            (state / "recovery/operations").rmdir()
            (state / "recovery/spawns").rmdir()
        (state / missing).rmdir()
    before = tree_bytes(state) if state.exists() else None
    with pytest.raises(RecoveryError):
        load(state)
    assert (tree_bytes(state) if state.exists() else None) == before


def test_existing_records_with_missing_journal_refuses_without_replacement(tmp_path):
    from core.restart_control.recovery import RecoveryError
    state = tmp_path / "state"
    engine.private_dir(state)
    load(state, initialize=True)
    write(state / "requests" / (RID + ".json"), {"retained": True})
    (state / "recovery/spawns").rmdir()
    (state / "recovery/operations").rmdir()
    (state / "recovery").rmdir()
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        load(state)
    assert tree_bytes(state) == before


def test_deleted_empty_view_refuses_on_restart(tmp_path):
    from core.restart_control.recovery import RecoveryError
    state = tmp_path / "state"
    engine.private_dir(state)
    load(state, initialize=True)
    (state / "approvals").rmdir()
    with pytest.raises(RecoveryError):
        load(state)
    assert not (state / "approvals").exists()


@pytest.mark.parametrize("entry", ["note.txt", "empty"])
def test_initialize_refuses_nonempty_root_without_repair(tmp_path, entry):
    from core.restart_control.recovery import RecoveryError
    state = tmp_path / "state"
    engine.private_dir(state)
    path = state / entry
    if entry == "empty":
        path.mkdir(mode=0o700)
    else:
        path.write_text("harmless")
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        load(state, initialize=True)
    assert tree_bytes(state) == before


def test_initialize_refuses_existing_empty_view_and_repeat(tmp_path):
    from core.restart_control.recovery import RecoveryError
    state = tmp_path / "state"
    engine.private_dir(state)
    (state / "recovery").mkdir(mode=0o700)
    with pytest.raises(RecoveryError):
        load(state, initialize=True)
    assert tree_bytes(state) == {"recovery": None}
    (state / "recovery").rmdir()
    load(state, initialize=True)
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        load(state, initialize=True)
    assert tree_bytes(state) == before


def test_partial_initialization_never_autorepairs(tmp_path):
    from core.restart_control.recovery import RecoveryError
    state = tmp_path / "state"
    engine.private_dir(state)
    def fail_on_spawns(path):
        if Path(path) == state / "recovery/spawns":
            raise OSError("fixture failure")
        return engine.private_dir(path)
    with pytest.raises(RecoveryError):
        load(state, initialize=True, private_dir=fail_on_spawns)
    before = tree_bytes(state)
    assert before == {"recovery": None, "recovery/operations": None}
    with pytest.raises(RecoveryError):
        load(state)
    with pytest.raises(RecoveryError):
        load(state, initialize=True)
    assert tree_bytes(state) == before


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_initialize_wrong_types_refuse_closed(tmp_path, value):
    from core.restart_control.recovery import RecoveryError
    state = tmp_path / "state"
    engine.private_dir(state)
    with pytest.raises(RecoveryError) as error:
        load(state, initialize=value)
    assert error.value.code == "recovery_invalid"
    assert tree_bytes(state) == {}


def write(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def test_schema_api_assertion(tmp_path):
    assert importlib.util.find_spec("core.restart_control.recovery") is not None, "recovery API missing"
    journal = store(tmp_path)
    assert journal.validate_binding(binding()) == binding()
    assert journal.validate_operation(operation(), request_id=RID) == operation()
    assert journal.validate_spawn(spawn(), spawn_id=SID) == spawn()
    assert journal.validate_fence(fence()) == fence()


@pytest.mark.parametrize("method,valid,keys", [
    ("validate_binding", binding, tuple(binding())),
    ("validate_operation", operation, tuple(operation())),
    ("validate_spawn", spawn, tuple(spawn())),
    ("validate_fence", fence, tuple(fence())),
])
def test_schema_exact_top_level_keys(tmp_path, method, valid, keys):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    for key in keys:
        row = valid()
        del row[key]
        with pytest.raises(RecoveryError):
            getattr(journal, method)(row)
        row = valid()
        row[key] = object()
        with pytest.raises(RecoveryError):
            getattr(journal, method)(row)
    row = valid()
    row["extra"] = 1
    with pytest.raises(RecoveryError):
        getattr(journal, method)(row)


@pytest.mark.parametrize("method,valid,field,bad", [
    ("validate_binding", binding, "request_id", "A" * 32),
    ("validate_binding", binding, "kind", "other"),
    ("validate_binding", binding, "controller_epoch", "0"),
    ("validate_binding", binding, "policy_digest", "z" * 64),
    ("validate_binding", binding, "controller_digest", "0"),
    ("validate_binding", binding, "controller_plist_digest", True),
    ("validate_binding", binding, "asset_digest", None),
    ("validate_binding", binding, "runtime_digest", None),
    ("validate_operation", operation, "version", True),
    ("validate_operation", operation, "scope", "unknown"),
    ("validate_operation", operation, "phase", "unknown"),
    ("validate_operation", operation, "outcome", "unknown"),
    ("validate_operation", operation, "primary_control_committed", 1),
    ("validate_spawn", spawn, "version", 1.0),
    ("validate_spawn", spawn, "spawn_id", "B" * 32),
    ("validate_spawn", spawn, "purpose", "other"),
    ("validate_spawn", spawn, "state", "other"),
    ("validate_spawn", spawn, "identity", {}),
    ("validate_fence", fence, "version", False),
    ("validate_fence", fence, "phase", "unknown"),
    ("validate_fence", fence, "state", "unknown"),
])
def test_schema_bad_fields(tmp_path, method, valid, field, bad):
    from core.restart_control.recovery import RecoveryError
    row = valid()
    row[field] = bad
    with pytest.raises(RecoveryError):
        getattr(store(tmp_path), method)(row)


@pytest.mark.parametrize("field,bad", [
    ("pid", True), ("pid", 0), ("uid", -1), ("uid", 1.0),
    ("pgid", 0), ("birth", [0, 0]), ("birth", [1, 1000000]),
    ("birth", [True, 0]), ("birth", [1.0, 0]), ("birth", [1]),
    ("birth", [1, 0, 0]), ("birth", [float("nan"), 0]),
])
def test_schema_identity_fields(tmp_path, field, bad):
    from core.restart_control.recovery import RecoveryError
    row = spawn()
    row["identity"][field] = bad
    with pytest.raises(RecoveryError):
        store(tmp_path).validate_spawn(row)


def test_schema_nested_keys_and_bindings(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    for row, method in ((operation(), journal.validate_operation),
                        (spawn(), journal.validate_spawn), (fence(), journal.validate_fence)):
        for key in binding():
            bad = copy.deepcopy(row)
            del bad["binding"][key]
            with pytest.raises(RecoveryError):
                method(bad)
        bad = copy.deepcopy(row)
        bad["binding"]["extra"] = 1
        with pytest.raises(RecoveryError):
            method(bad)
    bad = spawn()
    bad["identity"]["ppid"] = 1
    with pytest.raises(RecoveryError):
        journal.validate_spawn(bad)


def test_schema_null_and_purpose_restrictions(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    canary = binding("platform-canary", frozen=False)
    assert journal.validate_binding(canary) == canary
    for purpose in ("canary-query", "canary-bootstrap", "canary-bootout"):
        row = spawn()
        row.update(binding=canary, purpose=purpose)
        assert journal.validate_spawn(row) == row
    for purpose in ("candidate", "credential-resolver", "service-stop",
                    "service-start"):
        row = spawn()
        row.update(binding=canary, purpose=purpose)
        with pytest.raises(RecoveryError):
            journal.validate_spawn(row)
        row["binding"] = binding(frozen=False)
        with pytest.raises(RecoveryError):
            journal.validate_spawn(row)
    for row in (operation(), fence()):
        row["binding"] = canary
        if "state" in row:
            with pytest.raises(RecoveryError):
                journal.validate_fence(row)
        else:
            row["phase"] = "stopping"
            with pytest.raises(RecoveryError):
                journal.validate_operation(row)


@pytest.mark.parametrize("phase,outcome", [
    ("preparing", None), ("blocked", None), ("blocked", "blocked"),
    ("shutdown_pending", None), ("manual_recovery_required", None),
    ("manual_recovery_required", "manual_recovery_required"),
])
def test_schema_null_proxy_conservative_operation(tmp_path, phase, outcome):
    row = operation()
    row.update(binding=binding(frozen=False), phase=phase, outcome=outcome)
    assert store(tmp_path).validate_operation(row) == row


@pytest.mark.parametrize("phase,outcome", [
    ("preparing", "blocked"), ("blocked", "manual_recovery_required"),
    ("shutdown_pending", "blocked"), ("manual_recovery_required", "blocked"),
    ("prepared", None), ("restart_pending", None),
    ("restarted", "restarted"),
])
def test_schema_null_proxy_wrong_phase_outcome_refuses(tmp_path, phase, outcome):
    from core.restart_control.recovery import RecoveryError
    row = operation()
    row.update(binding=binding(frozen=False), phase=phase, outcome=outcome)
    with pytest.raises(RecoveryError):
        store(tmp_path).validate_operation(row)


@pytest.mark.parametrize("phase,outcome", [
    ("preparing", None), ("blocked", None), ("blocked", "blocked"),
    ("shutdown_pending", None), ("manual_recovery_required", None),
    ("manual_recovery_required", "manual_recovery_required"),
])
def test_schema_null_proxy_primary_control_refuses(tmp_path, phase, outcome):
    from core.restart_control.recovery import RecoveryError
    row = operation()
    row.update(binding=binding(frozen=False), phase=phase, outcome=outcome,
               primary_control_committed=True)
    with pytest.raises(RecoveryError):
        store(tmp_path).validate_operation(row)


@pytest.mark.parametrize("kind,frozen", [("proxy", True),
                                         ("platform-canary", False)])
def test_schema_cleanup_diagnostic_shared(tmp_path, kind, frozen):
    row = spawn()
    row.update(binding=binding(kind, frozen=frozen), purpose="cleanup-diagnostic")
    assert store(tmp_path).validate_spawn(row) == row


def test_schema_null_proxy_cleanup_diagnostic_refuses(tmp_path):
    from core.restart_control.recovery import RecoveryError
    row = spawn()
    row.update(binding=binding(frozen=False), purpose="cleanup-diagnostic")
    with pytest.raises(RecoveryError):
        store(tmp_path).validate_spawn(row)


@pytest.mark.parametrize("purpose", ["runtime-proof", "diagnostic", "preflight"])
def test_schema_canary_noncanary_purpose_refuses(tmp_path, purpose):
    from core.restart_control.recovery import RecoveryError
    row = spawn()
    row.update(binding=binding("platform-canary", frozen=False), purpose=purpose)
    with pytest.raises(RecoveryError):
        store(tmp_path).validate_spawn(row)


@pytest.mark.parametrize("purpose", ["candidate", "credential-resolver",
                                    "service-stop", "service-start", "preflight"])
def test_schema_null_proxy_nonproof_purpose_refuses(tmp_path, purpose):
    from core.restart_control.recovery import RecoveryError
    row = spawn()
    row.update(binding=binding(frozen=False), purpose=purpose)
    with pytest.raises(RecoveryError):
        store(tmp_path).validate_spawn(row)


@pytest.mark.parametrize("purpose", ["runtime-proof", "diagnostic"])
def test_schema_null_proxy_proof_purpose(tmp_path, purpose):
    row = spawn()
    row.update(binding=binding(frozen=False), purpose=purpose)
    assert store(tmp_path).validate_spawn(row) == row


@pytest.mark.parametrize("purpose", ["canary-query", "canary-bootstrap",
                                    "canary-bootout"])
def test_schema_canary_purpose_on_proxy_refuses(tmp_path, purpose):
    from core.restart_control.recovery import RecoveryError
    row = spawn()
    row["purpose"] = purpose
    with pytest.raises(RecoveryError):
        store(tmp_path).validate_spawn(row)


@pytest.mark.parametrize("phase,outcome", [
    ("canary_pending", None), ("canary_running", None),
    ("canary_passed", "canary_passed"),
    ("canary_blocked", "canary_blocked"),
])
def test_schema_each_canary_phase(tmp_path, phase, outcome):
    row = operation()
    row.update(binding=binding("platform-canary", frozen=False),
               phase=phase, outcome=outcome)
    assert store(tmp_path).validate_operation(row) == row


@pytest.mark.parametrize("phase,outcome", [
    ("canary_pending", None), ("canary_running", None),
    ("canary_passed", "canary_passed"),
    ("canary_blocked", "canary_blocked"),
])
def test_schema_canary_phase_on_proxy_refuses(tmp_path, phase, outcome):
    from core.restart_control.recovery import RecoveryError
    row = operation()
    row.update(phase=phase, outcome=outcome)
    with pytest.raises(RecoveryError):
        store(tmp_path).validate_operation(row)


@pytest.mark.parametrize("field", tuple(spawn()["identity"]))
def test_schema_identity_missing_key_refuses(tmp_path, field):
    from core.restart_control.recovery import RecoveryError
    row = spawn()
    del row["identity"][field]
    with pytest.raises(RecoveryError):
        store(tmp_path).validate_spawn(row)


def test_schema_json_wrong_types_are_closed(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    shapes = (None, True, 0, 1.5, float("nan"), float("inf"),
              "", [], {})
    cases = ((journal.validate_binding, binding(), (), {
        "request_id": str, "kind": str, "controller_epoch": str,
        "policy_digest": str, "controller_digest": str,
        "controller_plist_digest": str, "asset_digest": str,
        "runtime_digest": str}),
        (journal.validate_operation, operation(), (), {
            "version": int, "binding": dict, "scope": str, "phase": str,
            "outcome": (str, type(None)), "primary_control_committed": bool}),
        (journal.validate_spawn, spawn(), (), {
            "version": int, "spawn_id": str, "binding": dict,
            "purpose": str, "state": str, "identity": dict}),
        (journal.validate_fence, fence(), (), {
            "version": int, "binding": dict, "phase": str, "state": str}),
        (journal.validate_spawn, spawn(), ("identity",), {
            "pid": int, "uid": int, "pgid": int, "birth": list}))
    for validate, original, path, fields in cases:
        for field, allowed_types in fields.items():
            if not isinstance(allowed_types, tuple):
                allowed_types = (allowed_types,)
            for shape in shapes:
                if type(shape) in allowed_types:
                    continue
                row = copy.deepcopy(original)
                target = row["identity"] if path else row
                target[field] = shape
                with pytest.raises(RecoveryError):
                    validate(row)
    for validate in (journal.validate_binding, journal.validate_operation,
                     journal.validate_spawn, journal.validate_fence):
        for shape in shapes:
            if type(shape) is dict:
                continue
            with pytest.raises(RecoveryError):
                validate(shape)
    for validate, original in ((journal.validate_operation, operation()),
                               (journal.validate_spawn, spawn()),
                               (journal.validate_fence, fence())):
        for shape in shapes:
            if type(shape) is dict:
                continue
            row = copy.deepcopy(original)
            row["binding"] = shape
            with pytest.raises(RecoveryError):
                validate(row)
    for index in (0, 1):
        for shape in shapes:
            if type(shape) is int:
                continue
            row = spawn()
            row["identity"]["birth"][index] = shape
            with pytest.raises(RecoveryError):
                journal.validate_spawn(row)


@pytest.mark.parametrize("state,identity_ok", [("intent", False), ("uncertain", False),
                                                 ("owned", True), ("closed", True)])
def test_schema_identity_null_state(tmp_path, state, identity_ok):
    from core.restart_control.recovery import RecoveryError
    row = spawn()
    row.update(state=state, identity=None)
    if identity_ok:
        with pytest.raises(RecoveryError):
            store(tmp_path).validate_spawn(row)
    else:
        assert store(tmp_path).validate_spawn(row) == row


def test_schema_filename_binding(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    with pytest.raises(RecoveryError):
        journal.validate_operation(operation(), request_id=SID)
    with pytest.raises(RecoveryError):
        journal.validate_spawn(spawn(), spawn_id=RID)


def test_schema_inventory_reads_real_parser_and_rejects_duplicate_and_oversize(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    path = tmp_path / "state/recovery/operations" / (RID + ".json")
    write(path, operation())
    assert journal.inventory().operations[RID] == operation()
    path.write_text('{"version":1,"version":1}')
    with pytest.raises(RecoveryError):
        journal.inventory()
    path.write_bytes(b"x" * 65537)
    with pytest.raises(RecoveryError):
        journal.inventory()


@pytest.mark.parametrize("name", ["stray", "." + RID + ".json." + SID,
                                         "BAD.json"])
def test_schema_unknown_and_atomic_partial_entries(tmp_path, name):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    write(tmp_path / "state/recovery/operations" / name, operation())
    with pytest.raises(RecoveryError):
        journal.inventory()


def test_schema_population_boundary_aggregate(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    base = tmp_path / "state/requests"
    for number in range(1022):
        write(base / (f"{number:032x}.json"), {})
    assert journal.inventory().entry_count == 1024
    write(tmp_path / "state/approvals" / (RID + ".json"), {})
    with pytest.raises(RecoveryError):
        journal.inventory()


def test_schema_spawn_cap_per_operation(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    for number in range(64):
        row = spawn()
        row["spawn_id"] = f"{number:032x}"
        write(tmp_path / "state/recovery/spawns" / (row["spawn_id"] + ".json"), row)
    assert len(journal.inventory().spawns) == 64
    row = spawn()
    row["spawn_id"] = "f" * 32
    write(tmp_path / "state/recovery/spawns" / (row["spawn_id"] + ".json"), row)
    with pytest.raises(RecoveryError):
        journal.inventory()


@pytest.mark.parametrize("defect", ["file_mode", "hardlink", "symlink", "directory",
                                    "parent_mode", "parent_symlink", "wrong_owner"])
def test_schema_metadata_refusals(tmp_path, defect, monkeypatch):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    path = tmp_path / "state/recovery/operations" / (RID + ".json")
    write(path, operation())
    if defect == "file_mode":
        path.chmod(0o644)
    elif defect == "hardlink":
        os.link(path, tmp_path / "hardlink-target.json")
    elif defect == "symlink":
        path.unlink()
        path.symlink_to("target")
    elif defect == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    elif defect == "parent_mode":
        path.parent.chmod(0o755)
    elif defect == "parent_symlink":
        parent = path.parent
        parent.rename(parent.with_name("saved"))
        parent.symlink_to("saved", target_is_directory=True)
    else:
        original = Path.lstat
        def foreign(target):
            info = original(target)
            if Path(target) == path:
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info
        monkeypatch.setattr(Path, "lstat", foreign)
    with pytest.raises(RecoveryError):
        journal.inventory()


def test_schema_populated_canary_never_clears(tmp_path):
    journal = store(tmp_path)
    write(tmp_path / "state/canary/intent.json", {})
    view = journal.inventory()
    assert view.canary_uncertain is True
    assert view.entry_count == 3


def test_schema_cross_record_binding_mismatch_refuses(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    write(tmp_path / "state/recovery/operations" / (RID + ".json"), operation())
    row = spawn()
    row["binding"]["controller_epoch"] = "d" * 32
    write(tmp_path / "state/recovery/spawns" / (SID + ".json"), row)
    with pytest.raises(RecoveryError):
        journal.inventory()


@pytest.mark.parametrize("location", ["requests", "approvals", "recovery/spawns",
                                           "canary/detail", "canary/evidence"])
def test_schema_atomic_partial_in_each_view(tmp_path, location):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    write(tmp_path / "state" / location / ("." + RID + ".json." + SID), {})
    with pytest.raises(RecoveryError):
        journal.inventory()


@pytest.mark.parametrize("location", ["state", "state/recovery", "state/recovery/spawns",
                                           "state/requests", "state/approvals", "state/canary"])
def test_schema_private_parent_modes(tmp_path, location):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    parent = tmp_path / location
    if not parent.exists():
        parent.mkdir(mode=0o700)
    parent.chmod(0o755)
    with pytest.raises(RecoveryError):
        journal.inventory()


def test_schema_canary_scope_contents_refuse_until_receipt_schema(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal = store(tmp_path)
    write(tmp_path / "state/canary" / RID / "worker.json", {})
    with pytest.raises(RecoveryError):
        journal.inventory()


def _spawn_ready(tmp_path, *, atomic_json=engine.atomic_json, sync_fd=engine._sync_fd):
    journal = store(tmp_path)
    state = tmp_path / "state"
    ticket = journal.begin(binding(frozen=False), "fixture")
    _minimal_request(state)
    proof = journal.spawn_intent(ticket, "0" * 32, "runtime-proof")
    identity = spawn()["identity"]
    journal.spawn_owned(proof, identity)
    journal.spawn_closed(proof, identity, cleanup_proven=True)
    journal.bind_frozen(ticket, DIGEST, DIGEST)
    if atomic_json is not engine.atomic_json or sync_fd is not engine._sync_fd:
        journal._atomic_json = atomic_json
        journal._sync_fd = sync_fd
    return journal, state


def test_spawn_intent_is_durable_before_outside_action_and_identity_follows(tmp_path):
    journal, state = _spawn_ready(tmp_path)
    ticket = journal._active_ticket
    handle = journal.spawn_intent(ticket, SID, "candidate")

    def outside_action():
        assert load(state).inventory().spawns[SID] == {
            "version": 1, "spawn_id": SID, "binding": binding(),
            "purpose": "candidate", "state": "intent", "identity": None}
        return copy.deepcopy(spawn()["identity"])

    identity = outside_action()
    journal.spawn_owned(handle, identity)
    assert load(state).inventory().spawns[SID] == spawn()
    journal.spawn_closed(handle, identity, cleanup_proven=True)
    assert load(state).inventory().spawns[SID]["state"] == "closed"


def test_spawn_intent_without_identity_cannot_be_owned_or_closed(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    with pytest.raises(RecoveryError):
        journal.spawn_closed(handle, spawn()["identity"], cleanup_proven=True)
    with pytest.raises(RecoveryError):
        journal.spawn_owned(handle, None)
    assert load(state).inventory().spawns[SID]["state"] == "intent"
    assert load(state).inventory().spawns[SID]["identity"] is None


def test_spawn_exact_operation_handle_binding_and_order(tmp_path):
    from dataclasses import replace
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    ticket = journal._active_ticket
    handle = journal.spawn_intent(ticket, SID, "candidate")
    other = operation()
    other["binding"]["request_id"] = "d" * 32
    engine.atomic_json(state / "recovery/operations" / ("d" * 32 + ".json"), other)
    other_ticket = journal.operation_ticket("d" * 32)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(replace(ticket, request_id="d" * 32), SID, "candidate")
    for bad in (replace(handle, spawn_id="e" * 32),
                replace(handle, binding=other_ticket.binding),
                replace(handle, purpose="diagnostic")):
        with pytest.raises(RecoveryError):
            journal.spawn_owned(bad, spawn()["identity"])
    with pytest.raises(RecoveryError):
        journal.spawn_intent(ticket, SID, "candidate")
    other_store = load(state)
    with pytest.raises(RecoveryError):
        other_store.spawn_owned(handle, spawn()["identity"])
    changed = operation()
    changed["binding"]["controller_epoch"] = "e" * 32
    engine.atomic_json(state / "recovery/operations" / (RID + ".json"), changed)
    with pytest.raises(RecoveryError):
        journal.spawn_owned(handle, spawn()["identity"])
    assert engine._read_json(state / "recovery/spawns" / (SID + ".json"))["state"] == "intent"


@pytest.mark.parametrize("fault", ["before", "after"])
def test_spawn_intent_write_fault_never_invokes_action(tmp_path, fault):
    from core.restart_control.recovery import RecoveryError
    def failing_atomic(path, value):
        if fault == "before":
            raise OSError("injected")
        engine.atomic_json(path, value)
        raise OSError("injected")
    journal, state = _spawn_ready(tmp_path, atomic_json=failing_atomic)
    called = []
    with pytest.raises(RecoveryError):
        handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
        called.append(handle)
    assert called == []
    if fault == "after":
        assert load(state).inventory().spawns[SID]["state"] == "intent"
    assert journal.uncertain is True
    with pytest.raises(RecoveryError):
        journal.spawn_intent(journal._active_ticket, "e" * 32, "candidate")


@pytest.mark.parametrize("fault", ["before", "after"])
def test_spawn_identity_write_fault_retains_uncertainty(tmp_path, fault):
    from core.restart_control.recovery import RecoveryError
    writes = 0
    def failing_atomic(path, value):
        nonlocal writes
        writes += 1
        if writes == 2 and fault == "before":
            raise OSError("injected")
        engine.atomic_json(path, value)
        if writes == 2:
            raise OSError("injected")
    journal, state = _spawn_ready(tmp_path, atomic_json=failing_atomic)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    with pytest.raises(RecoveryError):
        journal.spawn_owned(handle, spawn()["identity"])
    assert journal.uncertain is True
    assert load(state).inventory().spawns[SID]["state"] in ("intent", "owned")
    with pytest.raises(RecoveryError):
        journal.spawn_closed(handle, spawn()["identity"], cleanup_proven=True)


@pytest.mark.parametrize("point", ["file_sync", "before_replace", "after_replace",
                                   "directory_sync"])
def test_spawn_identity_atomic_fault_points_keep_durable_uncertainty(tmp_path, monkeypatch,
                                                                     point):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    original_sync = engine._sync_fd
    original_replace = os.replace
    sync_calls = 0

    def sync_fault(fd):
        nonlocal sync_calls
        sync_calls += 1
        if (point == "file_sync" and sync_calls == 1) or (
                point == "directory_sync" and sync_calls == 2):
            raise OSError("injected")
        original_sync(fd)

    def replace_fault(src, dst):
        if point == "before_replace":
            raise OSError("injected")
        original_replace(src, dst)
        if point == "after_replace":
            raise OSError("injected")

    monkeypatch.setattr(engine, "_sync_fd", sync_fault)
    monkeypatch.setattr(os, "replace", replace_fault)
    with pytest.raises(RecoveryError):
        journal.spawn_owned(handle, spawn()["identity"])
    assert journal.uncertain is True
    row = load(state).inventory().spawns[SID]
    assert row["state"] == ("owned" if point in ("after_replace", "directory_sync")
                            else "intent")


def test_spawn_mark_uncertain_preserves_identity_and_refuses_reopening(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    journal.spawn_owned(handle, spawn()["identity"])
    journal.mark_uncertain(handle)
    row = load(state).inventory().spawns[SID]
    assert row["state"] == "uncertain" and row["identity"] == spawn()["identity"]
    with pytest.raises(RecoveryError):
        journal.spawn_closed(handle, spawn()["identity"], cleanup_proven=True)
    with pytest.raises(RecoveryError):
        journal.spawn_owned(handle, spawn()["identity"])


def test_spawn_close_requires_exact_identity_and_cleanup_proof(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    with pytest.raises(RecoveryError):
        journal.spawn_closed(handle, identity, cleanup_proven=False)
    assert load(state).inventory().spawns[SID]["state"] == "owned"
    journal.spawn_closed(handle, identity, cleanup_proven=True)
    with pytest.raises(RecoveryError):
        journal.spawn_owned(handle, identity)
    with pytest.raises(RecoveryError):
        journal.spawn_closed(handle, identity, cleanup_proven=True)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(journal._active_ticket, SID, "candidate")


def test_valid_close_missing_spawn_record_latches_even_with_proof_history(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    (state / "recovery/spawns" / (SID + ".json")).unlink()

    with pytest.raises(RecoveryError):
        journal.spawn_closed(handle, identity, cleanup_proven=True)
    assert journal.uncertain is True
    assert SID not in load(state).inventory().spawns
    with pytest.raises(RecoveryError):
        journal.spawn_intent(journal._active_ticket, "e" * 32, "candidate")


def test_valid_close_unreadable_spawn_record_latches(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    spawn_path = state / "recovery/spawns" / (SID + ".json")
    unreadable = False

    def read_json(path):
        if unreadable and path == spawn_path:
            raise OSError("injected read failure")
        return engine._read_json(path)

    journal._read_json = read_json
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    unreadable = True
    with pytest.raises(RecoveryError):
        journal.spawn_closed(handle, identity, cleanup_proven=True)
    unreadable = False
    assert journal.uncertain is True
    assert load(state).inventory().spawns[SID]["state"] == "owned"
    with pytest.raises(RecoveryError):
        journal.spawn_intent(journal._active_ticket, "e" * 32, "candidate")


def test_valid_close_lock_timeout_latches_uncertainty(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.monotonic()
        with pytest.raises(RecoveryError):
            journal.spawn_closed(handle, identity, cleanup_proven=True)
        assert time.monotonic() - start < 1.5
    finally:
        os.close(fd)
    assert journal.uncertain is True
    assert load(state).inventory().spawns[SID]["state"] == "owned"
    with pytest.raises(RecoveryError):
        journal.spawn_intent(journal._active_ticket, "e" * 32, "candidate")


def test_invalid_close_handle_does_not_poison_valid_store(tmp_path):
    from dataclasses import replace
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    with pytest.raises(RecoveryError):
        journal.spawn_closed(replace(handle), identity, cleanup_proven=True)
    with pytest.raises(RecoveryError):
        load(state).spawn_closed(handle, identity, cleanup_proven=True)
    assert journal.uncertain is False
    journal.spawn_closed(handle, identity, cleanup_proven=True)
    assert load(state).inventory().spawns[SID]["state"] == "closed"


def test_reentrant_uncertainty_during_close_remains_sticky(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = None

    def atomic_with_reentrant_mark(path, row):
        if row["state"] == "closed":
            with pytest.raises(RecoveryError):
                journal.mark_uncertain(handle)
        engine.atomic_json(path, row)

    journal._atomic_json = atomic_with_reentrant_mark
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    with pytest.raises(RecoveryError):
        journal.spawn_closed(handle, identity, cleanup_proven=True)
    assert journal.uncertain is True
    assert load(state).inventory().spawns[SID]["state"] == "closed"
    with pytest.raises(RecoveryError):
        journal.spawn_intent(journal._active_ticket, "e" * 32, "candidate")


@pytest.mark.parametrize("field,value", [("pid", 12.0), ("uid", True),
                                            ("pgid", 12.0), ("birth", [True, 0])])
def test_spawn_close_rejects_equal_numeric_lookalikes(tmp_path, field, value):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    with pytest.raises(RecoveryError):
        journal.spawn_closed(handle, {**identity, field: value}, cleanup_proven=True)
    assert load(state).inventory().spawns[SID]["state"] == "owned"


def test_spawn_owned_invalid_observation_sticks_uncertainty(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    with pytest.raises(RecoveryError):
        journal.spawn_owned(handle, {**spawn()["identity"], "pid": True})
    assert journal.uncertain is True
    assert load(state).inventory().spawns[SID]["state"] == "intent"


def test_spawn_owned_row_state_failure_sticks_uncertainty(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    with pytest.raises(RecoveryError):
        journal.spawn_owned(handle, identity)
    assert journal.uncertain is True
    assert load(state).inventory().spawns[SID]["state"] == "owned"
    with pytest.raises(RecoveryError):
        journal.spawn_intent(journal._active_ticket, "e" * 32, "candidate")


def test_invalid_spawn_owned_handle_does_not_poison_valid_handle(tmp_path):
    from dataclasses import replace
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    with pytest.raises(RecoveryError):
        journal.spawn_owned(replace(handle), spawn()["identity"])
    with pytest.raises(RecoveryError):
        load(state).spawn_owned(handle, spawn()["identity"])
    assert journal.uncertain is False
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    journal.spawn_closed(handle, identity, cleanup_proven=True)
    assert load(state).inventory().spawns[SID]["state"] == "closed"


def test_mark_uncertain_sticks_before_missing_row_read(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    (state / "recovery/spawns" / (SID + ".json")).unlink()
    with pytest.raises(RecoveryError):
        journal.mark_uncertain(handle)
    assert journal.uncertain is True


@pytest.mark.parametrize("phase,outcome", [("prepared", "prepared"),
                                            ("restarted", "restarted")])
def test_terminal_or_prepared_operation_refuses_child_intent(tmp_path, phase, outcome):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    terminal = operation()
    terminal.update(phase=phase, outcome=outcome)
    engine.atomic_json(state / "recovery/operations" / (RID + ".json"), terminal)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(journal._active_ticket, SID, "candidate")
    assert not (state / "recovery/spawns" / (SID + ".json")).exists()


@pytest.mark.parametrize("phase,kind,purpose", [
    ("prepared", "proxy", "candidate"),
    ("blocked", "proxy", "candidate"),
    ("restarted", "proxy", "candidate"),
    ("rolled_back", "proxy", "candidate"),
    ("manual_recovery_required", "proxy", "candidate"),
    ("shutdown_pending", "proxy", "candidate"),
    ("canary_passed", "platform-canary", "canary-query"),
    ("canary_blocked", "platform-canary", "canary-query"),
])
def test_null_outcome_terminal_or_stopped_operation_refuses_child_intent(
        tmp_path, phase, kind, purpose):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    row = operation()
    row.update(binding=binding(kind, frozen=kind == "proxy"), phase=phase,
               outcome=None)
    assert journal.validate_operation(row) == row
    engine.atomic_json(state / "recovery/operations" / (RID + ".json"), row)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(journal._active_ticket, SID, purpose)
    assert not (state / "recovery/spawns" / (SID + ".json")).exists()


def test_active_null_outcome_operation_allows_child_intent(tmp_path):
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    assert handle.spawn_id == SID
    assert load(state).inventory().spawns[SID]["state"] == "intent"


@pytest.mark.parametrize("point", ["file_sync", "before_replace", "after_replace",
                                   "directory_sync"])
def test_spawn_intent_atomic_fault_preserves_pending_precursor(tmp_path, monkeypatch, point):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    original_sync, original_replace = engine._sync_fd, os.replace
    sync_calls = 0

    def sync_fault(fd):
        nonlocal sync_calls
        sync_calls += 1
        if (point == "file_sync" and sync_calls == 1) or (
                point == "directory_sync" and sync_calls == 2):
            raise OSError("injected")
        original_sync(fd)

    def replace_fault(src, dst):
        if point == "before_replace":
            raise OSError("injected")
        original_replace(src, dst)
        if point == "after_replace":
            raise OSError("injected")

    monkeypatch.setattr(engine, "_sync_fd", sync_fault)
    monkeypatch.setattr(os, "replace", replace_fault)
    called = []
    with pytest.raises(RecoveryError):
        handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
        called.append(handle)
    assert called == []
    assert journal.uncertain is True
    fresh = load(state).inventory()
    assert fresh.operations[RID]["outcome"] is None
    assert fresh.operations[RID]["phase"] == "preparing"
    if point in ("file_sync", "before_replace"):
        assert SID not in fresh.spawns
    else:
        assert fresh.spawns[SID]["state"] == "intent"


def test_directory_fd_lock_excludes_distinct_descriptors(tmp_path):
    root = tmp_path / "private"
    engine.private_dir(root)
    first = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    second = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(first, fcntl.LOCK_UN)
        fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(first)
        os.close(second)


def test_spawn_lock_contention_refuses_within_bound(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.monotonic()
        with pytest.raises(RecoveryError):
            journal.spawn_intent(journal._active_ticket, SID, "candidate")
        assert time.monotonic() - start < 1.5
    finally:
        os.close(fd)
    assert SID not in load(state).inventory().spawns


def test_spawn_owned_lock_contention_sticks_uncertainty(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.monotonic()
        with pytest.raises(RecoveryError):
            journal.spawn_owned(handle, spawn()["identity"])
        assert time.monotonic() - start < 1.5
    finally:
        os.close(fd)
    assert journal.uncertain is True
    assert load(state).inventory().spawns[SID]["state"] == "intent"
    with pytest.raises(RecoveryError):
        journal.spawn_intent(journal._active_ticket, "e" * 32, "candidate")


def test_reentrant_uncertainty_cannot_be_swallowed_by_identity_write(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    handle = None

    def atomic_with_reentrant_mark(path, row):
        if row["state"] == "owned":
            with pytest.raises(RecoveryError):
                journal.mark_uncertain(handle)
        engine.atomic_json(path, row)

    journal._atomic_json = atomic_with_reentrant_mark
    handle = journal.spawn_intent(journal._active_ticket, SID, "candidate")
    with pytest.raises(RecoveryError):
        journal.spawn_owned(handle, spawn()["identity"])
    assert journal.uncertain is True
    assert load(state).inventory().spawns[SID]["identity"] == spawn()["identity"]


@pytest.mark.parametrize("distinct_stores", [False, True])
def test_same_spawn_id_transition_is_serialized_and_closed_record_stays_closed(tmp_path,
                                                                                 distinct_stores):
    from core.restart_control.recovery import RecoveryError
    journal, state = _spawn_ready(tmp_path)
    entered = threading.Event()
    second_done = threading.Event()
    outcomes = []
    errors = []
    original_atomic = engine.atomic_json

    def delayed_first(path, row):
        if row.get("state") == "intent" and threading.current_thread().name == "first":
            entered.set()
            second_done.wait(0.2)
        original_atomic(path, row)

    first_store = journal
    first_store._atomic_json = delayed_first
    second_store = load(state) if distinct_stores else first_store
    first_ticket = first_store._active_ticket
    second_ticket = second_store.operation_ticket(RID) if distinct_stores else first_ticket

    def run(store, ticket, tag):
        try:
            handle = store.spawn_intent(ticket, SID, "candidate")
            store.spawn_owned(handle, spawn()["identity"])
            store.spawn_closed(handle, spawn()["identity"], cleanup_proven=True)
            outcomes.append(tag)
        except RecoveryError:
            errors.append(tag)
        finally:
            if tag == "second":
                second_done.set()

    first = threading.Thread(target=run, args=(first_store, first_ticket, "first"),
                             name="first", daemon=True)
    second = threading.Thread(target=run, args=(second_store, second_ticket, "second"),
                              name="second", daemon=True)
    first.start()
    assert entered.wait(1)
    second.start()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert len(outcomes) == 1 and len(errors) == 1
    assert load(state).inventory().spawns[SID]["state"] == "closed"


def _mutation_ready(tmp_path):
    journal, state = _spawn_ready(tmp_path)
    journal.end_operation(journal._active_ticket)
    prepared = operation()
    prepared.update(phase="prepared", outcome="prepared")
    engine.atomic_json(state / "recovery/operations" / (RID + ".json"), prepared)
    _prepared_records(state)
    return journal, state, journal.operation_ticket(RID)


@pytest.mark.parametrize("defect", ["request_missing", "approval_missing", "consumed",
                                    "request_field", "binding", "approval_binding",
                                    "scope", "wrong_phase"])
def test_reopen_requires_complete_prepared_request_and_unconsumed_approval(tmp_path, defect):
    from core.restart_control.recovery import RecoveryError
    journal, state, ticket = _mutation_ready(tmp_path)
    request_path = state / "requests" / (RID + ".json")
    approval_path = state / "approvals" / (RID + ".json")
    plan = engine._read_json(request_path)
    approval = engine._read_json(approval_path)
    if defect == "request_missing":
        request_path.unlink()
    elif defect == "approval_missing":
        approval_path.unlink()
    elif defect == "consumed":
        approval_path.rename(approval_path.with_suffix(".consumed"))
    elif defect == "request_field":
        del plan["primary_checks"]
        engine.atomic_json(request_path, plan)
    elif defect == "binding":
        plan["candidate_binding"]["runtime_digest"] = "d" * 64
        engine.atomic_json(request_path, plan)
    elif defect == "approval_binding":
        approval["label"] = "other"
        engine.atomic_json(approval_path, approval)
    elif defect == "scope":
        plan["scope"] = "production"
        engine.atomic_json(request_path, plan)
    else:
        plan["phase"] = "restarted"
        engine.atomic_json(request_path, plan)
    with pytest.raises(RecoveryError):
        journal.reopen_restart(ticket)
    assert load(state).inventory().operations[RID]["phase"] == "prepared"
    assert journal.uncertain is False


def test_reopen_returns_refreshed_ticket_and_refuses_replay(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state, old = _mutation_ready(tmp_path)
    refreshed = journal.reopen_restart(old)
    assert refreshed is not old
    assert load(state).inventory().operations[RID]["phase"] == "restart_pending"
    with pytest.raises(RecoveryError):
        journal.reopen_restart(old)
    with pytest.raises(RecoveryError):
        journal.reopen_restart(refreshed)


def test_mutation_pending_and_fence_are_durable_before_outside_control(tmp_path):
    from core.restart_control.shutdown import Shutdown, ShutdownRequested
    journal, state, ticket = _mutation_ready(tmp_path)
    ticket = journal.reopen_restart(ticket)
    assert load(state).inventory().operations[RID]["phase"] == "restart_pending"
    assert load(state).inventory().operations[RID]["outcome"] is None
    ticket = journal.arm_mutation(ticket)
    assert load(state).inventory().fence == {
        "version": 1, "binding": binding(), "phase": "restart_pending",
        "state": "armed"}
    _consume_approval(state)
    assert load(state).inventory().approvals[RID][0] is True

    controls = []
    shutdown = Shutdown()
    shutdown.request()
    with pytest.raises(ShutdownRequested):
        shutdown.commit("service-stop")
        controls.append("stop")
    assert controls == []
    assert load(state).inventory().fence["state"] == "armed"


def _prepared_records(state):
    identity = {"pid": 12, "uid": os.getuid(), "birth": [1, 0]}
    plan = {"request_id": RID, "phase": "prepared", "scope": "fixture",
            "shared_state_side_effects": False,
            "controller_epoch": SID, "uid": os.getuid(), "label": "fixture",
            "original_identity": identity, "asset_digest": DIGEST,
            "runtime_digest": DIGEST, "policy_digest": DIGEST,
            "controller_digest": DIGEST,
            "candidate_binding": {"asset_digest": DIGEST, "runtime_digest": DIGEST},
            "lkg_binding": {"asset_digest": DIGEST, "runtime_digest": DIGEST},
            "primary_checks": {}, "candidate_checks": {},
            "prepared_at": 1.0, "prepared_monotonic": 1.0,
            "expires_at": 301.0, "expires_monotonic": 301.0}
    approval = {"version": 1, "owner_uid": os.getuid(),
                **engine.approval_binding(plan), "issued_at": 2.0,
                "issued_monotonic": 2.0, "expires_at": 300.0,
                "expires_monotonic": 300.0}
    engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    engine.atomic_json(state / "approvals" / (RID + ".json"), approval)
    return plan, approval


def _accepted_records(state, terminal="restarted"):
    plan, approval = _prepared_records(state)
    plan["phase"] = terminal
    plan["started" if terminal == "restarted" else "rollback"] = {
        "identity": {"pid": 13, "uid": os.getuid(), "birth": [2, 0]},
        "checks": {"accepted": True}}
    engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    (state / "approvals" / (RID + ".json")).unlink(missing_ok=True)
    engine.atomic_json(state / "approvals" / (RID + ".consumed"), approval)
    return plan, approval


def _consume_approval(state):
    approvals = state / "approvals"
    os.replace(approvals / (RID + ".json"), approvals / (RID + ".consumed"))
    fd = os.open(approvals, os.O_RDONLY | os.O_DIRECTORY)
    try:
        engine._sync_fd(fd)
    finally:
        os.close(fd)


def _request_phase(state, phase):
    path = state / "requests" / (RID + ".json")
    plan = engine._read_json(path)
    plan["phase"] = phase
    engine.atomic_json(path, plan)


def _postchecking(tmp_path, terminal="restarted"):
    from core.restart_control.shutdown import Shutdown
    journal, state, ticket = _mutation_ready(tmp_path)
    ticket = journal.arm_mutation(journal.reopen_restart(ticket))
    _consume_approval(state)
    _request_phase(state, "intent_consumed")
    ticket = journal.phase(ticket, "intent_consumed")
    shutdown = Shutdown()
    permit = shutdown.commit("service-stop")
    ticket = journal.phase(ticket, "stopping", committed_action=(shutdown, permit))
    _request_phase(state, "stopping")
    ticket = journal.phase(ticket, "starting")
    _request_phase(state, "starting")
    ticket = journal.phase(ticket, "postchecking")
    _request_phase(state, "postchecking")
    if terminal == "rolled_back":
        for phase in ("rollback_stopping", "rollback_restoring",
                      "rollback_starting", "rollback_postchecking"):
            ticket = journal.phase(ticket, phase)
            _request_phase(state, phase)
    return journal, state, ticket


@pytest.mark.parametrize("terminal", ["restarted", "rolled_back"])
def test_manual_phase_preserves_request_first_accepted_terminal(tmp_path, terminal):
    from core.restart_control.recovery import RecoveryError

    journal, state, ticket = _postchecking(tmp_path, terminal)
    accepted, _ = _accepted_records(state, terminal)
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        journal.phase(ticket, "manual_recovery_required")
    assert tree_bytes(state) == before
    assert journal.uncertain is False
    assert journal.inventory().requests[RID] == accepted


def _manual_failure_ready(tmp_path, journal_phase):
    from core.restart_control.shutdown import Shutdown

    journal, state, ticket = _mutation_ready(tmp_path)
    ticket = journal.arm_mutation(journal.reopen_restart(ticket))
    _consume_approval(state)
    phases = ("intent_consumed", "stopping", "starting", "postchecking",
              "rollback_stopping", "rollback_restoring", "rollback_starting",
              "rollback_postchecking")
    if journal_phase == "restart_pending":
        return journal, state, ticket
    shutdown = Shutdown()
    for phase in phases:
        _request_phase(state, phase)
        if phase == "stopping":
            ticket = journal.phase(ticket, phase, committed_action=(
                shutdown, shutdown.commit("service-stop")))
        else:
            ticket = journal.phase(ticket, phase)
        if phase == journal_phase:
            return journal, state, ticket
    raise AssertionError(journal_phase)


@pytest.mark.parametrize("purpose", ["candidate", "diagnostic", "cleanup-diagnostic"])
def test_request_first_manual_failure_bars_new_intents(tmp_path, purpose):
    from core.restart_control.recovery import RecoveryError

    journal, state, ticket = _manual_failure_ready(tmp_path, "starting")
    path = state / "requests" / (RID + ".json")
    plan = engine._read_json(path)
    plan.update(phase="manual_recovery_required", code="internal_error",
                failed_phase="starting")
    engine.atomic_json(path, plan)
    assert journal.require_clear(ticket) is None  # Accounting admits the failure pair.
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(ticket, SID, purpose)
    assert tree_bytes(state) == before


def test_request_first_manual_failure_preserves_issued_child_cleanup(tmp_path):
    from core.restart_control.recovery import RecoveryError

    journal, state, ticket = _manual_failure_ready(tmp_path, "starting")
    handle = journal.spawn_intent(ticket, SID, "candidate")
    identity = spawn()["identity"]
    journal.spawn_owned(handle, identity)
    path = state / "requests" / (RID + ".json")
    plan = engine._read_json(path)
    plan.update(phase="manual_recovery_required", code="internal_error",
                failed_phase="starting")
    engine.atomic_json(path, plan)
    assert journal.require_clear(ticket) is None
    with pytest.raises(RecoveryError):
        journal.spawn_intent(ticket, "d" * 32, "cleanup-diagnostic")
    journal.spawn_closed(handle, identity, cleanup_proven=True)
    assert journal.inventory().spawns[SID]["state"] == "closed"


@pytest.mark.parametrize("journal_phase,target,with_permit", [
    ("intent_consumed", "stopping", False),
    ("intent_consumed", "stopping", True),
    ("stopping", "starting", False),
    ("starting", "postchecking", False),
])
def test_manual_request_refuses_normal_phase_before_any_claim_or_write(
        tmp_path, journal_phase, target, with_permit):
    from core.restart_control.recovery import RecoveryError
    from core.restart_control.shutdown import Shutdown

    journal, state, ticket = _manual_failure_ready(tmp_path, journal_phase)
    path = state / "requests" / (RID + ".json")
    plan = engine._read_json(path)
    plan.update(phase="manual_recovery_required", code="internal_error",
                failed_phase=plan["phase"])
    engine.atomic_json(path, plan)
    assert journal.require_clear(ticket) is None
    shutdown = Shutdown()
    permit = shutdown.commit("service-stop") if with_permit else None
    before = tree_bytes(state)
    action = (shutdown, permit) if with_permit else None
    with pytest.raises(RecoveryError):
        journal.phase(ticket, target, committed_action=action)
    assert tree_bytes(state) == before
    assert journal.uncertain is False
    if with_permit:
        assert shutdown.claim(permit) == "service-stop"


@pytest.mark.parametrize("journal_phase,target", [
    ("stopping", "starting"),
    ("starting", "postchecking"),
])
def test_rollback_request_refuses_normal_forward_phase_before_write(
        tmp_path, journal_phase, target):
    from core.restart_control.recovery import RecoveryError

    journal, state, ticket = _manual_failure_ready(tmp_path, journal_phase)
    _request_phase(state, "rollback_stopping")
    assert journal.require_clear(ticket) is None
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        journal.phase(ticket, target)
    assert tree_bytes(state) == before
    assert journal.uncertain is False


@pytest.mark.parametrize("journal_phase", ["stopping", "starting", "postchecking"])
def test_rollback_request_requires_journal_catchup_before_new_child_intent(
        tmp_path, journal_phase):
    from core.restart_control.recovery import RecoveryError

    journal, state, ticket = _manual_failure_ready(tmp_path, journal_phase)
    _request_phase(state, "rollback_stopping")
    assert journal.require_clear(ticket) is None
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(ticket, SID, "candidate")
    assert tree_bytes(state) == before


@pytest.mark.parametrize("journal_phase,target,request_phase", [
    ("stopping", "starting", "stopping"),
    ("stopping", "starting", "starting"),
    ("starting", "postchecking", "starting"),
    ("starting", "postchecking", "postchecking"),
    ("stopping", "rollback_stopping", "rollback_stopping"),
    ("starting", "rollback_stopping", "rollback_stopping"),
    ("postchecking", "rollback_stopping", "rollback_stopping"),
])
def test_joint_phase_direction_keeps_defined_forward_and_rollback_catchup(
        tmp_path, journal_phase, target, request_phase):
    journal, state, ticket = _manual_failure_ready(tmp_path, journal_phase)
    _request_phase(state, request_phase)
    assert journal.require_clear(ticket) is None
    ticket = journal.phase(ticket, target)
    assert journal.inventory().operations[RID]["phase"] == target
    assert journal.require_clear(ticket) is None


@pytest.mark.parametrize("defect", ["bad_code", "wrong_failed_phase", "extra_key"])
def test_malformed_request_first_manual_failure_refuses_admission(tmp_path, defect):
    from core.restart_control.recovery import RecoveryError

    journal, state, ticket = _manual_failure_ready(tmp_path, "starting")
    path = state / "requests" / (RID + ".json")
    plan = engine._read_json(path)
    plan.update(phase="manual_recovery_required", code="internal_error",
                failed_phase="starting")
    if defect == "bad_code":
        plan["code"] = "not_a_code"
    elif defect == "wrong_failed_phase":
        plan["failed_phase"] = "rollback_postchecking"
    else:
        plan["extra"] = True
    engine.atomic_json(path, plan)
    with pytest.raises(RecoveryError):
        journal.require_clear(ticket)
    with pytest.raises(RecoveryError):
        journal.spawn_intent(ticket, SID, "candidate")


@pytest.mark.parametrize("journal_phase", [
    "restart_pending", "intent_consumed", "stopping", "starting",
    "postchecking", "rollback_stopping", "rollback_restoring",
    "rollback_starting", "rollback_postchecking"])
@pytest.mark.parametrize("request_order", ["current", "manual"])
def test_manual_phase_accepts_defined_failure_order_and_retains_fence(
        tmp_path, journal_phase, request_order):
    from core.restart_control.recovery import RecoveryError

    journal, state, ticket = _manual_failure_ready(tmp_path, journal_phase)
    before = journal.inventory().operations[RID]
    if request_order == "manual":
        plan = engine._read_json(state / "requests" / (RID + ".json"))
        failed_phase = plan["phase"]
        plan.update(phase="manual_recovery_required", code="internal_error",
                    failed_phase=failed_phase)
        engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    ticket = journal.phase(ticket, "manual_recovery_required")
    view = journal.inventory()
    assert view.operations[RID]["phase"] == "manual_recovery_required"
    assert view.operations[RID]["outcome"] is None
    assert view.operations[RID]["primary_control_committed"] is before["primary_control_committed"]
    assert view.fence["phase"] == "manual_recovery_required"
    assert view.fence["state"] == "uncertain"
    assert view.approvals[RID][0] is True
    with pytest.raises(RecoveryError):
        journal.require_clear(ticket)
    with pytest.raises(RecoveryError):
        load(state).require_clear()
    with pytest.raises(RecoveryError):
        journal.spawn_intent(ticket, "d" * 32, "diagnostic")
    with pytest.raises(RecoveryError):
        journal.reopen_restart(ticket)


@pytest.mark.parametrize("journal_phase,request_phase", [
    ("restart_pending", "intent_consumed"),
    ("intent_consumed", "prepared"),
    ("intent_consumed", "stopping"),
    ("stopping", "intent_consumed"),
    ("stopping", "starting"),
    ("stopping", "rollback_stopping"),
    ("starting", "stopping"),
    ("starting", "postchecking"),
    ("starting", "rollback_stopping"),
    ("postchecking", "starting"),
    ("postchecking", "rollback_stopping"),
    ("rollback_stopping", "stopping"),
    ("rollback_stopping", "starting"),
    ("rollback_stopping", "postchecking"),
    ("rollback_stopping", "rollback_restoring"),
    ("rollback_restoring", "rollback_stopping"),
    ("rollback_restoring", "rollback_starting"),
    ("rollback_starting", "rollback_restoring"),
    ("rollback_starting", "rollback_postchecking"),
    ("rollback_postchecking", "rollback_starting"),
])
def test_manual_phase_allows_other_defined_nonterminal_failure_pairs(
        tmp_path, journal_phase, request_phase):
    journal, state, ticket = _manual_failure_ready(tmp_path, journal_phase)
    _request_phase(state, request_phase)
    ticket = journal.phase(ticket, "manual_recovery_required")
    view = journal.inventory()
    assert view.requests[RID]["phase"] == request_phase
    assert view.operations[RID]["phase"] == "manual_recovery_required"
    assert view.fence["state"] == "uncertain"


@pytest.mark.parametrize("defect", ["wrong_failed_phase", "bad_code",
                                    "wrong_binding", "wrong_fence_binding",
                                    "unconsumed_approval", "approval_binding",
                                    "wrong_request_phase"])
def test_manual_phase_rejects_invalid_projected_failure_without_write(tmp_path, defect):
    from core.restart_control.recovery import RecoveryError

    journal, state, ticket = _manual_failure_ready(tmp_path, "starting")
    request_path = state / "requests" / (RID + ".json")
    plan = engine._read_json(request_path)
    if defect == "wrong_binding":
        plan["controller_digest"] = "d" * 64
    elif defect == "wrong_fence_binding":
        fence_path = state / "recovery/mutation.json"
        fenced = engine._read_json(fence_path)
        fenced["binding"]["controller_epoch"] = "d" * 32
        engine.atomic_json(fence_path, fenced)
    elif defect == "unconsumed_approval":
        approval_path = state / "approvals" / (RID + ".consumed")
        approval_path.rename(state / "approvals" / (RID + ".json"))
    elif defect == "approval_binding":
        approval_path = state / "approvals" / (RID + ".consumed")
        approval = engine._read_json(approval_path)
        approval["controller_digest"] = "d" * 64
        engine.atomic_json(approval_path, approval)
    elif defect == "wrong_request_phase":
        plan["phase"] = "rollback_postchecking"
    else:
        plan.update(phase="manual_recovery_required", code="internal_error",
                    failed_phase="starting")
        if defect == "wrong_failed_phase":
            plan["failed_phase"] = "rollback_postchecking"
        else:
            plan["code"] = "not_a_code"
    engine.atomic_json(request_path, plan)
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        journal.phase(ticket, "manual_recovery_required")
    assert tree_bytes(state) == before
    assert journal.uncertain is False


def test_starting_same_phase_edge_refuses_without_writing(tmp_path):
    from core.restart_control.recovery import RecoveryError

    journal, state, ticket = _manual_failure_ready(tmp_path, "starting")
    view = journal.inventory()
    assert view.operations[RID]["phase"] == "starting"
    assert view.operations[RID]["primary_control_committed"] is True
    assert view.requests[RID]["phase"] == "starting"
    assert journal.require_clear(ticket) is None

    paths = (state / "recovery/operations" / (RID + ".json"),
             state / "requests" / (RID + ".json"),
             state / "recovery/mutation.json")
    before = tuple(path.read_bytes() for path in paths)
    with pytest.raises(RecoveryError):
        journal.phase(ticket, "starting")
    assert tuple(path.read_bytes() for path in paths) == before
    assert journal.require_clear(ticket) is None

    ticket = journal.phase(ticket, "postchecking")
    _request_phase(state, "postchecking")
    assert journal.inventory().operations[RID]["phase"] == "postchecking"
    assert journal.require_clear(ticket) is None


def test_spec_r4_phase_refuses_regression_and_jump_without_consuming_permit(tmp_path):
    from core.restart_control.recovery import RecoveryError
    from core.restart_control.shutdown import Shutdown

    journal, state, ticket = _mutation_ready(tmp_path)
    ticket = journal.arm_mutation(journal.reopen_restart(ticket))
    _consume_approval(state)
    _request_phase(state, "intent_consumed")
    ticket = journal.phase(ticket, "intent_consumed")
    shutdown = Shutdown()
    permit = shutdown.commit("service-stop")
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        journal.phase(ticket, "rollback_stopping", committed_action=(shutdown, permit))
    assert tree_bytes(state) == before
    assert shutdown.claim(permit) == "service-stop"

    ticket = journal.phase(ticket, "stopping", committed_action=(shutdown,
                           shutdown.commit("service-stop")))
    _request_phase(state, "stopping")
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        journal.phase(ticket, "intent_consumed")
    assert tree_bytes(state) == before


@pytest.mark.parametrize("journal_phase", [
    "starting", "postchecking", "rollback_stopping"])
def test_projected_advanced_progress_requires_primary_commit(
        tmp_path, journal_phase):
    from dataclasses import replace
    from core.restart_control.recovery import RecoveryError

    # The public flow records the primary commit before advanced progress.
    journal, state, ticket = _manual_failure_ready(tmp_path, journal_phase)
    view = journal.inventory()
    assert view.operations[RID]["primary_control_committed"] is True
    assert journal.require_clear(ticket) is None

    # Isolate the validator invariant with an adversarial projected row. The
    # live ticket and all request, approval, and fence records stay valid.
    uncommitted = {**view.operations[RID], "primary_control_committed": False}
    projected = replace(view, operations={**view.operations, RID: uncommitted})
    with pytest.raises(RecoveryError):
        journal._require_clear_view(projected, ticket, projected_row=uncommitted)
    assert journal.require_clear(ticket) is None


@pytest.mark.parametrize("journal_phase,request_phase", [
    ("restart_pending", "prepared"),
    ("restart_pending", "intent_consumed"),
    ("intent_consumed", "prepared"),
    ("intent_consumed", "intent_consumed"),
    ("intent_consumed", "stopping"),
])
def test_pre_stopping_projected_commit_refuses_each_valid_request_pair(
        tmp_path, journal_phase, request_phase):
    from dataclasses import replace
    from core.restart_control.recovery import RecoveryError

    journal, state, ticket = _manual_failure_ready(tmp_path, journal_phase)
    _request_phase(state, request_phase)
    view = journal.inventory()
    assert journal._require_clear_view(view, ticket) is None
    contradictory = {**view.operations[RID], "primary_control_committed": True}
    projected = replace(view, operations={**view.operations, RID: contradictory})
    with pytest.raises(RecoveryError):
        journal._require_clear_view(projected, ticket, projected_row=contradictory)


@pytest.mark.parametrize("branch", ["preparing", "fenced"])
@pytest.mark.parametrize("value", ["missing", None, [], 0])
def test_spec_r4_malformed_active_request_refuses_closed(tmp_path, branch, value):
    from core.restart_control.recovery import RecoveryError

    if branch == "preparing":
        journal = store(tmp_path)
        ticket = journal.begin(binding(frozen=False), "fixture")
    else:
        journal, state, ticket = _mutation_ready(tmp_path)
        ticket = journal.arm_mutation(journal.reopen_restart(ticket))
        _consume_approval(state)
    path = tmp_path / "state/requests" / (RID + ".json")
    if value == "missing":
        path.unlink(missing_ok=True)
    else:
        engine.atomic_json(path, value)
    if branch == "preparing" and value == "missing":
        assert journal.require_clear(ticket) is None
        return
    with pytest.raises(RecoveryError) as error:
        journal._require_clear_view(journal.inventory(), ticket)
    assert error.value.code == "recovery_invalid"


def test_spec_r4_empty_engine_backend_label_stages_and_reopens(tmp_path):
    journal = store(tmp_path)
    ticket = _begin_and_freeze(journal, tmp_path / "state")
    state = tmp_path / "state"
    plan, approval = _prepared_records(state)
    plan["label"] = ""
    approval["label"] = ""
    engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    engine.atomic_json(state / "approvals" / (RID + ".json"), approval)
    journal.finish(ticket)
    ticket = journal.reopen_restart(journal.operation_ticket(RID))
    assert journal.require_clear(ticket) is None


def test_spec_r4_preaction_stopping_can_record_first_commit_in_same_phase(tmp_path):
    from core.restart_control.shutdown import Shutdown

    journal, state, ticket = _mutation_ready(tmp_path)
    ticket = journal.arm_mutation(journal.reopen_restart(ticket))
    _consume_approval(state)
    _request_phase(state, "intent_consumed")
    ticket = journal.phase(ticket, "intent_consumed")
    ticket = journal.phase(ticket, "stopping")
    _request_phase(state, "stopping")
    assert journal.require_clear(ticket) is None
    assert journal.inventory().operations[RID]["primary_control_committed"] is False
    shutdown = Shutdown()
    ticket = journal.phase(ticket, "stopping", committed_action=(
        shutdown, shutdown.commit("service-stop")))
    assert journal.inventory().operations[RID]["primary_control_committed"] is True
    assert journal.require_clear(ticket) is None


def test_spec_r4_first_commit_cannot_skip_stop_with_start_permit(tmp_path):
    from core.restart_control.recovery import RecoveryError
    from core.restart_control.shutdown import Shutdown

    journal, state, ticket = _mutation_ready(tmp_path)
    ticket = journal.arm_mutation(journal.reopen_restart(ticket))
    _consume_approval(state)
    _request_phase(state, "intent_consumed")
    ticket = journal.phase(ticket, "intent_consumed")
    ticket = journal.phase(ticket, "stopping")
    _request_phase(state, "stopping")
    shutdown = Shutdown()
    permit = shutdown.commit("service-start")
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        journal.phase(ticket, "starting", committed_action=(shutdown, permit))
    assert tree_bytes(state) == before
    assert shutdown.claim(permit) == "service-start"


@pytest.mark.parametrize("from_phase", ["stopping", "starting", "postchecking"])
def test_spec_r4_committed_fault_can_enter_rollback(tmp_path, from_phase):
    from core.restart_control.shutdown import Shutdown

    journal, state, ticket = _mutation_ready(tmp_path)
    ticket = journal.arm_mutation(journal.reopen_restart(ticket))
    _consume_approval(state)
    _request_phase(state, "intent_consumed")
    ticket = journal.phase(ticket, "intent_consumed")
    shutdown = Shutdown()
    ticket = journal.phase(ticket, "stopping", committed_action=(
        shutdown, shutdown.commit("service-stop")))
    if from_phase in ("starting", "postchecking"):
        _request_phase(state, "stopping")
        ticket = journal.phase(ticket, "starting")
    if from_phase == "postchecking":
        _request_phase(state, "starting")
        ticket = journal.phase(ticket, "postchecking")
    _request_phase(state, "rollback_stopping")
    ticket = journal.phase(ticket, "rollback_stopping")
    assert journal.inventory().operations[RID]["phase"] == "rollback_stopping"
    assert journal.require_clear(ticket) is None


@pytest.mark.parametrize("purpose,phase", [("diagnostic", "stopping"),
                                             ("candidate", "stopping"),
                                             ("canary-query", "stopping"),
                                             ("service-start", "stopping"),
                                             ("service-stop", "starting")])
def test_phase_refuses_nonprimary_or_wrong_phase_permit_before_write(tmp_path,
                                                                    purpose, phase):
    from core.restart_control.recovery import RecoveryError
    from core.restart_control.shutdown import Shutdown
    (tmp_path / "control").mkdir()
    control, control_state, control_ticket = _manual_failure_ready(
        tmp_path / "control", "intent_consumed")
    control_ticket = control.phase(control_ticket, "stopping")
    _request_phase(control_state, "stopping")
    assert control.require_clear(control_ticket) is None
    control_shutdown = Shutdown()
    control_ticket = control.phase(control_ticket, "stopping", committed_action=(
        control_shutdown, control_shutdown.commit("service-stop")))
    assert control.inventory().operations[RID]["primary_control_committed"] is True
    assert control.require_clear(control_ticket) is None

    (tmp_path / "negative").mkdir()
    journal, state, ticket = _manual_failure_ready(
        tmp_path / "negative", "intent_consumed")
    ticket = journal.phase(ticket, "stopping")
    _request_phase(state, "stopping")
    assert journal.require_clear(ticket) is None
    shutdown = Shutdown()
    permit = shutdown.commit(purpose)
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        journal.phase(ticket, phase, committed_action=(shutdown, permit))
    assert tree_bytes(state) == before
    assert load(state).inventory().operations[RID]["primary_control_committed"] is False
    assert shutdown.claim(permit) == purpose


def test_phase_refuses_terminal_request_before_claiming_stop_permit(tmp_path):
    from core.restart_control.recovery import RecoveryError
    from core.restart_control.shutdown import Shutdown

    journal, state, ticket = _mutation_ready(tmp_path)
    ticket = journal.arm_mutation(journal.reopen_restart(ticket))
    _accepted_records(state)
    shutdown = Shutdown()
    permit = shutdown.commit("service-stop")
    before = tree_bytes(state)
    with pytest.raises(RecoveryError):
        journal.phase(ticket, "stopping", committed_action=(shutdown, permit))
    assert tree_bytes(state) == before
    assert shutdown.claim(permit) == "service-stop"


def test_phase_primary_commit_once_then_progress_without_new_attestation(tmp_path):
    from core.restart_control.recovery import RecoveryError
    from core.restart_control.shutdown import Shutdown
    journal, state, ticket = _mutation_ready(tmp_path)
    ticket = journal.arm_mutation(journal.reopen_restart(ticket))
    _consume_approval(state)
    shutdown = Shutdown()
    _request_phase(state, "intent_consumed")
    ticket = journal.phase(ticket, "intent_consumed")
    ticket = journal.phase(ticket, "stopping",
                           committed_action=(shutdown, shutdown.commit("service-stop")))
    _request_phase(state, "stopping")
    assert load(state).inventory().operations[RID]["primary_control_committed"] is True
    second = shutdown.commit("service-start")
    with pytest.raises(RecoveryError):
        journal.phase(ticket, "starting", committed_action=(shutdown, second))
    assert shutdown.claim(second) == "service-start"
    journal.phase(ticket, "starting")
    assert load(state).inventory().operations[RID]["phase"] == "starting"


@pytest.mark.parametrize("terminal", ["restarted", "rolled_back"])
def test_finish_requires_authoritative_accepted_terminal_before_unlink(tmp_path, terminal):
    from core.restart_control.recovery import RecoveryError
    journal, state, ticket = _postchecking(tmp_path, terminal)
    with pytest.raises(RecoveryError):
        journal.finish(ticket)
    assert load(state).inventory().fence is not None
    _accepted_records(state, terminal)
    journal.finish(ticket)
    view = load(state).inventory()
    assert view.fence is None
    assert view.operations[RID]["phase"] == terminal
    assert view.operations[RID]["outcome"] == terminal
    assert view.approvals[RID][0] is True
    with pytest.raises(RecoveryError):
        journal.reopen_restart(ticket)


@pytest.mark.parametrize("defect", ["approval_missing", "approval_wrong", "request_wrong",
                                    "acceptance_missing", "child_open"])
def test_finish_refuses_incomplete_or_mismatched_proof(tmp_path, defect):
    from core.restart_control.recovery import RecoveryError
    journal, state, ticket = _postchecking(tmp_path)
    plan, approval = _accepted_records(state)
    if defect == "approval_missing":
        (state / "approvals" / (RID + ".consumed")).unlink()
    elif defect == "approval_wrong":
        approval["controller_digest"] = "d" * 64
        engine.atomic_json(state / "approvals" / (RID + ".consumed"), approval)
    elif defect == "request_wrong":
        plan["asset_digest"] = "d" * 64
        engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    elif defect == "acceptance_missing":
        del plan["started"]
        engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    else:
        child = spawn()
        child.update(state="intent", identity=None)
        engine.atomic_json(state / "recovery/spawns" / (SID + ".json"), child)
    with pytest.raises(RecoveryError):
        journal.finish(ticket)
    assert load(state).inventory().fence is not None


@pytest.mark.parametrize("defect", ["missing_shared", "wrong_shared", "missing_checks",
                                    "wrong_checks", "wrong_identity", "wrong_lkg",
                                    "extra_field", "malformed_accepted", "wrong_accepted_uid"])
def test_finish_requires_complete_engine_terminal_record(tmp_path, defect):
    from core.restart_control.recovery import RecoveryError
    journal, state, ticket = _postchecking(tmp_path)
    plan, _ = _accepted_records(state)
    if defect == "missing_shared":
        del plan["shared_state_side_effects"]
    elif defect == "wrong_shared":
        plan["shared_state_side_effects"] = True
    elif defect == "missing_checks":
        del plan["candidate_checks"]
    elif defect == "wrong_checks":
        plan["primary_checks"] = []
    elif defect == "wrong_identity":
        plan["original_identity"]["uid"] += 1
    elif defect == "wrong_lkg":
        plan["lkg_binding"]["runtime_digest"] = "d" * 64
    elif defect == "extra_field":
        plan["unexpected"] = True
    elif defect == "malformed_accepted":
        plan["started"]["checks"] = []
    else:
        plan["started"]["identity"]["uid"] += 1
    engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    with pytest.raises(RecoveryError):
        journal.finish(ticket)
    assert load(state).inventory().fence is not None


@pytest.mark.parametrize("clock", ["wall", "mono"])
def test_finish_rejects_approval_duration_over_300_without_freshness_gate(tmp_path, clock):
    from core.restart_control.recovery import RecoveryError
    journal, state, ticket = _postchecking(tmp_path)
    plan, approval = _accepted_records(state)
    key = "expires_at" if clock == "wall" else "expires_monotonic"
    plan[key] = 401.0
    approval[key] = 303.0
    engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    engine.atomic_json(state / "approvals" / (RID + ".consumed"), approval)
    with pytest.raises(RecoveryError):
        journal.finish(ticket)
    assert load(state).inventory().fence is not None


def test_finish_accepts_engine_legacy_empty_checks_and_expired_consumed_approval(tmp_path):
    journal, state, ticket = _postchecking(tmp_path)
    plan, approval = _accepted_records(state)
    plan["started"]["checks"] = {}
    engine.atomic_json(state / "requests" / (RID + ".json"), plan)
    assert approval["expires_at"] < time.time()
    journal.finish(ticket)
    assert load(state).inventory().fence is None


def _fault_store(state, *, atomic_json=engine.atomic_json, sync_fd=engine._sync_fd):
    from core.restart_control.recovery import RecoveryStore
    return RecoveryStore(state, read_json=engine._read_json,
                         atomic_json=atomic_json, private_path=engine._private_path,
                         private_dir=engine.private_dir, sync_fd=sync_fd)


@pytest.mark.parametrize("stage,point", [(stage, point)
    for stage in ("pending", "fence")
    for point in ("file_sync", "before_replace", "after_replace", "directory_sync")])
def test_mutation_atomic_faults_stick_and_never_reach_control(tmp_path, monkeypatch,
                                                               stage, point):
    from core.restart_control.recovery import RecoveryError
    _, state, _ = _mutation_ready(tmp_path)
    journal = _fault_store(state)
    ticket = journal.operation_ticket(RID)
    if stage == "fence":
        ticket = journal.reopen_restart(ticket)
    original_sync, original_replace = engine._sync_fd, os.replace
    sync_calls = 0

    def sync_fault(fd):
        nonlocal sync_calls
        sync_calls += 1
        if (point == "file_sync" and sync_calls == 1) or (
                point == "directory_sync" and sync_calls == 2):
            raise OSError("injected")
        original_sync(fd)

    def replace_fault(src, dst):
        if point == "before_replace":
            raise OSError("injected")
        original_replace(src, dst)
        if point == "after_replace":
            raise OSError("injected")

    monkeypatch.setattr(engine, "_sync_fd", sync_fault)
    monkeypatch.setattr(os, "replace", replace_fault)
    controls = []
    with pytest.raises(RecoveryError):
        if stage == "pending":
            ticket = journal.reopen_restart(ticket)
        else:
            ticket = journal.arm_mutation(ticket)
        controls.append("stop")
    assert controls == [] and journal.uncertain is True
    view = load(state).inventory()
    if stage == "pending":
        assert view.operations[RID]["phase"] in ("prepared", "restart_pending")
        assert view.fence is None
    else:
        assert view.operations[RID]["phase"] == "restart_pending"
        assert (view.fence is not None) == (point in ("after_replace", "directory_sync"))
    with pytest.raises(RecoveryError):
        journal.arm_mutation(ticket)


def test_fence_refuses_duplicate_foreign_and_stale_ticket(tmp_path):
    from core.restart_control.recovery import RecoveryError
    journal, state, old = _mutation_ready(tmp_path)
    pending = journal.reopen_restart(old)
    with pytest.raises(RecoveryError):
        journal.arm_mutation(old)
    journal.arm_mutation(pending)
    with pytest.raises(RecoveryError):
        journal.arm_mutation(pending)
    assert load(state).inventory().fence["binding"] == binding()
    other = operation()
    other["binding"]["request_id"] = "d" * 32
    other.update(phase="restart_pending", outcome=None)
    engine.atomic_json(state / "recovery/operations" / ("d" * 32 + ".json"), other)
    with pytest.raises(RecoveryError):
        journal.arm_mutation(journal.operation_ticket("d" * 32))
    assert load(state).inventory().fence["binding"] == binding()


@pytest.mark.parametrize("kind,frozen", [("platform-canary", False), ("proxy", False)])
def test_fence_refuses_canary_and_null_proxy(tmp_path, kind, frozen):
    from core.restart_control.recovery import RecoveryError
    journal, state, _ = _mutation_ready(tmp_path)
    row = operation()
    row.update(binding=binding(kind, frozen=frozen), phase="preparing", outcome=None)
    if kind == "platform-canary":
        row["phase"] = "canary_running"
    engine.atomic_json(state / "recovery/operations" / (RID + ".json"), row)
    with pytest.raises(RecoveryError):
        journal.arm_mutation(journal.operation_ticket(RID))
    assert not (state / "recovery/mutation.json").exists()


@pytest.mark.parametrize("point", ["before_unlink", "after_unlink", "directory_sync"])
def test_finish_unlink_fault_is_sticky_and_terminal_never_reopens(tmp_path, monkeypatch,
                                                                  point):
    from core.restart_control.recovery import RecoveryError
    journal, state, ticket = _postchecking(tmp_path)
    _accepted_records(state)
    original_unlink = os.unlink
    original_sync = journal._sync_fd

    def unlink_fault(path, *args, **kwargs):
        if Path(path) == state / "recovery/mutation.json":
            if point == "before_unlink":
                raise OSError("injected")
            original_unlink(path, *args, **kwargs)
            if point == "after_unlink":
                raise OSError("injected")
            return
        original_unlink(path, *args, **kwargs)

    def sync_fault(fd):
        if point == "directory_sync":
            raise OSError("injected")
        original_sync(fd)

    monkeypatch.setattr(os, "unlink", unlink_fault)
    journal._sync_fd = sync_fault
    with pytest.raises(RecoveryError):
        journal.finish(ticket)
    assert journal.uncertain is True
    view = load(state).inventory()
    assert view.operations[RID]["outcome"] == "restarted"
    assert (view.fence is not None) == (point == "before_unlink")
    with pytest.raises(RecoveryError):
        journal.reopen_restart(ticket)


@pytest.mark.parametrize("point", ["before_replace", "after_replace", "directory_sync"])
def test_consumed_intent_fault_marks_fence_uncertain_without_control(tmp_path, point):
    from core.restart_control.recovery import RecoveryError
    journal, state, ticket = _mutation_ready(tmp_path)
    ticket = journal.arm_mutation(journal.reopen_restart(ticket))
    _, approval = _accepted_records(state)
    consumed = state / "approvals" / (RID + ".consumed")
    unconsumed = state / "approvals" / (RID + ".json")
    consumed.rename(unconsumed)
    approval_dir = state / "approvals"
    controls = []

    def consume():
        if point == "before_replace":
            raise OSError("injected")
        os.replace(unconsumed, consumed)
        if point == "after_replace":
            raise OSError("injected")
        fd = os.open(approval_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            if point == "directory_sync":
                raise OSError("injected")
            engine._sync_fd(fd)
        finally:
            os.close(fd)

    with pytest.raises(OSError):
        consume()
        controls.append("stop")
    journal.mark_mutation_uncertain(ticket)
    assert controls == [] and journal.uncertain is True
    assert load(state).inventory().fence["state"] == "uncertain"
    assert load(state).inventory().operations[RID]["phase"] == "restart_pending"
    with pytest.raises(RecoveryError):
        journal.phase(ticket, "stopping")
