"""Closed wire schemas for the isolated rehearsal (FR-001, FR-006).

Three distinct surfaces, each refusing rather than repairing:

``manifest``
    The operator-supplied version-1 proposal. Its bytes are pinned by a
    separately reviewed SHA-256, so loading verifies the reviewed digest before
    parsing; every file entry names an absolute path inside one configured
    input root plus the lowercase 64-hex digest of the bytes actually admitted.
``receipt``
    The sanitized public record: status vocabulary, nullable guest result,
    failure class, supervisor closure proof and timestamps. Host paths and
    input filenames are never receipt values. A failure class must belong to
    the status that reports it, closure evidence must be internally coherent
    (it is the host's proof, never the guest's), and a ``passed`` receipt can
    only be rendered against the admitted run scope.

The guest result carries coverage as closed evidence: fixed-vocabulary route
records for exactly the fixture routes exercised, the features those routes
account for, declared substitutions, and typed omission records. An empty
omission list is a declaration that nothing was omitted; the field is always
present, so silence can never be read as full coverage.
``binding``
    Shape validity is not admission. ``check_binding`` compares a parsed
    receipt against the admitted run scope (run ID, overlay/pin/proposal,
    fixture-set, effective launch identity and runtime digests), so an
    otherwise valid frame naming the wrong run or a different proposal is
    refused.

Identity hashing lives in one place: ``effective_launch_digest`` binds the
sanitized fixture YAML plus the resolved behavior-affecting settings and
refuses unknown behaviors instead of hashing them. The ephemeral test key is
not a representable setting, so it can never enter a digest or a receipt.

Pure standard library; no third-party dependency and no installation step.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat

__all__ = [
    "EXCLUSIONS", "EXERCISED_FEATURES", "FAILURE_CLASSES", "FIXTURE_ROUTES",
    "FrameError", "MAX_FRAME_BYTES", "OMISSION_REASONS",
    "REQUIRED_CHECKS", "ROUTE_RESULTS", "SCHEMA_VERSION", "STATUSES",
    "SUBSTITUTIONS", "SchemaError", "check_binding", "decode_frame",
    "effective_launch_digest", "launch_settings", "load_manifest", "load_receipt",
    "manifest_digest", "render_manifest", "render_receipt", "strict_json",
    "validate_manifest", "validate_receipt",
]

SCHEMA_VERSION = 1
MAX_FRAME_BYTES = 65536
_HEX64 = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"[0-9a-f]{32}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_IMAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

#: Fixed check vocabulary; the order is the required order in a guest result.
REQUIRED_CHECKS = (
    "environment_admission",
    "runtime_provenance",
    "patch_sentinel",
    "liveness",
    "readiness",
    "unauthenticated_rejection",
    "test_key_acceptance",
    "fixture_routes",
    "identity_continuity",
)

#: Fixed feature vocabulary: what a rehearsal result can claim to have exercised.
EXERCISED_FEATURES = ("health_endpoints", "auth_checks", "local_stub_route")

#: Route identifier -> the one feature it exercises (FR-004 exact attribution).
FIXTURE_ROUTES = {
    "health_liveness": "health_endpoints",
    "health_readiness": "health_endpoints",
    "unauthenticated_rejection": "auth_checks",
    "test_key_acceptance": "auth_checks",
    "stub_completion": "local_stub_route",
}

ROUTE_RESULTS = ("passed", "failed")

#: Fixed substitution vocabulary: what the rehearsal used instead of production.
SUBSTITUTIONS = ("local_stub_provider", "test_key_substitution")

#: Fixed exclusion vocabulary: what a rehearsal result explicitly does not cover.
EXCLUSIONS = ("provider_routes", "real_credentials", "production_features")

#: Why an omission was declared; free-form prose never becomes a receipt value.
OMISSION_REASONS = ("not_in_scope", "unsupported", "not_exercised")

STATUSES = ("refused", "failed", "incomplete", "passed")

#: Per-status failure vocabulary. ``passed`` carries none.
FAILURE_CLASSES = {
    "refused": ("input_refused", "isolation_unavailable", "scope_occupied"),
    "failed": ("check_failed", "identity_changed", "startup_failed", "teardown_failed"),
    "incomplete": ("closure_uncertain", "owner_lost", "worker_lost"),
    "passed": (),
}

#: Supervisor closure methods; ``none`` is honest only when nothing started.
CLOSURE_METHODS = ("supervisor_attest", "backend_reconcile", "none")

#: Every class in the vocabulary, before the per-status pairing is applied.
_ALL_FAILURE_CLASSES = frozenset(
    name for names in FAILURE_CLASSES.values() for name in names)

#: The only optional version-1 fixture setting, with its fixed type.
LAUNCH_SETTINGS = {"fixture_trace": bool}

_FILE_ENTRY_FIELDS = frozenset({"path", "sha256"})
_IMAGE_FIELDS = frozenset({"id", "sha256"})
_MANIFEST_REQUIRED = frozenset({"version", "overlay", "pin", "runtime_bundle",
                                "guest_image", "fixture_set"})
_MANIFEST_OPTIONAL = frozenset({"native_receipt", "fixture_trace"})
_MANIFEST_FIELDS = _MANIFEST_REQUIRED | _MANIFEST_OPTIONAL
_GUEST_FIELDS = frozenset({"version", "run_id", "overlay_sha256", "pin_sha256",
                           "proposal_sha256", "fixture_set_sha256",
                           "effective_launch_sha256", "runtime_sha256", "checks",
                           "exercised_routes", "exercised_features", "substitutions",
                           "exclusions"})
_ROUTE_FIELDS = frozenset({"id", "feature", "result"})
_OMISSION_FIELDS = frozenset({"feature", "reason"})
_RECEIPT_FIELDS = frozenset({"version", "run_id", "status", "guest", "failure_class",
                             "closure", "started_at", "finished_at"})
_SCOPE_FIELDS = frozenset({"run_id", "overlay_sha256", "pin_sha256", "proposal_sha256",
                           "fixture_set_sha256", "effective_launch_sha256",
                           "runtime_sha256"})
_CLOSURE_FIELDS = frozenset({"started", "proved", "method"})

# Serialized guest field -> admitting-scope field, for the binding comparison.
_BOUND_GUEST_FIELDS = {
    "run_id": "run_id",
    "overlay_sha256": "overlay_sha256",
    "pin_sha256": "pin_sha256",
    "proposal_sha256": "proposal_sha256",
    "fixture_set_sha256": "fixture_set_sha256",
    "effective_launch_sha256": "effective_launch_sha256",
    "runtime_sha256": "runtime_sha256",
}


class SchemaError(ValueError):
    """Closed-schema refusal. ``code`` is fixed, path-free vocabulary."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class FrameError(SchemaError):
    """A frame could not be read as exactly one bounded record."""


