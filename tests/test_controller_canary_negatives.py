"""T022: canary scope/qualification negatives — stale/incomplete evidence.

Every test in this file is a NEGATIVE: some part of the canary evidence
chain is made wrong or incomplete, and the system must refuse — no
success envelope, no qualification, no auto-repair, no retry. Scan-level
cases prove the bounded namespace refuses stale/mismatched/incomplete
terminal evidence for a consumed scope. Lane-level cases drive the real
orchestration (fake platform adapter, REAL double-forked worker
children) into each refusal and assert the blocked record, the durable
scope diagnostic, the retained (never deleted) state, and that any live
worker child is cleaned up cooperatively by exact identity. No real
launchctl, no production state, no credentials.
"""
import json
import os
import signal
import time

import pytest

from restart_control.engine import Fault, atomic_json
from restart_control import canary
from restart_control.startup import run_startup_canary
from restart_control.shutdown import Shutdown

from test_restart_runtime import runtime, private_root  # noqa: F401
from test_controller_canary import (NONCE, arm, detail, evidence,
                                    place_state)
from test_controller_startup import (FakePlatform, _FakeController,
                                     _arm, _make_lane)


# == T022 scan negatives: scope evidence cross-bindings ========================

def _consumed_state(tmp_path, *, evidence_record, detail_record=None):
    """A consumed scope with its arm, terminal evidence, optional detail."""
    place_state(tmp_path, scopes=[NONCE], evidence_records=[evidence_record],
                detail_records=[detail_record] if detail_record else ())
    root = tmp_path/"canary"/NONCE
    atomic_json(root/"intent.json", arm())
    return root


def _file_detail(tmp_path, evidence_record, detail_record):
    """File a detail record under the digest the evidence names."""
    blob = json.dumps(detail_record, sort_keys=True).encode()
    path = tmp_path/"canary"/"detail"/(evidence_record["detail_digest"]+".json")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(blob)
    path.chmod(0o600)


def test_scan_rejects_omitted_detail_file(tmp_path):
    """Omitted detail file: the evidence's detail_digest names nothing."""
    _consumed_state(tmp_path, evidence_record=evidence(),
                    detail_record=None)
    with pytest.raises(Fault, match="recovery_invalid"):
        canary.scan_canary_namespace(tmp_path)


def test_scan_rejects_bad_detail_digest(tmp_path):
    """The evidence names a detail digest that is not the one placed."""
    placed = detail()
    wrong = evidence()
    wrong["detail_digest"] = "f"*64  # names another attempt's detail
    _consumed_state(tmp_path, evidence_record=wrong, detail_record=placed)
    with pytest.raises(Fault, match="recovery_invalid"):
        canary.scan_canary_namespace(tmp_path)


def test_scan_rejects_detail_bound_to_foreign_attempt(tmp_path):
    """A detail record whose nonce binding is not the evidence's."""
    record = evidence()
    foreign = detail()
    foreign["nonce"] = "b"*32
    _consumed_state(tmp_path, evidence_record=record, detail_record=None)
    _file_detail(tmp_path, record, foreign)
    with pytest.raises(Fault, match="recovery_invalid"):
        canary.scan_canary_namespace(tmp_path)


@pytest.mark.parametrize("key,value", [
    ("boot_identity", "99999999-9999-9999-9999-999999999999"),
    ("policy_digest", "1"*64),
    ("controller_digest", "2"*64),
    ("controller_plist_digest", "3"*64),
])
def test_scan_rejects_generation_and_boot_mismatch(tmp_path, key, value):
    """Evidence from another boot/generation never settles this scope.

    The consumed arm's own bindings are what the evidence must equal:
    clock/boot mismatch or changed policy/controller/plist sources
    refuse (the evidence is stale relative to the arm that ran here).
    """
    record = evidence()
    record[key] = value
    placed = detail()
    # Keep the detail internally consistent with the mutated evidence so
    # ONLY the arm/evidence cross-binding can refuse.
    for field in ("nonce", "boot_identity", "controller_identity",
                  "controller_epoch"):
        placed[field] = record[field]
    _consumed_state(tmp_path, evidence_record=record, detail_record=None)
    _file_detail(tmp_path, record, placed)
    with pytest.raises(Fault, match="recovery_invalid"):
        canary.scan_canary_namespace(tmp_path)


def test_scan_rejects_sibling_scope_evidence(tmp_path):
    """Evidence from a sibling nonce never settles this scope."""
    record = evidence()
    record["nonce"] = "b"*32  # a sibling attempt's nonce
    record["canary_label"] = canary.canary_label(record["nonce"])
    _consumed_state(tmp_path, evidence_record=record, detail_record=None)
    _file_detail(tmp_path, record, detail())
    with pytest.raises(Fault, match="recovery_invalid"):
        canary.scan_canary_namespace(tmp_path)


