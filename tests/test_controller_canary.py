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
sys.path.insert(0, str(ROOT/'tests'))
from restart_control.engine import Fault, atomic_json, private_dir
from restart_control import canary
from test_restart_runtime import runtime, private_root  # noqa: F401


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


def evidence(now=0.0, mono=0.0, scope="fixture", nonce=NONCE,
             detail_digest="e" * 64):
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
            "detail_digest": detail_digest,
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
    summary = canary.scan_canary_namespace(tmp_path, boot=UUID)
    assert summary["armed"]["nonce"] == NONCE
    assert summary["scopes"] == {}


def test_scan_arm_from_foreign_kernel_boot_is_stale(tmp_path):
    # An arm still inside its wall-clock window but from a previous
    # kernel boot must be treated as stale, never consumed.
    place_state(tmp_path, arm_record=arm(now=time.time(), mono=time.monotonic()))
    with pytest.raises(Fault):
        canary.scan_canary_namespace(
            tmp_path, boot="00000000-0000-0000-0000-000000000000")

def test_scan_expired_arm_blocks(tmp_path):
    place_state(tmp_path, arm_record=arm(now=0.0, mono=0.0))
    with pytest.raises(Fault):
        canary.scan_canary_namespace(tmp_path)


def test_scan_arm_nonce_with_existing_scope_blocks(tmp_path):
    place_state(tmp_path, arm_record=arm(now=time.time(), mono=time.monotonic()),
                scopes=[NONCE])
    with pytest.raises(Fault):
        canary.scan_canary_namespace(tmp_path, boot=UUID)


def test_scan_consumed_scope_with_terminal_evidence_passes(tmp_path):
    # A consumed scope always carries the consumed arm (the one-time
    # consumption renames it into the scope) plus terminal evidence whose
    # detail_digest names its own validated detail record, at the
    # detail's true content digest.
    record_detail = detail()
    blob = json.dumps(record_detail, sort_keys=True).encode()
    detail_digest = hashlib.sha256(blob).hexdigest()
    record_evidence = evidence(detail_digest=detail_digest)
    place_state(tmp_path, scopes=[NONCE], evidence_records=[record_evidence],
                detail_records=[record_detail])
    root = tmp_path/"canary"/NONCE
    atomic_json(root/"intent.json", arm())
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


# -- T017: arm command and one-time consumption -------------------------------

ARM_BINDINGS = {"policy_digest": DIGEST, "controller_digest": DIGEST,
                "controller_plist_digest": DIGEST}


def test_arm_canary_creates_exclusive_private_envelope(tmp_path):
    record = canary.arm_canary(tmp_path, ARM_BINDINGS)
    root = tmp_path/"canary"
    assert (root/"intent.json").exists()
    assert (root/"intent.json").stat().st_mode & 0o777 == 0o600
    assert len(record["nonce"]) == 32
    assert json.loads((root/"intent.json").read_text()) == record
    canary.validate_arm(record, operator_uid=os.getuid())
    # Second arm refuses: exclusive, never overwritten.
    with pytest.raises(Fault):
        canary.arm_canary(tmp_path, ARM_BINDINGS)


def test_arm_refuses_existing_uncertainty(tmp_path):
    canary.arm_canary(tmp_path, ARM_BINDINGS)
    # Any consumed scope present (even without evidence) blocks a new arm.
    scope = tmp_path/"canary"/NONCE
    scope.mkdir(mode=0o700)
    (scope/"intent.json").write_text("{}")
    with pytest.raises(Fault):
        canary.arm_canary(tmp_path, ARM_BINDINGS)


def test_arm_rejects_incomplete_bindings(tmp_path):
    for bad in ({}, {**ARM_BINDINGS, "policy_digest": "z"*64},
                {**ARM_BINDINGS, "extra": 1},
                {k: v for k, v in ARM_BINDINGS.items() if k != "controller_digest"}):
        with pytest.raises(Fault):
            canary.arm_canary(tmp_path, bad)


def test_consume_arm_moves_envelope_into_scope_once(tmp_path):
    record = canary.arm_canary(tmp_path, ARM_BINDINGS)
    consumed = canary.consume_arm(tmp_path, operator_uid=os.getuid())
    assert consumed["nonce"] == record["nonce"]
    scope = tmp_path/"canary"/record["nonce"]
    assert (scope/"intent.json").exists()
    assert not (tmp_path/"canary"/"intent.json").exists()
    # Replay: the arm is gone; a second consumption refuses.
    with pytest.raises(Fault):
        canary.consume_arm(tmp_path, operator_uid=os.getuid())


def test_consume_arm_refuses_when_scope_is_dirty(tmp_path):
    record = canary.arm_canary(tmp_path, ARM_BINDINGS)
    scope = tmp_path/"canary"/record["nonce"]
    scope.mkdir(mode=0o700)
    (scope/"worker.json").write_text("{}")
    with pytest.raises(Fault):
        canary.consume_arm(tmp_path, operator_uid=os.getuid())


def test_arm_bindings_equal_runtime_binding_facts(runtime, private_root):
    """The arm's generation hashes equal Runtime.binding_facts()."""
    from restart_control.canary import arm_bindings
    assert arm_bindings(runtime.policy_path) == runtime.binding_facts()