# --- strict decoding ------------------------------------------------------


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise SchemaError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(_):
    raise SchemaError("invalid_constant")


_DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_reject_constant)


def strict_json(raw):
    """Decode JSON refusing duplicate keys and non-JSON constants."""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise SchemaError("invalid_frame") from None
    if not isinstance(raw, str):
        raise SchemaError("invalid_frame")
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except SchemaError:
        raise
    except (ValueError, TypeError, RecursionError):
        raise SchemaError("invalid_frame") from None


def _canonical(obj):
    """Ascii-only canonical JSON: no surprise encodings in a public record."""
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, OverflowError):
        raise SchemaError("invalid_value") from None


# --- shared field checks --------------------------------------------------


def _object(value, fields):
    if not isinstance(value, dict):
        raise SchemaError("invalid_type")
    if set(value) - fields:
        raise SchemaError("unknown_field")


def _required_fields(value, required):
    if required - set(value):
        raise SchemaError("missing_field")


def _version(value):
    if type(value) is not int or value != SCHEMA_VERSION:
        raise SchemaError("unsupported_version")


def _digest_value(value):
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise SchemaError("digest_invalid")


def _run_id(value):
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
        raise SchemaError("invalid_run_id")


def _timestamp(value):
    if value is None:
        return
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise SchemaError("invalid_timestamp")


