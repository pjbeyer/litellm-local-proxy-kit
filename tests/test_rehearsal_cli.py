"""Closed manifest and receipt contract tests for the isolated rehearsal (T002).

These tests encode FR-001 (explicit, reviewed inputs; digests not paths) and
FR-006 (sanitized result bound to the verified runtime and check outcomes).
Everything here is synthetic: no network, no guest, no live proxy, no
credential material and no absolute host path in the repository.

The contract is closed in both directions: unknown or missing fields refuse,
and an otherwise schema-valid frame that names the wrong run or a different
proposal identity is still refused by the binding check.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

RUN_ID = "0123456789abcdef0123456789abcdef"
OTHER_RUN_ID = "fedcba9876543210fedcba9876543210"
PROPOSAL_SHA256 = hashlib.sha256(b"synthetic rendered proposal").hexdigest()
EFFECTIVE_SHA256 = hashlib.sha256(b"synthetic effective launch").hexdigest()


@pytest.fixture(scope="module")
def s():
    """Import the schema package lazily so the red phase fails per test."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import rehearsal.schemas as schemas

    return schemas


@pytest.fixture
def tree(tmp_path):
    """A reviewed input root with synthetic files, mirroring real admission."""
    root = tmp_path / "reviewed inputs"
    root.mkdir()
    for name in ("overlay.yaml", "pin.lock", "fixtures.yaml", "native receipt.json"):
        (root / name).write_text(f"{name}: synthetic\n", encoding="utf-8")
    bundle = root / "runtime bundle"
    bundle.mkdir()
    (bundle / "manifest.txt").write_text("synthetic bundle\n", encoding="utf-8")
    return root