def test_cli_arm_canary_against_installed_layout(runtime, private_root):
    """The real entrypoint arms once and refuses pending uncertainty."""
    import subprocess
    import sys as _sys
    entrypoint = ROOT/'core'/'restart_controller.py'
    home = private_root/'cli-home'
    home.mkdir(mode=0o700)
    env = {'HOME': str(home), 'PATH': os.defpath,
           'TMPDIR': os.environ['TMPDIR']}
    argv = [_sys.executable, '-I', str(entrypoint),
            '--policy', str(runtime.policy_path), 'arm-canary']
    result = subprocess.run(argv, env=env, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout.decode())
    assert response['status'] == 'armed'
    assert canary._NONCE.fullmatch(response['nonce'])
    assert response['label'] == canary.canary_label(response['nonce'])
    # Pending arm: a second arm refuses.
    result = subprocess.run(argv, env=env, capture_output=True, timeout=60)
    assert result.returncode == 78
    assert json.loads(result.stdout.decode())['code'] != 'ok'

def _fake_policy(tmp_path):
    """Minimal valid policy with a private state dir for worker CLI tests."""
    policy = {"version": 1, "kit_root": str(ROOT),
              "deployment_root": str(tmp_path/'deployment'),
              "plist": str(tmp_path/'plist'), "auto_plist": str(tmp_path/'auto'),
              "state_dir": str(tmp_path), "scratch_dir": str(tmp_path/'scratch'),
              "candidate_port": 49132, "models": ['fixture-direct', 'fixture-router'],
              "controller_root": str(tmp_path/'controller'),
              "controller_label": 'fixture.controller'}
    policy_path = tmp_path/'policy.json'
    atomic_json(policy_path, policy)
    return policy_path


# -- T018: disposable worker mode --------------------------------------------

def _consumed_scope(tmp_path, nonce=NONCE):
    """A genuinely consumed scope: consumed arm + the lane's generated plist.

    The worker refuses any scope lacking these (plan §5): an arbitrary
    worker invocation must not populate an empty scope.
    """
    import plistlib
    scope = canary.canary_scope(tmp_path, nonce)
    arm = {'version': 1, 'operator_uid': os.getuid(), 'nonce': nonce,
           'policy_digest': '0'*64, 'controller_digest': '0'*64,
           'controller_plist_digest': '0'*64, 'boot_identity': UUID,
           'issued_at': time.time(), 'expires_at': time.time()+300,
           'issued_monotonic': time.monotonic(),
           'expires_monotonic': time.monotonic()+300}
    from restart_control.engine import atomic_json
    atomic_json(scope/'intent.json', arm)
    label = canary.canary_label(nonce)
    (scope/(label+'.plist')).write_bytes(plistlib.dumps({'Label': label}))
    (scope/(label+'.plist')).chmod(0o600)
    return scope


def test_worker_records_identity_and_exits_on_marker(tmp_path):
    """A real retained child records its true kernel identity and obeys
    the one-time cooperative exit marker."""
    import subprocess
    import sys as _sys
    scope = _consumed_scope(tmp_path)
    entrypoint = ROOT/'core'/'restart_controller.py'
    argv = [_sys.executable, '-I', str(entrypoint), '--policy',
            str(_fake_policy(tmp_path)), 'canary-worker', NONCE]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            env={'HOME': str(tmp_path), 'PATH': os.defpath,
                                 'TMPDIR': os.environ['TMPDIR']})
    try:
        worker = scope/'incarnation-1.json'
        deadline = time.monotonic() + 15
        while not worker.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert worker.exists()
        record = canary._read_json(worker)
        assert record['identity']['pid'] == proc.pid
        assert record['identity']['uid'] == os.getuid()
        # One-time cooperative exit marker: exit 0, bounded status JSON.
        (scope/'exit.marker').touch()
        out, _ = proc.communicate(timeout=15)
        assert proc.returncode == 0
        payload = json.loads(out.decode())
        assert payload['outcome'] == 'exit_marker'
        assert payload['requested'] is False
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=10)


def test_worker_third_incarnation_refuses(tmp_path):
    """A third worker incarnation in the same scope is uncertain state."""
    scope = canary.canary_scope(tmp_path, NONCE)
    (scope/'incarnation-1.json').write_text('{}')
    (scope/'incarnation-2.json').write_text('{}')
    with pytest.raises(Fault):
        canary.run_canary_worker(tmp_path, NONCE)


def test_worker_refuses_unconsumed_scope(tmp_path):
    """An arbitrary worker invocation cannot populate an empty scope."""
    canary.canary_scope(tmp_path, NONCE)  # exists but empty: no arm, no plist
    with pytest.raises(Fault):
        canary.run_canary_worker(tmp_path, NONCE)
    scope = canary.canary_scope(tmp_path, NONCE)
    assert not (scope/'incarnation-1.json').exists()


def test_canary_worker_shutdown_deadline(tmp_path):
    """SIGTERM latches the shared 50s budget; the worker exits cooperatively."""
    import signal
    import subprocess
    import sys as _sys
    scope = _consumed_scope(tmp_path)
    entrypoint = ROOT/'core'/'restart_controller.py'
    argv = [_sys.executable, '-I', str(entrypoint), '--policy',
            str(_fake_policy(tmp_path)), 'canary-worker', NONCE]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            env={'HOME': str(tmp_path), 'PATH': os.defpath,
                                 'TMPDIR': os.environ['TMPDIR']})
    try:
        worker = scope/'incarnation-1.json'
        deadline = time.monotonic() + 15
        while not worker.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert worker.exists()
        # One real SIGTERM to the retained child: cooperative shutdown via
        # the shared latch, no escalation, prompt bounded exit.
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=15)
        assert proc.returncode == 0
        payload = json.loads(out.decode())
        assert payload['outcome'] == 'shutdown_requested'
        assert payload['requested'] is True
        assert payload['deadline'] is not None
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=10)