# --- manifest -------------------------------------------------------------


def _input_root(input_root):
    """The configured reviewed input root.

    Absolute, existing, a real directory, dedicated (not the filesystem root)
    and not writable by group or other. A configured root that is itself a
    symlink is an unaudited filesystem source and refuses. The resolved root is
    returned so later comparisons use one canonical form (macOS volume
    aliases such as /var -> /private/var are not a path escape).
    """
    if not isinstance(input_root, str) or not input_root or not os.path.isabs(input_root):
        raise SchemaError("invalid_input_root")
    if os.path.normpath(input_root) != input_root or os.path.normpath(input_root) == os.sep:
        raise SchemaError("invalid_input_root")
    if os.path.islink(input_root):
        raise SchemaError("invalid_input_root")
    try:
        info = os.stat(input_root)
    except OSError:
        raise SchemaError("invalid_input_root") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise SchemaError("invalid_input_root")
    # Keep both forms: the operator's absolute root for the lexical component
    # walk and the resolved root for containment, so a volume alias in an
    # *ancestor* is not an escape while the root itself stays a real directory.
    return os.path.abspath(input_root), os.path.realpath(input_root)


def _admit_path(value, roots, *, directory=False):
    """Require both an approved path spelling and canonical containment.

    The configured root may have an aliased ancestor (for example, macOS
    ``/var``). Only that root spelling and its canonical form are accepted;
    neither permits symlinks below the root.
    """
    given_root, real_root = roots
    if not isinstance(value, str) or not os.path.isabs(value):
        raise SchemaError("invalid_path")
    # Do not normalize away components before checking the supplied path.
    if os.path.normpath(value) != value:
        raise SchemaError("path_escape")
    if value.startswith(given_root + os.sep):
        base = given_root
    elif value.startswith(real_root + os.sep):
        base = real_root
    else:
        raise SchemaError("path_escape")
    probe = base
    for part in value[len(base) + 1:].split(os.sep):
        probe = os.path.join(probe, part)
        if os.path.islink(probe):
            raise SchemaError("symlink_refused")
    resolved = os.path.realpath(value)
    if not resolved.startswith(real_root + os.sep):
        raise SchemaError("path_escape")
    try:
        info = os.lstat(resolved)
    except OSError:
        raise SchemaError("file_missing") from None
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise SchemaError("not_a_directory")
        if info.st_mode & 0o022:
            raise SchemaError("writable_input")
        return resolved
    if not stat.S_ISREG(info.st_mode):
        raise SchemaError("not_a_file")
    if info.st_nlink != 1:
        raise SchemaError("multiply_linked")
    if info.st_mode & 0o022:
        raise SchemaError("writable_input")
    return resolved