def _tree_sha(path):
    digest = hashlib.sha256()
    for child in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(child.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(child.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _digest(path):
    if path.is_dir():
        return _tree_sha(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(root, name):
    return {"path": str(root / name), "sha256": _digest(root / name)}


def _manifest(root):
    """Known-good version-1 manifest with real digests over the fixture bytes."""
    return {
        "version": 1,
        "overlay": _entry(root, "overlay.yaml"),
        "pin": _entry(root, "pin.lock"),
        "runtime_bundle": _entry(root, "runtime bundle"),
        "guest_image": {"id": "rehearsal-guest-v1", "sha256": _digest(root / "fixtures.yaml")},
        "fixture_set": _entry(root, "fixtures.yaml"),
        "native_receipt": _entry(root, "native receipt.json"),
    }


def _scope(manifest):
    return {
        "run_id": RUN_ID,
        "overlay_sha256": manifest["overlay"]["sha256"],
        "pin_sha256": manifest["pin"]["sha256"],
        "proposal_sha256": PROPOSAL_SHA256,
        "fixture_set_sha256": manifest["fixture_set"]["sha256"],
        "effective_launch_sha256": EFFECTIVE_SHA256,
        "runtime_sha256": manifest["runtime_bundle"]["sha256"],
    }


def _routes(schemas, *drop, result="passed"):
    return [{"id": name, "feature": feature, "result": result}
            for name, feature in schemas.FIXTURE_ROUTES.items() if name not in drop]


def _guest(schemas, scope, **overrides):
    guest = {
        "version": 1,
        "run_id": scope["run_id"],
        "overlay_sha256": scope["overlay_sha256"],
        "pin_sha256": scope["pin_sha256"],
        "proposal_sha256": scope["proposal_sha256"],
        "fixture_set_sha256": scope["fixture_set_sha256"],
        "effective_launch_sha256": scope["effective_launch_sha256"],
        "runtime_sha256": scope["runtime_sha256"],
        "checks": {name: "passed" for name in schemas.REQUIRED_CHECKS},
        "exercised_routes": _routes(schemas),
        "exercised_features": ["health_endpoints", "auth_checks", "local_stub_route"],
        "substitutions": ["local_stub_provider", "test_key_substitution"],
        "exclusions": [{"feature": "provider_routes", "reason": "not_in_scope"},
                       {"feature": "real_credentials", "reason": "not_in_scope"}],
    }
    guest.update(overrides)
    return guest


def _receipt(schemas, scope, *, guest=None, status="passed", failure_class=None,
             closure=None, finished_at="2026-11-26T12:00:00Z"):
    if closure is None:
        closure = {"started": True, "proved": True, "method": "supervisor_attest"}
    return {
        "version": 1,
        "run_id": scope["run_id"],
        "status": status,
        "guest": guest,
        "failure_class": failure_class,
        "closure": closure,
        # A refusal settles before admission, so nothing ever started.
        "started_at": None if status == "refused" else "2026-11-26T11:59:00Z",
        "finished_at": finished_at,
    }


def _code(schemas, call, *args, **kwargs):
    with pytest.raises(schemas.SchemaError) as caught:
        call(*args, **kwargs)
    return caught.value.code


# --- known good -----------------------------------------------------------


def test_known_good_manifest_and_receipt_validate(s, tree):
    manifest = _manifest(tree)
    raw = json.dumps(manifest, sort_keys=True).encode("utf-8")
    reviewed = hashlib.sha256(raw).hexdigest()

    loaded = s.load_manifest(raw, reviewed_sha256=reviewed, input_root=str(tree))
    assert loaded["version"] == 1
    assert loaded["fixture_trace"] is False  # documented v1 default, resolved
    assert loaded["overlay"]["path"] == manifest["overlay"]["path"]
    assert "fixture_trace" not in manifest  # the caller's input is not mutated

    scope = _scope(manifest)
    receipt = _receipt(s, scope, guest=_guest(s, scope))
    assert s.validate_receipt(receipt, scope=scope)["status"] == "passed"

    rendered = s.render_receipt(receipt, scope=scope)
    assert json.loads(rendered)["status"] == "passed"
    # Host paths and input filenames are invocation inputs, never receipt values.
    assert str(tree).encode() not in rendered
    assert b"overlay.yaml" not in rendered
    assert b"fixtures.yaml" not in rendered


def test_manifest_digest_is_canonical_across_key_order(s, tree):
    manifest = _manifest(tree)
    root = str(tree)
    assert s.validate_manifest(manifest, input_root=root)
    reordered = dict(reversed(list(manifest.items())))
    assert s.manifest_digest(manifest, input_root=root) == s.manifest_digest(reordered, input_root=root)


# --- duplicate and unexpected data ---------------------------------------


def test_duplicate_keys_refused_at_any_depth(s):
    top = b'{"version":1,"version":1}'
    assert _code(s, s.strict_json, top) == "duplicate_key"
    nested = b'{"version":1,"overlay":{"path":"/a/b","path":"/a/c"}}'
    assert _code(s, s.strict_json, nested) == "duplicate_key"


def test_unexpected_fields_refused(s, tree):
    manifest = _manifest(tree)
    manifest["argv"] = ["--extra"]
    assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "unknown_field"

    manifest = _manifest(tree)
    manifest["overlay"]["mode"] = "0644"
    assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "unknown_field"

    manifest = _manifest(tree)
    manifest["guest_image"]["root"] = "/images"
    assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "unknown_field"

    receipt_scope = _scope(_manifest(tree))
    receipt = _receipt(s, receipt_scope, guest=_guest(s, receipt_scope))
    receipt["detail"] = "free text"
    assert _code(s, s.validate_receipt, receipt) == "unknown_field"

    guest = _guest(s, receipt_scope)
    guest["stdout"] = "raw output"
    receipt = _receipt(s, receipt_scope, guest=guest)
    assert _code(s, s.validate_receipt, receipt) == "unknown_field"


def test_missing_fields_refused(s, tree):
    for field in ("version", "overlay", "pin", "runtime_bundle", "guest_image", "fixture_set"):
        manifest = _manifest(tree)
        del manifest[field]
        assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "missing_field"

    manifest = _manifest(tree)
    del manifest["overlay"]["sha256"]
    assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "missing_field"

    manifest = _manifest(tree)
    manifest["fixture_trace"] = "false"
    assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "invalid_type"

    assert _code(s, s.validate_manifest, [1, 2, 3], input_root=str(tree)) == "invalid_type"


def test_manifest_replaced_together_with_internal_hashes_refused(s, tree):
    """The reviewed digest pins the exact bytes, so rewriting both the file and
    every internal hash cannot silently enter execution."""
    reviewed_raw = json.dumps(_manifest(tree), sort_keys=True).encode("utf-8")
    reviewed = hashlib.sha256(reviewed_raw).hexdigest()

    (tree / "overlay.yaml").write_text("overlay.yaml: replaced\n", encoding="utf-8")
    replaced = _manifest(tree)  # internal hashes recomputed for the new bytes
    replaced_raw = json.dumps(replaced, sort_keys=True).encode("utf-8")
    assert replaced_raw != reviewed_raw

    # The replaced manifest is itself well formed; only the reviewed-bytes pin refuses it.
    assert s.validate_manifest(replaced, input_root=str(tree))
    assert _code(s, s.load_manifest, replaced_raw, reviewed_sha256=reviewed,
                 input_root=str(tree)) == "manifest_digest_mismatch"
    # Admission only follows a fresh operator review of the exact new bytes.
    assert s.load_manifest(replaced_raw, reviewed_sha256=hashlib.sha256(replaced_raw).hexdigest(),
                           input_root=str(tree))["overlay"]["sha256"] == replaced["overlay"]["sha256"]

    assert _code(s, s.load_manifest, reviewed_raw, reviewed_sha256="A" * 64,
                 input_root=str(tree)) == "reviewed_digest_invalid"


# --- invalid input roots and paths ---------------------------------------


def test_invalid_input_roots_refused(s, tree, tmp_path):
    manifest = _manifest(tree)
    for bad_root in ("relative/root", "/", str(tree / ".."), ""):
        assert _code(s, s.validate_manifest, manifest, input_root=bad_root) == "invalid_input_root"

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "overlay.yaml").write_text("synthetic\n", encoding="utf-8")
    for field, value in (("overlay", str(outside / "overlay.yaml")),
                         ("pin", "pin.lock"),
                         ("fixture_set", str(tree / ".." / "fixtures.yaml"))):
        candidate = _manifest(tree)
        candidate[field] = {"path": value, "sha256": candidate[field]["sha256"]}
        code = _code(s, s.validate_manifest, candidate, input_root=str(tree))
        assert code == ("path_escape" if field != "pin" else "invalid_path")

    duplicate = _manifest(tree)
    duplicate["pin"] = dict(duplicate["overlay"])
    assert _code(s, s.validate_manifest, duplicate, input_root=str(tree)) == "duplicate_path"

    complete = _manifest(tree)
    del complete["native_receipt"]  # optional; makes the next case a real file check
    missing_file = _manifest(tree)
    missing_file["native_receipt"]["path"] = str(tree / "never written.json")
    assert _code(s, s.validate_manifest, missing_file, input_root=str(tree)) == "file_missing"
    assert s.validate_manifest(complete, input_root=str(tree))["native_receipt"] is None


def test_input_files_must_be_unshared_immutable_and_real(s, tree):
    """A reviewed input is one real, single-link, non-writable file. An alias,
    a hard link or a writable file is an unaudited source and refuses."""
    manifest = _manifest(tree)

    os.symlink(tree / "overlay.yaml", tree / "alias.yaml")
    aliased = _manifest(tree)
    aliased["overlay"]["path"] = str(tree / "alias.yaml")
    assert _code(s, s.validate_manifest, aliased, input_root=str(tree)) == "symlink_refused"

    shared = tree / "pin.lock"
    os.link(shared, tree / "pin copy.lock")
    assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "multiply_linked"
    os.unlink(tree / "pin copy.lock")

    writable = tree / "fixtures.yaml"
    writable.chmod(0o666)
    try:
        assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "writable_input"
    finally:
        writable.chmod(0o644)

    # A relative or nonexistent root never becomes an implicit default.
    assert _code(s, s.validate_manifest, manifest, input_root=str(tree / "missing")) \
        == "invalid_input_root"
    assert _code(s, s.validate_manifest, manifest, input_root="./inputs") == "invalid_input_root"


def test_digests_must_be_lowercase_hex_64(s, tree):
    for bad in ("A" * 64, "a" * 63, "g" * 64, "", 7, None, True):
        manifest = _manifest(tree)
        manifest["overlay"]["sha256"] = bad
        assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "digest_invalid"

    manifest = _manifest(tree)
    manifest["guest_image"]["sha256"] = "C" * 64
    assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "digest_invalid"


# --- binding: run ID and proposal digests --------------------------------


def test_wrong_run_id_in_schema_valid_frame_refused(s, tree):
    scope = _scope(_manifest(tree))
    receipt = _receipt(s, scope, guest=_guest(s, scope), status="failed",
                       failure_class="check_failed")
    receipt["run_id"] = OTHER_RUN_ID
    # Shape alone accepts it; the binding check is what refuses it.
    assert s.validate_receipt(receipt)["run_id"] == OTHER_RUN_ID
    assert _code(s, s.validate_receipt, receipt, scope=scope) == "receipt_run_mismatch"

    stray_guest = _receipt(s, scope, guest=_guest(s, scope, run_id=OTHER_RUN_ID))
    assert _code(s, s.validate_receipt, stray_guest, scope=scope) == "receipt_run_mismatch"


@pytest.mark.parametrize("field", ["overlay_sha256", "pin_sha256", "proposal_sha256"])
def test_mismatched_proposal_digests_in_schema_valid_frame_refused(s, tree, field):
    scope = _scope(_manifest(tree))
    guest = _guest(s, scope, **{field: "c3" * 32})
    assert guest[field] != scope[field]
    receipt = _receipt(s, scope, guest=guest)
    assert s.validate_receipt(receipt)["guest"][field] == "c3" * 32
    assert _code(s, s.validate_receipt, receipt, scope=scope) == "receipt_proposal_mismatch"


def test_scope_must_be_complete_and_typed(s, tree):
    scope = _scope(_manifest(tree))
    receipt = _receipt(s, scope, guest=_guest(s, scope))
    incomplete = {k: v for k, v in scope.items() if k != "runtime_sha256"}
    assert _code(s, s.validate_receipt, receipt, scope=incomplete) == "scope_invalid"
    assert _code(s, s.validate_receipt, receipt, scope={**scope, "extra": "x"}) == "scope_invalid"
    assert _code(s, s.validate_receipt, receipt, scope={**scope, "run_id": "not-a-run"}) == "scope_invalid"


# --- receipt status semantics --------------------------------------------


def test_unknown_status_and_failure_class_refused(s, tree):
    scope = _scope(_manifest(tree))
    receipt = _receipt(s, scope, guest=_guest(s, scope))
    assert _code(s, s.validate_receipt, {**receipt, "status": "ok"}) == "unknown_status"
    assert _code(s, s.validate_receipt, {**receipt, "failure_class": "boom"}) == "unknown_failure_class"
    assert _code(s, s.validate_receipt,
                 {**receipt, "closure": {"started": True, "proved": True, "method": "guess"}}) \
        == "unknown_closure_method"
    assert _code(s, s.validate_receipt, {**receipt, "started_at": "26-11-2026 12:00"}) \
        == "invalid_timestamp"

    guest = _guest(s, scope)
    guest["checks"]["liveness"] = "skipped"
    assert _code(s, s.validate_receipt, _receipt(s, scope, guest=guest)) == "invalid_check_result"
    guest = _guest(s, scope)
    del guest["checks"]["liveness"]
    assert _code(s, s.validate_receipt, _receipt(s, scope, guest=guest)) == "missing_check"
    guest = _guest(s, scope)
    guest["checks"]["made_up"] = "passed"
    assert _code(s, s.validate_receipt, _receipt(s, scope, guest=guest)) == "unknown_check"
    guest = _guest(s, scope, exclusions=[{"feature": "real_credentials", "reason": "not_in_scope"},
                                         {"feature": "provider_routes", "reason": "not_in_scope"}])
    assert _code(s, s.validate_receipt, _receipt(s, scope, guest=guest)) == "exclusions_invalid"
    for bad in ([1, 2], [{"feature": "provider_routes", "reason": "not_in_scope"},
                         {"feature": "made_up", "reason": "not_in_scope"}],
                [{"feature": "provider_routes", "reason": "not_in_scope"},
                 {"feature": "provider_routes", "reason": "not_in_scope"}],
                ["provider_routes"],
                [{"feature": "provider_routes"}],
                [{"feature": "provider_routes", "reason": "because I said so"}]):
        guest = _guest(s, scope, exclusions=bad)
        assert _code(s, s.validate_receipt, _receipt(s, scope, guest=guest)) == "exclusions_invalid"


def test_passed_requires_guest_all_checks_and_proved_closure(s, tree):
    scope = _scope(_manifest(tree))
    assert _code(s, s.validate_receipt, _receipt(s, scope, guest=None)) == "passed_without_guest"

    receipt = _receipt(s, scope, guest=_guest(s, scope),
                       closure={"started": True, "proved": False, "method": "none"})
    assert _code(s, s.validate_receipt, receipt) == "passed_without_closure"

    guest = _guest(s, scope)
    guest["checks"]["identity_continuity"] = "failed"
    assert _code(s, s.validate_receipt, _receipt(s, scope, guest=guest)) == "passed_with_failed_check"

    assert _code(s, s.validate_receipt,
                 _receipt(s, scope, guest=_guest(s, scope), failure_class="check_failed")) \
        == "passed_with_failure_class"


def test_refused_settles_only_when_nothing_started(s, tree):
    scope = _scope(_manifest(tree))
    good = _receipt(s, scope, status="refused", failure_class="input_refused",
                    closure={"started": False, "proved": False, "method": "none"})
    assert s.validate_receipt(good)["failure_class"] == "input_refused"

    started = {**good, "closure": {"started": True, "proved": True, "method": "backend_reconcile"}}
    assert _code(s, s.validate_receipt, started) == "refused_after_start"
    with_guest = {**good, "guest": _guest(s, scope)}
    assert _code(s, s.validate_receipt, with_guest) == "refused_with_guest"

    failed = _receipt(s, scope, status="failed", failure_class="check_failed")
    assert s.validate_receipt(failed)["status"] == "failed"
    assert _code(s, s.validate_receipt, _receipt(s, scope, status="failed")) == "failed_without_class"


def test_typed_incomplete_result_keeps_run_and_cannot_pass(s, tree):
    scope = _scope(_manifest(tree))
    incomplete = _receipt(s, scope, guest=None, status="incomplete",
                          failure_class="closure_uncertain",
                          closure={"started": True, "proved": False, "method": "none"},
                          finished_at=None)
    loaded = s.load_receipt(s.render_receipt(incomplete, scope=scope), scope=scope)
    assert loaded["status"] == "incomplete"
    assert loaded["guest"] is None
    assert loaded["run_id"] == RUN_ID
    assert loaded["finished_at"] is None

    # Unknown closure cannot masquerade as a pass or as a settled failure.
    assert _code(s, s.validate_receipt, {**incomplete, "status": "passed",
                                         "failure_class": None}) == "passed_without_guest"
    assert _code(s, s.validate_receipt,
                 {**incomplete, "closure": {"started": True, "proved": True,
                                            "method": "supervisor_attest"}}) \
        == "incomplete_with_closure"
    # An incomplete run cannot be upgraded by adding a settled timestamp.
    assert _code(s, s.validate_receipt, {**incomplete, "finished_at": "2026-11-26T12:00:00Z"}) \
        == "incomplete_with_timestamp"
    # ...nor by dropping the failure class that explains the missing evidence.
    assert _code(s, s.validate_receipt, {**incomplete, "failure_class": None}) \
        == "missing_failure_class"


# --- closure coherence and trusted rendering (review F2) -----------------


def test_passed_receipt_cannot_render_without_trusted_scope(s, tree):
    """A pass is a host claim; it may only be serialized against the admitted scope."""
    scope = _scope(_manifest(tree))
    receipt = _receipt(s, scope, guest=_guest(s, scope))
    assert _code(s, s.render_receipt, receipt) == "passed_unbound"
    missing = {k: v for k, v in scope.items() if k != "fixture_set_sha256"}
    assert _code(s, s.render_receipt, receipt, missing) == "scope_invalid"

    # A non-pass still renders without a scope: it claims nothing that needs proof.
    failed = _receipt(s, scope, guest=None, status="failed", failure_class="startup_failed")
    assert json.loads(s.render_receipt(failed))["status"] == "failed"


@pytest.mark.parametrize("closure", [
    {"started": False, "proved": True, "method": "none"},
    {"started": False, "proved": True, "method": "backend_reconcile"},
    {"started": False, "proved": False, "method": "backend_reconcile"},
    {"started": True, "proved": True, "method": "none"},
    {"started": True, "proved": False, "method": "supervisor_attest"},
])
def test_contradictory_closure_evidence_refused(s, tree, closure):
    scope = _scope(_manifest(tree))
    receipt = _receipt(s, scope, guest=_guest(s, scope), closure=closure)
    assert _code(s, s.validate_receipt, receipt) == "closure_incoherent"


def test_coherent_closure_evidence_accepted(s, tree):
    scope = _scope(_manifest(tree))
    for closure in ({"started": True, "proved": True, "method": "supervisor_attest"},
                    {"started": True, "proved": True, "method": "backend_reconcile"}):
        receipt = _receipt(s, scope, guest=_guest(s, scope), closure=closure)
        assert s.validate_receipt(receipt, scope=scope)["closure"] == closure


def test_settled_timestamp_requires_started_timestamp(s, tree):
    """A finish without a start is a lifecycle contradiction, not a pass."""
    scope = _scope(_manifest(tree))
    receipt = _receipt(s, scope, guest=_guest(s, scope))
    assert _code(s, s.validate_receipt, {**receipt, "started_at": None}) == "missing_timestamp"
    failed = _receipt(s, scope, guest=None, status="failed", failure_class="startup_failed")
    assert _code(s, s.validate_receipt, {**failed, "started_at": None}) == "missing_timestamp"


# --- exercised coverage and declared omissions (review F3) ---------------


def test_fixture_route_checks_must_report_exercised_routes(s, tree):
    scope = _scope(_manifest(tree))
    # Internally consistent empty coverage: only the "a passing route check must
    # name its routes" rule can refuse this, so nothing else can mask it.
    empty = _guest(s, scope, exercised_routes=[], exercised_features=[])
    assert _code(s, s.validate_receipt, _receipt(s, scope, guest=empty)) == "coverage_invalid"
    assert _code(s, s.validate_receipt,
                 _receipt(s, scope, guest=_guest(s, scope, exercised_routes=None))) \
        == "invalid_type"

    guest = _guest(s, scope)
    del guest["exercised_routes"]
    assert _code(s, s.validate_receipt, _receipt(s, scope, guest=guest)) == "missing_field"


def test_fixture_route_check_result_must_match_route_records(s, tree):
    """The aggregate check and the per-route records may not disagree."""
    scope = _scope(_manifest(tree))

    # A passed aggregate check may not hide a failed route record.
    routes = _routes(s)
    routes[-1]["result"] = "failed"
    guest = _guest(s, scope, exercised_routes=routes)
    receipt = _receipt(s, scope, guest=guest, status="failed", failure_class="check_failed")
    assert _code(s, s.validate_receipt, receipt) == "coverage_invalid"

    # A failed aggregate check with an honest partial route list is accepted.
    guest["checks"]["fixture_routes"] = "failed"
    assert s.validate_receipt(receipt)["guest"]["exercised_routes"][-1]["result"] == "failed"

    # A failed check may also be reported with the routes that did complete and
    # pass before the failure; only a pass has to account for every route record.
    guest = _guest(s, scope)
    guest["checks"]["fixture_routes"] = "failed"
    receipt = _receipt(s, scope, guest=guest, status="failed", failure_class="check_failed")
    assert s.validate_receipt(receipt)["guest"]["exercised_routes"]


def test_route_records_are_fixed_vocabulary_and_bounded(s, tree):
    scope = _scope(_manifest(tree))
    for bad in ([{"id": "made_up", "feature": "health_endpoints", "result": "passed"}],
                [{"id": "health_liveness", "feature": "provider_routes", "result": "passed"}],
                [{"id": "health_liveness", "feature": "health_endpoints", "result": "skipped"}],
                [{"id": "health_liveness", "feature": "health_endpoints", "result": "passed",
                  "path": "/health/liveliness"}],
                ["health_liveness"],
                [{"id": "health_liveness", "feature": "health_endpoints", "result": "passed",
                  "detail": "free text"}],
                _routes(s) + [dict(_routes(s)[0])]):
        guest = _guest(s, scope, exercised_routes=bad)
        code = _code(s, s.validate_receipt, _receipt(s, scope, guest=guest))
        assert code in ("coverage_invalid", "unknown_field")


@pytest.mark.parametrize("identifier", [None, 1, True, [], {}])
def test_route_id_must_be_a_string(s, tree, identifier):
    scope = _scope(_manifest(tree))
    routes = _routes(s)
    routes[0] = {**routes[0], "id": identifier}
    guest = _guest(s, scope, exercised_routes=routes)
    receipt = _receipt(s, scope, guest=guest)
    assert _code(s, s.validate_receipt, receipt) == "coverage_invalid"


def test_exercised_features_must_match_exercised_routes(s, tree):
    """A feature may not be credited without a route that exercised it."""
    scope = _scope(_manifest(tree))
    guest = _guest(s, scope, exercised_features=["health_endpoints"])
    assert _code(s, s.validate_receipt, _receipt(s, scope, guest=guest)) == "coverage_invalid"
    guest = _guest(s, scope, exercised_features=["health_endpoints", "auth_checks",
                                                 "local_stub_route", "provider_routes"])
    assert _code(s, s.validate_receipt, _receipt(s, scope, guest=guest)) == "coverage_invalid"

    dropped = _guest(s, scope, exercised_routes=_routes(s, "stub_completion"),
                     exercised_features=["health_endpoints", "auth_checks"])
    assert s.validate_receipt(_receipt(s, scope, guest=dropped))["guest"]["exercised_features"] \
        == ("health_endpoints", "auth_checks")


def test_omissions_are_typed_records_and_empty_is_an_explicit_declaration(s, tree):
    scope = _scope(_manifest(tree))
    nothing = _guest(s, scope, exclusions=[])
    assert s.validate_receipt(_receipt(s, scope, guest=nothing))["guest"]["exclusions"] == ()
    assert json.loads(s.render_receipt(_receipt(s, scope, guest=nothing), scope=scope)) \
        ["guest"]["exclusions"] == []

    # Absent is not the same as declared-empty.
    guest = _guest(s, scope)
    del guest["exclusions"]
    assert _code(s, s.validate_receipt, _receipt(s, scope, guest=guest)) == "missing_field"

    for bad in ([{"feature": "provider_routes"}],
                [{"feature": "provider_routes", "reason": "because"}],
                [{"feature": "provider_routes", "reason": "not_in_scope", "note": "x"}],
                [{"feature": "local_stub_route", "reason": "not_in_scope"}],
                ["provider_routes"]):
        guest = _guest(s, scope, exclusions=bad)
        assert _code(s, s.validate_receipt, _receipt(s, scope, guest=guest)) == "exclusions_invalid"


def test_substitutions_are_declared_from_fixed_vocabulary(s, tree):
    scope = _scope(_manifest(tree))
    guest = _guest(s, scope, substitutions=[])
    assert s.validate_receipt(_receipt(s, scope, guest=guest))["guest"]["substitutions"] == ()
    for bad in (["real_provider"], ["local_stub_provider", "local_stub_provider"],
                "local_stub_provider", [{"name": "local_stub_provider"}]):
        guest = _guest(s, scope, substitutions=bad)
        code = _code(s, s.validate_receipt, _receipt(s, scope, guest=guest))
        assert code in ("invalid_substitution", "invalid_type")


# --- failure class typing and status pairing (review F4) -----------------


@pytest.mark.parametrize("failure_class", [[], {}, 7, True, ["check_failed"]])
def test_failure_class_must_be_a_string(s, tree, failure_class):
    scope = _scope(_manifest(tree))
    receipt = _receipt(s, scope, guest=None, status="failed", failure_class=failure_class)
    assert _code(s, s.validate_receipt, receipt) == "unknown_failure_class"


@pytest.mark.parametrize("status,failure_class", [
    ("refused", "check_failed"),
    ("refused", "closure_uncertain"),
    ("failed", "input_refused"),
    ("failed", "worker_lost"),
    ("incomplete", "startup_failed"),
    ("incomplete", "isolation_unavailable"),
])
def test_failure_class_must_pair_with_status(s, tree, status, failure_class):
    scope = _scope(_manifest(tree))
    closure = {"started": True, "proved": True, "method": "supervisor_attest"}
    finished = "2026-11-26T12:00:00Z"
    if status == "refused":
        closure = {"started": False, "proved": False, "method": "none"}
    elif status == "incomplete":
        closure = {"started": True, "proved": False, "method": "none"}
        finished = None
    receipt = _receipt(s, scope, guest=None, status=status, failure_class=failure_class,
                       closure=closure, finished_at=finished)
    assert _code(s, s.validate_receipt, receipt) == "failure_class_status_mismatch"


# --- reviewed-bytes admission of bundle contents (review F1) -------------


def test_bundle_entries_get_the_same_admission_as_top_level_files(s, tree, tmp_path):
    """Defensive contract: packaged guest files are admitted like reviewed files.

    The bundle digest must not be the one place that follows an alias, hashes a
    second name for the same inode, or reads bytes from outside the root.
    """
    bundle = tree / "second bundle"
    bundle.mkdir()
    payload = bundle / "payload.txt"
    payload.write_text("synthetic payload\n", encoding="utf-8")
    clean = _manifest(tree)
    clean["runtime_bundle"] = {"path": str(bundle), "sha256": _digest(bundle)}
    loaded = s.validate_manifest(clean, input_root=str(tree))
    assert loaded["runtime_bundle"]["path"]
    # Admission does not change the packaged-bytes identity of a clean bundle.
    assert loaded["runtime_bundle"]["sha256"] == _tree_sha(bundle)
    entry = {"path": str(bundle), "sha256": "a" * 64}

    os.symlink(payload, bundle / "alias.txt")
    manifest = _manifest(tree)
    manifest["runtime_bundle"] = entry
    assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "symlink_refused"
    os.unlink(bundle / "alias.txt")

    os.link(payload, bundle / "shared.txt")
    manifest = _manifest(tree)
    manifest["runtime_bundle"] = entry
    assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "multiply_linked"
    os.unlink(bundle / "shared.txt")

    nested = bundle / "lib"
    nested.mkdir()
    (nested / "module.py").write_text("value = 1\n", encoding="utf-8")
    os.symlink(nested, bundle / "lib alias")
    manifest = _manifest(tree)
    manifest["runtime_bundle"] = entry
    assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "symlink_refused"
    os.unlink(bundle / "lib alias")

    writable = nested / "module.py"
    writable.chmod(0o666)
    try:
        manifest = _manifest(tree)
        manifest["runtime_bundle"] = entry
        assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "writable_input"
    finally:
        writable.chmod(0o644)

    outside = tmp_path / "outside bundle"
    outside.mkdir()
    (outside / "payload.txt").write_text("synthetic payload\n", encoding="utf-8")
    manifest = _manifest(tree)
    manifest["runtime_bundle"] = {"path": str(outside), "sha256": _digest(outside)}
    assert _code(s, s.validate_manifest, manifest, input_root=str(tree)) == "path_escape"


def test_root_form_tolerance_survives_resolved_containment(s, tree, tmp_path):
    """A volume-alias ancestor is not an escape; the bytes still come from root.

    This mirrors macOS ``/var`` -> ``/private/var``: the configured root is a
    real directory reached through an aliased ancestor, so the lexical and
    resolved forms differ while both name the same tree.
    """
    storage = tmp_path / "storage"
    storage.mkdir()
    volume = storage / "volume"
    tree.rename(volume)
    mount = tmp_path / "mnt"
    os.symlink(storage, mount)
    try:
        alias = str(mount / "volume")
        real = str(volume)
        assert alias != real
        assert os.path.realpath(alias) == os.path.realpath(real)
        assert not os.path.islink(alias)
        manifest = _manifest(volume)
        for name in ("overlay", "pin", "fixture_set", "runtime_bundle", "native_receipt"):
            manifest[name]["path"] = alias + manifest[name]["path"][len(real):]
        loaded = s.validate_manifest(manifest, input_root=alias)
        assert loaded["overlay"]["path"] == str(volume / "overlay.yaml")
        # Canonical entries also work with either configured root spelling.
        canonical = _manifest(volume.resolve())
        assert s.validate_manifest(canonical, input_root=alias) == loaded
        assert s.validate_manifest(canonical, input_root=real) == loaded
        # Configuring the canonical root does not approve a separate alias.
        assert _code(s, s.validate_manifest, manifest, input_root=real) == "path_escape"
        # The alias still cannot carry a path out of the root.
        escaped = _manifest(volume)
        escaped["pin"] = {"path": str(tmp_path / "elsewhere"), "sha256": "a" * 64}
        assert _code(s, s.validate_manifest, escaped, input_root=alias) == "path_escape"
    finally:
        os.unlink(mount)


# --- frame discipline -----------------------------------------------------


def test_repeated_trailing_empty_and_oversize_frames_refused(s, tree):
    scope = _scope(_manifest(tree))
    frame = s.render_receipt(_receipt(s, scope, guest=_guest(s, scope)), scope=scope)
    assert s.load_receipt(frame, scope=scope)["status"] == "passed"
    assert s.decode_frame(b"\n" + frame)  # bounded leading whitespace is tolerated

    assert _code(s, s.load_receipt, frame + frame, scope=scope) == "frame_repeated"
    assert _code(s, s.load_receipt, frame + b"garbage") == "frame_trailing_data"
    assert _code(s, s.load_receipt, b"") == "empty_frame"
    assert _code(s, s.load_receipt, None) == "invalid_frame"
    assert _code(s, s.load_receipt, frame + b"\x00" * s.MAX_FRAME_BYTES) == "frame_oversize"
    assert s.MAX_FRAME_BYTES == 65536


# --- effective launch identity -------------------------------------------


def test_effective_launch_digest_binds_fixture_and_settings_only(s):
    fixture = b"model_list:\n  - model_name: local\n    litellm_params:\n      model: fixture/model\n"
    base = s.effective_launch_digest(fixture, {"fixture_trace": False})
    assert len(base) == 64 and base == base.lower()
    assert base == s.effective_launch_digest(fixture)
    assert base == s.effective_launch_digest(fixture, {"fixture_trace": False})
    assert base != s.effective_launch_digest(fixture, {"fixture_trace": True})
    assert base != s.effective_launch_digest(fixture.replace(b"local", b"other"),
                                             {"fixture_trace": False})

    secret = "fixture-only-secret-value"
    for settings in ({"LITELLM_MASTER_KEY": secret}, {"fixture_trace": False, "api_key": secret},
                     {"fixture_trace": "false"}):
        with pytest.raises(s.SchemaError) as caught:
            s.effective_launch_digest(fixture, settings)
        assert caught.value.code in ("unknown_behavior", "invalid_type")
        assert secret not in str(caught.value)

    assert _code(s, s.effective_launch_digest, b"", {"fixture_trace": False}) == "fixture_invalid"
    assert _code(s, s.effective_launch_digest, b"a\x00b", {}) == "fixture_invalid"


def test_ephemeral_key_never_enters_identity_or_receipt(s, tree):
    manifest = _manifest(tree)
    manifest["fixture_trace"] = True
    loaded = s.validate_manifest(manifest, input_root=str(tree))
    settings = s.launch_settings(loaded)
    assert settings == {"fixture_trace": True}

    fixture = (tree / "fixtures.yaml").read_bytes()
    effective = s.effective_launch_digest(fixture, settings)
    scope = {**_scope(manifest), "effective_launch_sha256": effective}
    receipt = _receipt(s, scope, guest=_guest(s, scope))
    rendered = s.render_receipt(receipt, scope=scope)
    assert b'"sk-' not in rendered
    assert b"ephemeral" not in rendered.lower()
    assert effective.encode() in rendered
