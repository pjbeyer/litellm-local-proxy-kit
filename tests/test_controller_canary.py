"""Canary envelope/scope schema tests. All live-query responses are FAKE.

T016: no real launchctl, no live classification, no production state and
no credentials are touched here. Every platform response in this file is
an explicitly fake fixture; live query/absence classification must be
proven against the actual platform before a permitted bootstrap (later
tasks, operator-gated).
"""
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'core'))
from restart_control.engine import Fault, atomic_json, private_dir
from restart_control import canary


UUID = "12345678-1234-1234-1234-123456789abc"
NONCE = "a" * 32
DIGEST = "0" * 64


def identity(pid=100):
    return {"pid": pid, "uid": os.getuid(), "ppid": 1, "pgid": pid,
            "birth": [2, pid]}


def arm(now=0.0, mono=0.0, nonce=NONCE):
    return {"version": 1, "operator_uid": os.getuid(), "nonce": nonce,
            "policy_digest": DIGEST, "controller_digest": DIGEST,
            "controller_plist_digest": DIGEST, "boot_identity": UUID,
            "issued_at": now, "expires_at": now + 300,
            "issued_monotonic": mono, "expires_monotonic": mono + 300}


def supervision():
    return {"KeepAlive": True, "ThrottleInterval": 30,
            "RunAtLoad": True, "ExitTimeOut": 60}


def shutdown_contract():
    return {"version": 1, "supervisor_exit_seconds": 60,
            "shutdown_deadline_seconds": 50,
            "logging_environment": {"OSLogRateLimit": "64"}}


def evidence(now=0.0, mono=0.0, scope="fixture", nonce=NONCE):
    return {"version": 1, "scope": scope, "nonce": nonce,
            "boot_identity": UUID, "operator_uid": os.getuid(),
            "controller_identity": identity(200),
            "controller_epoch": "b" * 32,
            "controller_digest": DIGEST, "policy_digest": DIGEST,
            "controller_plist_digest": DIGEST,
            "controller_supervision": supervision(),
            "shutdown_contract": shutdown_contract(),
            "canary_label": canary.canary_label(nonce),
            "canary_plist_digest": "1" * 64,
            "incarnations": [
                {**identity(300), "birth": [3, 0], "runs": 1},
                {**identity(301), "birth": [4, 0], "runs": 2}],
            "checks": {k: True for k in canary.CHECK_KEYS},
            "detail_digest": "e" * 64,
            "issued_at": now, "issued_monotonic": mono}


def step(name, start, end, rule):
    return {"name": name, "started_monotonic": start,
            "completed_monotonic": end, "outcome": "passed",
            "identity": identity(300) if rule["identity"] == "first" else
                       identity(301) if rule["identity"] == "second" else None,
            "runs": rule["runs"],
            "loaded_exit_seconds": 60 if rule["loaded"] else None,
            "loaded_environment_digest": "f" * 64 if rule["loaded"] else None,
            "exit_code": rule["exit_code"]}


def detail(now=0.0):
    steps = [step(name, 1.0 + i, 2.0 + i, canary.STEP_APPLICABILITY[name])
             for i, name in enumerate(canary.STEP_ORDER)]
    return {"version": 1, "nonce": NONCE, "boot_identity": UUID,
            "controller_identity": identity(200),
            "controller_epoch": "b" * 32, "steps": steps,
            "shutdown": {"identity": identity(301),
                         "requested_monotonic": 50.0,
                         "exit_observed_monotonic": 55.0,
                         "cleanup_completed_monotonic": 60.0,
                         "deadline_seconds": 50}}


# -- derived scope ------------------------------------------------------------

def test_canary_label_is_derived_from_nonce_only():
    assert canary.canary_label(NONCE) == "local.restart-canary." + NONCE


@pytest.mark.parametrize("bad", ["", "A" * 32, "a" * 31, "a" * 33, None, 5,
                                 "local.restart-canary." + NONCE])
def test_canary_label_refuses_invalid_nonce(bad):
    with pytest.raises(ValueError):
        canary.canary_label(bad)


def test_scope_directory_is_derived_and_private(tmp_path):
    # Root under TMPDIR: the privacy validator walks every ancestor and
    # refuses symlinked paths (pytest's default basetemp has one).
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="t016-", dir=os.environ["TMPDIR"]))
    scope = canary.canary_scope(root, NONCE)
    assert scope == root/"canary"/NONCE
    assert scope.stat().st_mode & 0o777 == 0o700
    assert scope.stat().st_uid == os.getuid()