def test_scan_rejects_recycled_identity_birth(tmp_path):
    """Two incarnations sharing one birth refuse; runs alone never pass."""
    record = evidence()
    record["incarnations"][1]["birth"] = list(record["incarnations"][0]["birth"])
    place_state(tmp_path, scopes=[NONCE], evidence_records=[record])
    root = tmp_path/"canary"/NONCE
    atomic_json(root/"intent.json", arm())
    with pytest.raises(Fault, match="recovery_invalid"):
        canary.scan_canary_namespace(tmp_path)


# == T022 lane negatives: real orchestration refusals ==========================

def _wrap(fake, wrapper):
    """Instance-level response mutation; never a class-level patch."""
    original = fake.execute
    fake.execute = lambda argv, timeout: wrapper(argv, timeout, original)


def _cleanup_children(fake):
    """Cooperatively SIGTERM any live worker child by its exact pid."""
    for pid, _runs in list(fake._children):
        try:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                    time.sleep(0.05)
                except OSError:
                    break
        except OSError:
            pass


def _lane(runtime, *, keepalive=False, consume=True):
    """A lane over a fresh arm, consumed exactly as serve would consume it."""
    arm_record = _arm(runtime)
    if consume:
        # The production serve path consumes the arm before the lane
        # runs; a direct lane run must mirror that retained-state shape.
        canary.consume_arm(runtime.state, operator_uid=os.getuid())
    fake = FakePlatform(runtime, runtime.policy_path, runtime.state)
    lane = _make_lane(runtime, fake, arm_record)
    if keepalive:
        original = lane._keepalive
        lane._keepalive = lambda: (fake.worker_exited(1), original())[1]
    return lane, fake, arm_record


def _scope_diagnostic(runtime, arm_record):
    scope = canary.canary_scope(runtime.state, arm_record["nonce"])
    return json.loads((scope/"diagnostic.json").read_text())


def test_lane_rejects_wrong_supervision_parity(runtime, private_root):
    """A loaded job whose throttle (supervision) differs is refused."""
    import restart_control.startup as startup
    lane, fake, arm_record = _lane(runtime)
    def wrong_supervision(argv, timeout, original):
        status, raw = original(argv, timeout)
        if argv[1] == "print":
            raw = raw.replace(b"minimum runtime = 30",
                              b"minimum runtime = 90")
        return status, raw
    _wrap(fake, wrong_supervision)
    original_cap = startup.QUERY_CAP
    startup.QUERY_CAP = 1.0
    try:
        result = lane.run()
    finally:
        startup.QUERY_CAP = original_cap
        _cleanup_children(fake)
    assert result["outcome"] == "canary_blocked"
    assert result["code"] == "canary_first_load_failed"
    assert _scope_diagnostic(runtime, arm_record)["code"] == \
        "canary_first_load_failed"
    # No retry: bootstrap happened exactly once.
    assert len([c for c in fake.calls if c[1] == "bootstrap"]) == 1


def test_lane_rejects_wrong_logging_environment(runtime, private_root):
    """A loaded job missing its declared logging environment refuses."""
    import restart_control.startup as startup
    lane, fake, arm_record = _lane(runtime)
    def wrong_logging(argv, timeout, original):
        status, raw = original(argv, timeout)
        if argv[1] == "print":
            raw = raw.replace(b"OSLogRateLimit => 64", b"")
        return status, raw
    _wrap(fake, wrong_logging)
    original_cap = startup.QUERY_CAP
    startup.QUERY_CAP = 1.0
    try:
        result = lane.run()
    finally:
        startup.QUERY_CAP = original_cap
        _cleanup_children(fake)
    assert result["outcome"] == "canary_blocked"
    assert result["code"] == "canary_first_load_failed"


def test_lane_rejects_respawn_count_other_than_two(runtime, private_root):
    """A third incarnation (runs == 3) never satisfies the lane.

    The second distinct birth is observed, but the platform reports the
    respawn count as three: the lane accepts exactly two and blocks.
    """
    import restart_control.startup as startup
    lane, fake, arm_record = _lane(runtime, keepalive=True)
    def third_incarnation(argv, timeout, original):
        status, raw = original(argv, timeout)
        if argv[1] == "print":
            raw = raw.replace(b"runs = 2", b"runs = 3")
        return status, raw
    _wrap(fake, third_incarnation)
    original_cap = startup.KEEPALIVE_CAP
    startup.KEEPALIVE_CAP = 2.0
    try:
        result = lane.run()
    finally:
        startup.KEEPALIVE_CAP = original_cap
        _cleanup_children(fake)
    assert result["outcome"] == "canary_blocked"
    assert result["code"] == "canary_keepalive_failed"
    assert _scope_diagnostic(runtime, arm_record)["code"] == \
        "canary_keepalive_failed"
    evidence_dir = runtime.state/"canary"/"evidence"
    assert not evidence_dir.exists() or not list(evidence_dir.iterdir())


