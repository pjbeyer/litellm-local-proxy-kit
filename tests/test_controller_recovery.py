"""Closed recovery records and bounded, private on-disk inventory."""

import copy
import importlib.util
import json
import os
from pathlib import Path

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