def test_scope_refuses_production_label_equality():
    # The derived label can never equal a production/controller label:
    # production labels do not carry the fixed local.restart-canary prefix.
    assert canary.canary_label(NONCE).startswith("local.restart-canary.")


# -- arm envelope schema -------------------------------------------------------

def test_valid_arm_passes_and_returns_nonce():
    assert canary.validate_arm(arm(), operator_uid=os.getuid()) == NONCE


@pytest.mark.parametrize("mutation", [
    lambda r: r.pop("nonce"),
    lambda r: r.update(unexpected=1),
    lambda r: r.update(version=2),
    lambda r: r.update(version=True),
    lambda r: r.update(operator_uid=os.getuid() + 1),
    lambda r: r.update(nonce="A" * 32),
    lambda r: r.update(policy_digest="z" * 64),
    lambda r: r.update(controller_digest="0" * 63),
    lambda r: r.update(boot_identity="not-a-uuid"),
    lambda r: r.update(issued_at=float("inf")),
    lambda r: r.update(expires_at=0.0),          # expiry before issue
    lambda r: r.update(expires_at=301.0 + 0.0),  # window > 300s
    lambda r: r.update(issued_monotonic=1e9),
])
def test_arm_schema_mutation_refuses_in_turn(mutation):
    record = arm()
    mutation(record)
    with pytest.raises(ValueError):
        canary.validate_arm(record, operator_uid=os.getuid())


def test_arm_freshness_is_dual_clock():
    record = arm(now=100.0, mono=50.0)
    assert canary.arm_is_fresh(record, 101.0, 51.0)
    assert not canary.arm_is_fresh(record, 100.0 - 1, 51.0)   # too early
    assert not canary.arm_is_fresh(record, 400.0 + 1, 51.0)   # wall expired
    assert not canary.arm_is_fresh(record, 101.0, 350.0 + 1)  # mono expired


# -- evidence envelope schema -------------------------------------------------

def test_valid_evidence_passes():
    canary.validate_evidence(evidence(), operator_uid=os.getuid())


@pytest.mark.parametrize("mutation", [
    lambda r: r.pop("checks"),
    lambda r: r.update(unexpected=1),
    lambda r: r.update(scope="unknown-scope"),
    lambda r: r.update(operator_uid=os.getuid() + 1),
    lambda r: r.update(nonce="B" * 32),
    lambda r: r.update(boot_identity="nope"),
    lambda r: r.update(controller_identity={"pid": 1}),
    lambda r: r.update(controller_epoch="c" * 31),
    lambda r: r.update(detail_digest="g" * 63),
    lambda r: r.update(canary_label="local.restart-canary." + "b" * 32),
    lambda r: r.update(incarnations=[r["incarnations"][0]]),
    lambda r: r.update(incarnations=[r["incarnations"][0], r["incarnations"][0]]),
    lambda r: r["incarnations"][1].update(runs=1),
    lambda r: r.update(checks={"bootstrap": True}),
    lambda r: r["checks"].update(cleanup="yes"),   # checks are exact booleans
])
def test_evidence_schema_mutation_refuses_in_turn(mutation):
    record = evidence()
    mutation(record)
    with pytest.raises(ValueError):
        canary.validate_evidence(record, operator_uid=os.getuid())


def test_evidence_canary_label_must_be_derived_not_stated():
    record = evidence()
    record["canary_label"] = "local.restart-canary." + "b" * 32
    with pytest.raises(ValueError):
        canary.validate_evidence(record, operator_uid=os.getuid())


def test_evidence_cross_binding_rejects_sibling_scope():
    record = evidence()
    expected = evidence()
    expected["controller_identity"] = identity(999)  # a sibling context
    with pytest.raises(ValueError):
        canary.validate_evidence(record, operator_uid=os.getuid(), expected=expected)


# -- detail schema ------------------------------------------------------------

def test_valid_detail_passes():
    canary.validate_detail(detail())


@pytest.mark.parametrize("mutation", [
    lambda r: r.pop("shutdown"),
    lambda r: r.update(unexpected=1),
    lambda r: r.update(nonce="C" * 32),
    lambda r: r.update(steps=[]),
    lambda r: r["steps"].reverse(),
    lambda r: r["steps"].pop(2),
    lambda r: r["steps"][0].update(exit_code=1),
    lambda r: r["steps"][1].update(loaded_exit_seconds=1000),
    lambda r: r["steps"][2].update(identity=None),  # runs fields explicit null only where applicable
    lambda r: r["steps"][4].update(loaded_environment_digest="0" * 63),
    lambda r: r["shutdown"].update(deadline_seconds=100),
    lambda r: r["shutdown"].update(requested_monotonic=100.0),
    lambda r: r["shutdown"].update(cleanup_completed_monotonic=200.0),
])
def test_detail_schema_mutation_refuses_in_turn(mutation):
    record = detail()
    mutation(record)
    with pytest.raises(ValueError):
        canary.validate_detail(record)