def test_lane_rejects_lost_latch_receipt(runtime, private_root):
    """Missing latch.json after bootout: shutdown detail refuses."""
    lane, fake, arm_record = _lane(runtime, keepalive=True)
    scope = canary.canary_scope(runtime.state, arm_record["nonce"])
    def no_receipt(argv, timeout, original):
        status, raw = original(argv, timeout)
        if argv[1] == "bootout":
            try:
                (scope/"latch.json").unlink()
            except OSError:
                pass
        return status, raw
    _wrap(fake, no_receipt)
    result = lane.run()
    _cleanup_children(fake)
    assert result["outcome"] == "canary_blocked"
    assert result["code"] == "canary_shutdown_receipt_missing"
    assert _scope_diagnostic(runtime, arm_record)["code"] == \
        "canary_shutdown_receipt_missing"


def test_lane_rejects_incomplete_cleanup(runtime, private_root):
    """A job still loaded after bootout refuses: cleanup proves absence."""
    lane, fake, arm_record = _lane(runtime, keepalive=True)
    lingering = {"active": False}
    def bootout_but_job_stays(argv, timeout, original):
        status, raw = original(argv, timeout)
        if argv[1] == "bootout":
            lingering["active"] = True
        return status, raw
    _wrap(fake, bootout_but_job_stays)
    original_job_state = runtime._job_state

    def lingering_job_state(label=None, expected=None, expected_path=None):
        state = original_job_state(label, expected, expected_path)
        if state is None and lingering["active"]:
            # launchd still shows the service after bootout: the job
            # was never genuinely removed.
            return {"identity": None, "runs": 2, "parity": False}
        return state
    runtime._job_state = lingering_job_state
    try:
        result = lane.run()
    finally:
        runtime._job_state = original_job_state
        _cleanup_children(fake)
    assert result["outcome"] == "canary_blocked"
    assert result["code"] == "canary_cleanup_failed"
    assert _scope_diagnostic(runtime, arm_record)["code"] == \
        "canary_cleanup_failed"


def test_lane_rejects_control_refusal_without_retry(runtime, private_root):
    """A refused bootout blocks the lane once; never a retry."""
    lane, fake, arm_record = _lane(runtime, keepalive=True)
    def refused(argv, timeout, original):
        if argv[1] == "bootout":
            fake.calls.append(list(argv))
            return 3, b"bootout: fixture refused"
        return original(argv, timeout)
    _wrap(fake, refused)
    result = lane.run()
    _cleanup_children(fake)
    assert result["outcome"] == "canary_blocked"
    assert result["code"] == "service_control_denied"
    assert len([c for c in fake.calls if c[1] == "bootout"]) == 1


def test_lane_control_refusal_emits_no_success_envelope(runtime, private_root):
    """On a refused bootstrap no evidence/detail file is ever written."""
    lane, fake, arm_record = _lane(runtime)
    def refused(argv, timeout, original):
        if argv[1] == "bootstrap":
            fake.calls.append(list(argv))
            return 5, b"bootstrap: fixture refused"
        return original(argv, timeout)
    _wrap(fake, refused)
    result = lane.run()
    _cleanup_children(fake)
    assert result["outcome"] == "canary_blocked"
    assert result["code"] == "service_control_denied"
    for name in ("evidence", "detail"):
        directory = runtime.state/"canary"/name
        assert not directory.exists() or not list(directory.iterdir())
    # The consumed arm is retained in the scope, never silently deleted.
    scope = canary.canary_scope(runtime.state, arm_record["nonce"])
    assert (scope/"intent.json").exists()


def test_lane_completed_attempt_never_replays(runtime, private_root):
    """After one completed pass, a re-serve performs zero canary activity."""
    lane, fake, arm_record = _lane(runtime, keepalive=True)
    result = lane.run()
    assert result["outcome"] == "canary_passed", result
    _cleanup_children(fake)
    # A fresh serve over the same state: the consumed scope is settled
    # by cross-bound evidence, no arm exists, and nothing runs again.
    fresh = FakePlatform(runtime, runtime.policy_path, runtime.state)
    _make_lane(runtime, fresh, arm_record)  # rebinds runtime helpers
    replay = run_startup_canary(runtime, _FakeController(), Shutdown())
    assert replay is None
    assert fresh.calls == []
    # The one-time arm is gone; a second explicit consumption refuses.
    with pytest.raises(Fault, match="recovery_invalid"):
        canary.consume_arm(runtime.state, operator_uid=os.getuid())