def _file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _bundle_digest(path, roots):
    """Canonical packaged-bytes digest for a runtime bundle directory.

    Path, then bytes, NUL-delimited per file: no ordering or boundary ambiguity
    and no host metadata in the identity. Every packaged child is admitted with
    the same checks as a top-level reviewed file (resolved containment, no
    symlink component, single link, not writable) before it is hashed, so the
    bundle is never the one place that follows an alias or reads other bytes.
    """
    digest = hashlib.sha256()
    base = os.path.abspath(path)
    pending = [base]
    files = []
    while pending:
        current = pending.pop()
        try:
            children = sorted(os.listdir(current))
        except OSError:
            raise SchemaError("file_missing") from None
        for name in children:
            candidate = os.path.join(current, name)
            child = _admit_path(candidate, roots, directory=os.path.isdir(candidate))
            if os.path.isdir(child):
                pending.append(child)
                continue
            files.append(child)
    # Ordered by path parts, the canonical walk order of prior digests.
    for child in sorted(files, key=lambda item: tuple(os.path.relpath(item, base).split(os.sep))):
        digest.update(os.path.relpath(child, base).replace(os.sep, "/").encode("utf-8"))
        digest.update(b"\0")
        with open(child, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def _entry(value, root, *, directory=False):
    _object(value, _FILE_ENTRY_FIELDS)
    _required_fields(value, _FILE_ENTRY_FIELDS)
    _digest_value(value["sha256"])
    path = _admit_path(value["path"], root, directory=directory)
    actual = _bundle_digest(path, root) if directory else _file_digest(path)
    if actual != value["sha256"]:
        raise SchemaError("file_digest_mismatch")
    return {"path": path, "sha256": value["sha256"]}


def _image(value):
    _object(value, _IMAGE_FIELDS)
    _required_fields(value, _IMAGE_FIELDS)
    if not isinstance(value["id"], str) or not _IMAGE_ID.fullmatch(value["id"]):
        raise SchemaError("invalid_image")
    _digest_value(value["sha256"])
    return {"id": value["id"], "sha256": value["sha256"]}


def _normalize_manifest(manifest, input_root):
    """Validate and normalize.

    ``input_root=None`` produces canonical bytes for review/signing without
    admitting files. With a root, every entry is admitted against real bytes.
    """
    if not isinstance(manifest, dict):
        raise SchemaError("invalid_type")
    _object(manifest, _MANIFEST_FIELDS)
    _required_fields(manifest, _MANIFEST_REQUIRED)
    _version(manifest["version"])

    trace = manifest.get("fixture_trace", False)
    if type(trace) is not bool:
        raise SchemaError("invalid_type")
    native = manifest.get("native_receipt")

    if input_root is None:
        out = {"version": SCHEMA_VERSION, "fixture_trace": trace}
        for name in ("overlay", "pin", "runtime_bundle", "fixture_set"):
            value = manifest[name]
            _object(value, _FILE_ENTRY_FIELDS)
            _required_fields(value, _FILE_ENTRY_FIELDS)
            _digest_value(value["sha256"])
            if not isinstance(value["path"], str) or not os.path.isabs(value["path"]):
                raise SchemaError("invalid_path")
            out[name] = {"path": value["path"], "sha256": value["sha256"]}
        if native is not None:
            _object(native, _FILE_ENTRY_FIELDS)
            _required_fields(native, _FILE_ENTRY_FIELDS)
            _digest_value(native["sha256"])
            if not isinstance(native["path"], str) or not os.path.isabs(native["path"]):
                raise SchemaError("invalid_path")
            out["native_receipt"] = {"path": native["path"], "sha256": native["sha256"]}
        else:
            out["native_receipt"] = None
        out["guest_image"] = _image(manifest["guest_image"])
        return out

    root = _input_root(input_root)
    out = {"version": SCHEMA_VERSION}
    for name in ("overlay", "pin", "fixture_set"):
        out[name] = _entry(manifest[name], root)
    out["runtime_bundle"] = _entry(manifest["runtime_bundle"], root, directory=True)
    out["native_receipt"] = None if native is None else _entry(native, root)
    out["guest_image"] = _image(manifest["guest_image"])
    out["fixture_trace"] = trace

    # Distinct reviewed inputs: one file may not fill two roles.
    seen = {}
    for name in ("overlay", "pin", "fixture_set", "native_receipt"):
        if out[name] is None:
            continue
        if out[name]["path"] in seen:
            raise SchemaError("duplicate_path")
        seen[out[name]["path"]] = name
    if out["runtime_bundle"]["path"] in seen:
        raise SchemaError("duplicate_path")
    return out


def manifest_digest(manifest, input_root=None):
    """Digest of the canonical manifest bytes a reviewer signs."""
    return hashlib.sha256(_canonical(_normalize_manifest(manifest, input_root))).hexdigest()


def render_manifest(manifest, input_root=None):
    return _canonical(_normalize_manifest(manifest, input_root))


def validate_manifest(manifest, input_root=None):
    return _normalize_manifest(manifest, input_root)


def load_manifest(raw, reviewed_sha256, input_root):
    """Admit reviewed manifest bytes.

    The separately supplied reviewed digest pins the exact bytes *before*
    parsing, so replacing the manifest together with all its internal hashes
    cannot enter execution without a new operator review.
    """
    if not isinstance(reviewed_sha256, str) or not _HEX64.fullmatch(reviewed_sha256):
        raise SchemaError("reviewed_digest_invalid")
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise SchemaError("invalid_frame")
    raw = bytes(raw)
    if len(raw) > MAX_FRAME_BYTES:
        raise SchemaError("frame_oversize")
    if hashlib.sha256(raw).hexdigest() != reviewed_sha256:
        raise SchemaError("manifest_digest_mismatch")
    return _normalize_manifest(strict_json(raw), input_root)


# --- receipt --------------------------------------------------------------


def _checks(value):
    if not isinstance(value, dict):
        raise SchemaError("invalid_type")
    if set(value) - set(REQUIRED_CHECKS):
        raise SchemaError("unknown_check")
    if set(REQUIRED_CHECKS) - set(value):
        raise SchemaError("missing_check")
    for name in REQUIRED_CHECKS:
        if value[name] not in ("passed", "failed"):
            raise SchemaError("invalid_check_result")
    return {name: value[name] for name in REQUIRED_CHECKS}


def _coverage_list(value):
    """A coverage field is an explicit list; cardinality is fixed by vocabulary."""
    if not isinstance(value, list):
        raise SchemaError("invalid_type")
    return value


def _exercised_routes(value):
    """Exact FR-004 coverage: fixed route identifiers, each attributed once.

    A route record is a closed fixed-vocabulary tuple; request paths, probe
    bodies or free-form detail are not representable, so a route claim can
    never carry a passthrough string.
    """
    routes = []
    for item in _coverage_list(value):
        # A closed record: extra detail, a missing member or a non-record is a
        # coverage claim the schema cannot represent, not a tolerable variant.
        if not isinstance(item, dict) or set(item) != _ROUTE_FIELDS:
            raise SchemaError("coverage_invalid")
        identifier = item["id"]
        if not isinstance(identifier, str) or identifier not in FIXTURE_ROUTES:
            raise SchemaError("coverage_invalid")
        if item["feature"] != FIXTURE_ROUTES[identifier]:
            raise SchemaError("coverage_invalid")
        if item["result"] not in ROUTE_RESULTS:
            raise SchemaError("coverage_invalid")
        routes.append({"id": identifier, "feature": item["feature"],
                       "result": item["result"]})
    order = list(FIXTURE_ROUTES)
    if len({route["id"] for route in routes}) != len(routes):
        raise SchemaError("coverage_invalid")
    # Canonical order is required, not imposed: reordering is a repair.
    if [route["id"] for route in routes] != sorted(
            (route["id"] for route in routes), key=order.index):
        raise SchemaError("coverage_invalid")
    return tuple(routes)


def _exercised_features(value, routes):
    """Features credited only when a route record actually exercised them."""
    listed = _coverage_list(value)
    if any(not isinstance(item, str) or item not in EXERCISED_FEATURES for item in listed):
        raise SchemaError("coverage_invalid")
    observed = {route["feature"] for route in routes}
    # Canonical order and exact set: this also rejects duplicates, a credited
    # feature with no route, and a route whose feature was not credited.
    if listed != [name for name in EXERCISED_FEATURES if name in observed]:
        raise SchemaError("coverage_invalid")
    return tuple(listed)


def _substitutions(value):
    """Declared substitutes for production behavior (FR-006)."""
    listed = _coverage_list(value)
    if any(not isinstance(item, str) or item not in SUBSTITUTIONS for item in listed):
        raise SchemaError("invalid_substitution")
    if listed != [name for name in SUBSTITUTIONS if name in listed]:
        raise SchemaError("invalid_substitution")
    return tuple(listed)


def _exclusions(value):
    """Declared omissions: typed records, never bare or absent-implies-nothing."""
    omissions = []
    for item in _coverage_list(value):
        if not isinstance(item, dict) or set(item) != _OMISSION_FIELDS:
            raise SchemaError("exclusions_invalid")
        if item["feature"] not in EXCLUSIONS or item["reason"] not in OMISSION_REASONS:
            raise SchemaError("exclusions_invalid")
        omissions.append({"feature": item["feature"], "reason": item["reason"]})
    order = list(EXCLUSIONS)
    if len({omission["feature"] for omission in omissions}) != len(omissions):
        raise SchemaError("exclusions_invalid")
    if [omission["feature"] for omission in omissions] != sorted(
            (omission["feature"] for omission in omissions), key=order.index):
        raise SchemaError("exclusions_invalid")
    return tuple(omissions)


def _guest(value):
    _object(value, _GUEST_FIELDS)
    _required_fields(value, _GUEST_FIELDS)
    _version(value["version"])
    _run_id(value["run_id"])
    for name in ("overlay_sha256", "pin_sha256", "proposal_sha256",
                 "fixture_set_sha256", "effective_launch_sha256", "runtime_sha256"):
        _digest_value(value[name])
    checks = _checks(value["checks"])
    routes = _exercised_routes(value["exercised_routes"])
    # FR-004: a passing fixture-route check must name exactly the routes that
    # ran, and a route that failed cannot sit under a passed aggregate check.
    # A failed check may honestly list a partial set (it can fail before every
    # route is reached), so no minimum is imposed there.
    if checks["fixture_routes"] == "passed" and (
            not routes or any(route["result"] == "failed" for route in routes)):
        raise SchemaError("coverage_invalid")
    return {
        "version": SCHEMA_VERSION,
        "run_id": value["run_id"],
        "overlay_sha256": value["overlay_sha256"],
        "pin_sha256": value["pin_sha256"],
        "proposal_sha256": value["proposal_sha256"],
        "fixture_set_sha256": value["fixture_set_sha256"],
        "effective_launch_sha256": value["effective_launch_sha256"],
        "runtime_sha256": value["runtime_sha256"],
        "checks": checks,
        "exercised_routes": routes,
        "exercised_features": _exercised_features(value["exercised_features"], routes),
        "substitutions": _substitutions(value["substitutions"]),
        "exclusions": _exclusions(value["exclusions"]),
    }


def _closure(value):
    """Host closure evidence; contradictory combinations are refused, not resolved.

    ``proved`` and a real ``method`` are the same claim, and neither is possible
    when nothing started: a closure that says it proved teardown of a run it
    never started (or that proved nothing by a real method) is incoherent, not
    merely unusual.
    """
    _object(value, _CLOSURE_FIELDS)
    _required_fields(value, _CLOSURE_FIELDS)
    if type(value["started"]) is not bool or type(value["proved"]) is not bool:
        raise SchemaError("invalid_type")
    if value["method"] not in CLOSURE_METHODS:
        raise SchemaError("unknown_closure_method")
    started, proved, method = value["started"], value["proved"], value["method"]
    if proved != (method != "none"):
        raise SchemaError("closure_incoherent")
    if (proved or method != "none") and not started:
        raise SchemaError("closure_incoherent")
    return {"started": started, "proved": proved, "method": method}


def _scope(value):
    """The admitted run scope; a malformed scope makes every binding unprovable."""
    try:
        if not isinstance(value, dict) or set(value) != _SCOPE_FIELDS:
            raise SchemaError("scope_invalid")
        _run_id(value["run_id"])
        for name in sorted(_SCOPE_FIELDS - {"run_id"}):
            _digest_value(value[name])
    except SchemaError:
        raise SchemaError("scope_invalid") from None
    return value


def check_binding(receipt, scope):
    """Compare a shape-valid receipt with the exclusively reserved run scope."""
    if receipt["run_id"] != scope["run_id"]:
        raise SchemaError("receipt_run_mismatch")
    if receipt["guest"] is None:
        return
    if receipt["guest"]["run_id"] != scope["run_id"]:
        raise SchemaError("receipt_run_mismatch")
    for name, scope_name in _BOUND_GUEST_FIELDS.items():
        if receipt["guest"][name] != scope[scope_name]:
            raise SchemaError("receipt_proposal_mismatch")


def _shape(receipt):
    _object(receipt, _RECEIPT_FIELDS)
    _required_fields(receipt, _RECEIPT_FIELDS)
    _version(receipt["version"])
    _run_id(receipt["run_id"])

    status = receipt["status"]
    if status not in STATUSES:
        raise SchemaError("unknown_status")
    # Typed before paired: a list here is a refusal, never an uncaught TypeError.
    failure_class = receipt["failure_class"]
    if failure_class is not None and not isinstance(failure_class, str):
        raise SchemaError("unknown_failure_class")
    if failure_class is not None and failure_class not in _ALL_FAILURE_CLASSES:
        raise SchemaError("unknown_failure_class")

    guest = None if receipt["guest"] is None else _guest(receipt["guest"])
    closure = _closure(receipt["closure"])
    _timestamp(receipt["started_at"])
    _timestamp(receipt["finished_at"])

    # A class explains one specific settlement; carrying another status's class
    # would attribute the outcome to a cause the state machine never allows.
    if status != "passed" and failure_class is not None and \
            failure_class not in FAILURE_CLASSES[status]:
        raise SchemaError("failure_class_status_mismatch")

    if status == "passed":
        if guest is None:
            raise SchemaError("passed_without_guest")
        if failure_class is not None:
            raise SchemaError("passed_with_failure_class")
        if not closure["proved"]:
            raise SchemaError("passed_without_closure")
        if any(result != "passed" for result in guest["checks"].values()):
            raise SchemaError("passed_with_failed_check")
        # Route coverage is enforced once, in _guest, for every status.
        if receipt["started_at"] is None:
            raise SchemaError("missing_timestamp")
        if receipt["finished_at"] is None:
            raise SchemaError("missing_timestamp")

    elif status == "refused":
        if failure_class is None:
            raise SchemaError("missing_failure_class")
        if closure["started"]:
            raise SchemaError("refused_after_start")
        if guest is not None:
            raise SchemaError("refused_with_guest")
        if receipt["started_at"] is not None:
            raise SchemaError("refused_after_start")

    elif status == "failed":
        if failure_class is None:
            raise SchemaError("failed_without_class")
        if not closure["proved"]:
            raise SchemaError("failed_without_closure")
        if receipt["started_at"] is None:
            raise SchemaError("missing_timestamp")
        if receipt["finished_at"] is None:
            raise SchemaError("missing_timestamp")

    else:  # incomplete: unknown closure may never settle; run ID is retained
        if closure["proved"]:
            raise SchemaError("incomplete_with_closure")
        if receipt["finished_at"] is not None:
            raise SchemaError("incomplete_with_timestamp")
        if failure_class is None:
            raise SchemaError("missing_failure_class")

    return {
        "version": SCHEMA_VERSION,
        "run_id": receipt["run_id"],
        "status": status,
        "guest": guest,
        "failure_class": failure_class,
        "closure": closure,
        "started_at": receipt["started_at"],
        "finished_at": receipt["finished_at"],
    }


def validate_receipt(receipt, scope=None):
    """Validate a receipt; with ``scope``, also prove the run/proposal binding."""
    normalized = _shape(receipt)
    if scope is not None:
        check_binding(normalized, _scope(scope))
    return normalized


def _external(receipt):
    out = dict(receipt)
    if out["guest"] is not None:
        guest = dict(out["guest"])
        guest["exercised_routes"] = [dict(route) for route in guest["exercised_routes"]]
        guest["exercised_features"] = list(guest["exercised_features"])
        guest["substitutions"] = list(guest["substitutions"])
        guest["exclusions"] = [dict(omission) for omission in guest["exclusions"]]
        out["guest"] = guest
    return out


def render_receipt(receipt, scope=None):
    """Canonical, bounded, sanitized render of an admitted receipt.

    A ``passed`` receipt may only be serialized against the run scope the host
    admitted: the pass is a host claim, and rendering it without the run/identity
    binding would publish an unbound success.
    """
    normalized = validate_receipt(receipt, scope=scope)
    if normalized["status"] == "passed" and scope is None:
        raise SchemaError("passed_unbound")
    frame = _canonical(_external(normalized))
    if len(frame) > MAX_FRAME_BYTES:
        raise SchemaError("frame_oversize")
    return frame


# --- frame discipline -----------------------------------------------------


def decode_frame(raw):
    """Read exactly one bounded record: no repeat, no trailing bytes."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise FrameError("invalid_frame")
    frame = bytes(raw)
    if not frame:
        raise FrameError("empty_frame")
    if len(frame) > MAX_FRAME_BYTES:
        raise FrameError("frame_oversize")
    try:
        text = frame.decode("utf-8")
    except UnicodeDecodeError:
        raise FrameError("invalid_frame") from None
    text = text.lstrip("\t\n\r")
    if not text:
        raise FrameError("empty_frame")
    if not text.startswith("{"):
        raise FrameError("invalid_frame")
    try:
        parsed, end = _DECODER.raw_decode(text)
    except SchemaError:
        raise
    except (ValueError, RecursionError):
        raise FrameError("invalid_frame") from None
    rest = text[end:]
    if rest.strip():
        if rest.lstrip("\t\n\r ").startswith("{"):
            raise FrameError("frame_repeated")
        raise FrameError("frame_trailing_data")
    if not isinstance(parsed, dict):
        raise FrameError("invalid_frame")
    return parsed


def load_receipt(raw, scope=None):
    return validate_receipt(decode_frame(raw), scope=scope)


# --- effective launch identity -------------------------------------------


def launch_settings(manifest):
    """Resolved behavior-affecting settings for an admitted manifest."""
    trace = manifest.get("fixture_trace", False)
    if type(trace) is not bool:
        raise SchemaError("invalid_type")
    return {"fixture_trace": trace}


def effective_launch_digest(fixture_yaml, settings=None):
    """Bind the sanitized fixture YAML plus the resolved settings.

    Unknown settings refuse rather than being hashed: a credential or an
    arbitrary environment map must never become identity material.
    """
    if not isinstance(fixture_yaml, (bytes, bytearray, memoryview)):
        raise SchemaError("fixture_invalid")
    fixture = bytes(fixture_yaml)
    if not fixture or b"\0" in fixture:
        raise SchemaError("fixture_invalid")
    try:
        text = fixture.decode("utf-8")
    except UnicodeDecodeError:
        raise SchemaError("fixture_invalid") from None
    if not text.strip():
        raise SchemaError("fixture_invalid")

    resolved = {} if settings is None else settings
    if not isinstance(resolved, dict):
        raise SchemaError("invalid_type")
    if set(resolved) - set(LAUNCH_SETTINGS):
        raise SchemaError("unknown_behavior")
    for name, caster in LAUNCH_SETTINGS.items():
        if type(resolved.get(name, False)) is not caster:
            raise SchemaError("invalid_type")

    material = {
        "version": SCHEMA_VERSION,
        "fixture_sha256": hashlib.sha256(fixture).hexdigest(),
        "settings": {name: resolved.get(name, False) for name in LAUNCH_SETTINGS},
    }
    return hashlib.sha256(_canonical(material)).hexdigest()