def test_detail_step_applicability_is_frozen():
    # exit_once: first identity, runs 1, null loaded fields
    record = detail()
    exit_once = record["steps"][2]
    assert exit_once["identity"]["pid"] == 300
    assert exit_once["runs"] == 1
    assert exit_once["loaded_exit_seconds"] is None
    assert exit_once["loaded_environment_digest"] is None
    assert exit_once["exit_code"] is None
    # bootout: last verified second identity, exit code 0
    bootout = record["steps"][4]
    assert bootout["identity"]["pid"] == 301
    assert bootout["exit_code"] == 0


# -- bounded namespace scan ---------------------------------------------------

def place_state(tmp_path, *, arm_record=None, scopes=(), evidence_records=(),
                detail_records=(), extra=None):
    root = tmp_path/"canary"
    root.mkdir(mode=0o700)
    if arm_record is not None:
        atomic_json(root/"intent.json", arm_record)
    for nonce in scopes:
        private_dir(root/nonce)
    for record in evidence_records:
        blob = json.dumps(record, sort_keys=True).encode()
        name = hashlib.sha256(blob).hexdigest()
        private_dir(root/"evidence")
        (root/"evidence"/(name + ".json")).write_bytes(blob)
        (root/"evidence"/(name + ".json")).chmod(0o600)
    for record in detail_records:
        blob = json.dumps(record, sort_keys=True).encode()
        name = hashlib.sha256(blob).hexdigest()
        private_dir(root/"detail")
        (root/"detail"/(name + ".json")).write_bytes(blob)
        (root/"detail"/(name + ".json")).chmod(0o600)
    if extra:
        extra(root)
    return root


def test_scan_without_namespace_is_clean(tmp_path):
    assert canary.scan_canary_namespace(tmp_path) == {"armed": None, "scopes": {}}


def test_scan_fresh_pending_arm_is_admissible(tmp_path):
    place_state(tmp_path, arm_record=arm(now=time.time(), mono=time.monotonic()))
    summary = canary.scan_canary_namespace(tmp_path)
    assert summary["armed"]["nonce"] == NONCE
    assert summary["scopes"] == {}


def test_scan_expired_arm_blocks(tmp_path):
    place_state(tmp_path, arm_record=arm(now=0.0, mono=0.0))
    with pytest.raises(Fault):
        canary.scan_canary_namespace(tmp_path)


def test_scan_arm_nonce_with_existing_scope_blocks(tmp_path):
    place_state(tmp_path, arm_record=arm(now=time.time(), mono=time.monotonic()),
                scopes=[NONCE])
    with pytest.raises(Fault):
        canary.scan_canary_namespace(tmp_path)


def test_scan_consumed_scope_with_terminal_evidence_passes(tmp_path):
    place_state(tmp_path, scopes=[NONCE], evidence_records=[evidence()],
                detail_records=[detail()])
    summary = canary.scan_canary_namespace(tmp_path)
    assert list(summary["scopes"]) == [NONCE]


def test_scan_consumed_scope_without_evidence_blocks(tmp_path):
    place_state(tmp_path, scopes=[NONCE], detail_records=[detail()])
    with pytest.raises(Fault):
        canary.scan_canary_namespace(tmp_path)


@pytest.mark.parametrize("name,kind", [
    ("stray.json", "file"), (".intent.json.abc123", "file"),
    ("notes.txt", "file"), ("evidence", "dir"), ("detail", "dir"),
])
def test_scan_unrecognized_entries_block(tmp_path, name, kind):
    def extra(root):
        target = root/name
        if kind == "dir":
            target.mkdir()
        else:
            target.write_text("stray\n")
    place_state(tmp_path, extra=extra)
    with pytest.raises(Fault):
        canary.scan_canary_namespace(tmp_path)


def test_scan_partial_scope_write_blocks(tmp_path):
    place_state(tmp_path, scopes=[NONCE],
                extra=lambda root: (root/NONCE/".partial").write_text("x\n"))
    with pytest.raises(Fault):
        canary.scan_canary_namespace(tmp_path)