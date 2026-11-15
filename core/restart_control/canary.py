"""Disposable launchd canary envelopes, scope validation and startup scan.

T016: frozen schemas and validation only. No service control, no spawn, no
credential resolution and no raw subprocess output — arming (T017), the
worker (T018) and startup orchestration (T019) arrive later. Every check
fails closed: an ambiguous, stale, foreign or unrecognized canary record
blocks mutation admission; it can never be silently upgraded, deleted or
reinterpreted.

Scope derivation is one-way: the label, directory and file names follow
from the arm nonce by fixed rules (plan section 4) and are never accepted
from a caller, an RPC field or an environment value. A canary scope that
collides with an existing job, a production label or an unexpected file
refuses rather than probing further.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time

from pathlib import Path

from .engine import Fault, _read_json, _sync_fd, _sync_dir, canonical_digest, private_dir

__all__ = ["ARM_KEYS", "EVIDENCE_KEYS", "CHECK_KEYS", "EVIDENCE_SCOPES",
           "DETAIL_KEYS", "STEP_ORDER", "STEP_APPLICABILITY",
           "canary_label", "canary_scope",
           "validate_arm", "validate_evidence", "validate_detail",
           "scan_canary_namespace"]

_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EPOCH = re.compile(r"[0-9a-f]{32}\Z")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_BOUND_S = 65536          # canonical JSON byte bound for one record
_BOUND_POPULATION = 4096  # directory entries scanned in one namespace walk

# Plan section 2: the arm envelope under canary/intent.json.
ARM_KEYS = {"version", "operator_uid", "nonce", "policy_digest",
            "controller_digest", "controller_plist_digest", "boot_identity",
            "issued_at", "expires_at", "issued_monotonic", "expires_monotonic"}

# Plan section 9: the evidence envelope under canary/evidence/<digest>.json.
EVIDENCE_KEYS = {"version", "scope", "nonce", "boot_identity", "operator_uid",
                 "controller_identity", "controller_epoch", "controller_digest",
                 "policy_digest", "controller_plist_digest", "controller_supervision",
                 "shutdown_contract", "canary_label", "canary_plist_digest",
                 "incarnations", "checks", "detail_digest", "issued_at", "issued_monotonic"}

CHECK_KEYS = {"loaded_definition", "bootstrap", "keepalive", "bootout",
               "cleanup", "shutdown_budget"}

# Plan section 8: only fixture scope is accepted offline; live classes
# arrive with the producer in later tasks. A fixture record can never be
# relabeled as production qualification by editing its scope string.
EVIDENCE_SCOPES = {"fixture", "isolated-launchd", "intended-controller"}

# Plan "Closed canary detail": the detail record under
# canary/detail/<detail_digest>.json.
DETAIL_KEYS = {"version", "nonce", "boot_identity", "controller_identity",
               "controller_epoch", "steps", "shutdown"}

STEP_ORDER = ("bootstrap", "first_loaded", "exit_once", "keepalive",
               "bootout", "cleanup")

# Per-step applicability frozen from the plan: identity/runs/loaded fields
# are explicit null where not applicable — never omitted — and exit_code is
# exactly 0 for bootstrap/bootout, null otherwise.
STEP_APPLICABILITY = {
    "bootstrap":     {"identity": None, "runs": None, "loaded": None, "exit_code": 0},
    "first_loaded":  {"identity": "first", "runs": 1, "loaded": True, "exit_code": None},
    "exit_once":     {"identity": "first", "runs": 1, "loaded": None, "exit_code": None},
    "keepalive":     {"identity": "second", "runs": 2, "loaded": True, "exit_code": None},
    "bootout":       {"identity": "second", "runs": 2, "loaded": None, "exit_code": 0},
    "cleanup":        {"identity": None, "runs": 0, "loaded": None, "exit_code": None},
}

STEP_KEYS = {"name", "started_monotonic", "completed_monotonic", "outcome",
             "identity", "runs", "loaded_exit_seconds",
             "loaded_environment_digest", "exit_code"}

SHUTDOWN_KEYS = {"identity", "requested_monotonic", "exit_observed_monotonic",
                  "cleanup_completed_monotonic", "deadline_seconds"}


def canary_label(nonce):
    """The one disposable label derived from an arm nonce (never caller-set)."""
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        raise ValueError("canary_nonce_invalid")
    return "local.restart-canary." + nonce


def canary_scope(state, nonce):
    """The one private scope directory derived from state and nonce."""
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        raise ValueError("canary_nonce_invalid")
    root = Path(state)/"canary"
    if not root.exists():
        private_dir(root)
    return private_dir(root/nonce)


def _plain_int(value):
    return type(value) is int


def _finite(value):
    return type(value) in (int, float) and value == value and value not in (
        float("inf"), float("-inf"))


def _identity(value):
    if (type(value) is not dict or set(value) != {"pid", "uid", "ppid", "pgid", "birth"}
            or any(not _plain_int(value[k]) for k in ("pid", "uid", "ppid", "pgid"))):
        raise ValueError("canary_identity_invalid")
    birth = value["birth"]
    if (type(birth) is not list or len(birth) != 2
            or any(not _plain_int(p) for p in birth)
            or birth[0] <= 0 or not 0 <= birth[1] < 1000000):
        raise ValueError("canary_identity_invalid")
    return value


def validate_arm(record, *, operator_uid):
    """Validate one arm envelope: closed keys, exact generation bindings.

    The operator UID is compared against the caller-supplied current uid.
    Freshness (dual-clock expiry) is checked separately by arm_is_fresh; a
    valid arm returns its nonce. Anything else refuses — an arm is never
    normalized, completed or upgraded in place.
    """
    if not isinstance(record, dict) or set(record) != ARM_KEYS:
        raise ValueError("canary_arm_invalid")
    if (record["version"] != 1 or not _plain_int(record["version"])
            or not _plain_int(record["operator_uid"])
            or record["operator_uid"] != operator_uid
            or not isinstance(record["nonce"], str) or not _NONCE.fullmatch(record["nonce"])
            or not _DIGEST.fullmatch(record["policy_digest"] or "")
            or not _DIGEST.fullmatch(record["controller_digest"] or "")
            or not _DIGEST.fullmatch(record["controller_plist_digest"] or "")
            or not _UUID.fullmatch(record["boot_identity"] or "")
            or not _finite(record["issued_at"]) or not _finite(record["expires_at"])
            or not _finite(record["issued_monotonic"]) or not _finite(record["expires_monotonic"])):
        raise ValueError("canary_arm_invalid")
    if not (record["issued_at"] < record["expires_at"]
            and 0 < record["expires_at"] - record["issued_at"] <= 300
            and record["issued_monotonic"] < record["expires_monotonic"]
            and 0 < record["expires_monotonic"] - record["issued_monotonic"] <= 300):
        raise ValueError("canary_arm_invalid")
    return record["nonce"]


def arm_is_fresh(record, wall, mono):
    """Dual-clock freshness check: valid only inside BOTH expiry windows."""
    return (record["issued_at"] <= wall < record["expires_at"]
            and record["issued_monotonic"] <= mono < record["expires_monotonic"])


# -- arm lifecycle (T017): private exclusive creation and one-time use ------

_ARM_LIFETIME = 300.0


def arm_bindings(policy_path):
    """The exact generation hashes an arm binds, computed without Runtime.

    Equals Runtime.binding_facts() for the same policy and controller
    sources: canonical policy digest, canonical digest of the closed
    controller source registry facts, and the generated controller plist
    bytes. Reads only public source files — no credentials, no spawn.
    """
    import plistlib

    from .engine import _read_json, canonical_digest
    from .runtime_identity import CONTROLLER_INPUTS, file_fact
    from .runtime import controller_job

    policy_path = Path(policy_path)
    policy = _read_json(policy_path)
    kit = Path(policy["kit_root"])
    controller = {name: file_fact(kit/name) for name in CONTROLLER_INPUTS}
    proxy = plistlib.loads(Path(policy["plist"]).read_bytes())
    python = proxy["EnvironmentVariables"]["LITELLM_PROXY_PYTHON"]
    job = controller_job(policy, policy_path, python)
    return {"policy_digest": canonical_digest(policy),
            "controller_digest": canonical_digest(controller),
            "controller_plist_digest": hashlib.sha256(
                plistlib.dumps(job, sort_keys=True)).hexdigest()}


def arm_canary(state, bindings):
    """Create one arm envelope; exclusive, private, never overwritten.

    ``bindings`` carries the exact generation hashes from the CURRENT
    policy/controller sources (policy_digest, controller_digest,
    controller_plist_digest) and the current boot identity is read from the
    bounded kernel primitive — never an ambient environment value. Refuses
    when any pending or consumed canary uncertainty exists: an existing
    intent.json, any consumed nonce scope, partial temporaries or an
    unrecognized entry. Emits no service control and resolves no
    credentials.
    """
    from .runtime_identity import kernel_boot_identity
    keys = {"policy_digest", "controller_digest", "controller_plist_digest"}
    if (not isinstance(bindings, dict) or set(bindings) != keys
            or any(not _DIGEST.fullmatch(bindings[k] or "") for k in keys)):
        raise Fault("invalid_request")
    # Refuse any pending/consumed canary uncertainty BEFORE writing.
    scan_canary_namespace(state)
    root = Path(state)/"canary"
    if not root.exists():
        private_dir(root)
    nonce = secrets.token_hex(16)
    # Capture both clocks ONCE: the expiry windows must be exactly the
    # arm lifetime, immune to clock reads straddling a boundary.
    now, mono = time.time(), time.monotonic()
    record = {"version": 1, "operator_uid": os.getuid(), "nonce": nonce,
              "policy_digest": bindings["policy_digest"],
              "controller_digest": bindings["controller_digest"],
              "controller_plist_digest": bindings["controller_plist_digest"],
              "boot_identity": kernel_boot_identity(),
              "issued_at": now,
              "expires_at": now + _ARM_LIFETIME,
              "issued_monotonic": mono,
              "expires_monotonic": mono + _ARM_LIFETIME}
    validate_arm(record, operator_uid=os.getuid())
    # Exclusive creation: O_EXCL, private mode, no overwrite, synced.
    destination = root/"intent.json"
    blob = json.dumps(record, sort_keys=True, allow_nan=False).encode()
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        raise Fault("state_io") from None
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(blob)
            stream.flush()
            _sync_fd(stream.fileno())
    finally:
        _sync_dir(root)
    return record


def consume_arm(state, *, operator_uid):
    """Atomically consume the one valid arm into its private nonce scope.

    A synced rename moves the arm into the nonce-specific private canary
    scope where the startup orchestration (T019) carries it through. The
    rename is synced on both the destination directory and the source
    root; the consumed arm is never deleted, so an uncertain outcome is
    always reconstructable. Refuses when the arm is absent, invalid,
    expired, replayed, or its scope already exists.
    """
    root = Path(state)/"canary"
    if not root.exists():
        raise Fault("recovery_invalid")
    arm_path = root/"intent.json"
    try:
        record = _read_json(arm_path)
    except FileNotFoundError:
        raise Fault("recovery_invalid") from None
    validate_arm(record, operator_uid=operator_uid)
    if not arm_is_fresh(record, time.time(), time.monotonic()):
        raise Fault("recovery_invalid")
    scope = canary_scope(state, record["nonce"])
    if any(scope.iterdir()):
        # A scope with content for this nonce means replay or an earlier
        # partially-consumed attempt: refuse, never merge or overwrite.
        raise Fault("recovery_invalid")
    try:
        os.replace(arm_path, scope/"intent.json")
    except OSError:
        raise Fault("state_io") from None
    _sync_dir(scope)
    _sync_dir(root)
    return record


# -- disposable worker mode (T018) -------------------------------------------

_WORKER_POLL = 0.25


def run_canary_worker(state, nonce, *, stdout=None):
    """Disposable canary worker: no Runtime, no HTTP, no credentials.

    Validates its fixed private scope, records its own bounded identity
    and start time, cooperatively obeys the one-time private exit
    marker, and handles SIGTERM/SIGINT through the shared shutdown
    latch with the fixed 50-second budget — never a signal escalation.
    At most two incarnations exist per scope (launchd KeepAlive's second
    distinct birth): the first consumes the one-time exit marker and
    exits cooperatively; the second stays until SIGTERM and records the
    durable first-latch receipt. Emits only the bounded status JSON on
    stdout; no raw subprocess output.
    """
    import signal

    from .engine import atomic_json
    from .process_identity import process_identity
    from .shutdown import Shutdown, ShutdownRequested

    if stdout is None:
        stdout = sys.stdout
    scope = canary_scope(state, nonce)
    first = scope/"incarnation-1.json"
    second = scope/"incarnation-2.json"
    if second.exists():
        # At most two incarnations per scope: a third is uncertain
        # state, never silently overwritten.
        raise Fault("recovery_invalid")
    # The worker runs only inside a genuinely consumed scope: the arm
    # must have been consumed into this scope (intent.json present) and
    # the derived canary job generated by the lane (its plist present),
    # so an arbitrary worker invocation cannot populate an empty scope
    # (plan §5: validate its generated private scope before recording).
    if not (scope/"intent.json").exists():
        raise Fault("recovery_invalid")
    if not any(path.name.startswith("local.restart-canary.")
               and path.name.endswith(".plist")
               for path in scope.iterdir()):
        raise Fault("recovery_invalid")
    worker = first if not first.exists() else second
    identity = process_identity(os.getpid())
    if identity["uid"] != os.getuid():
        raise Fault("recovery_invalid")
    shutdown = Shutdown()
    # First-SIGTERM latch receipt (plan: shutdown detail): the private
    # monotonic observation at the first request, recorded durably in
    # the scope by the signal handler itself — the deadline is derived,
    # the receipt is observed. One-time exclusive creation.
    latched = {}
    def _latch(*_):
        if "requested_monotonic" not in latched:
            latched["requested_monotonic"] = time.monotonic()
            # Request shutdown FIRST: the handler only latches the state
            # and never blocks — any durable receipt write happens after
            # the main loop observes the request (plan: the handler is
            # restricted to requesting shutdown; nested signals during
            # file I/O would reenter the handler and a stalled sync
            # would eat the 50-second budget before the latch is set).
            shutdown.request()
            receipt = scope/"latch.json"
            if not receipt.exists():
                try:
                    atomic_json(receipt, {"version": 1, "nonce": nonce,
                                           "requested_monotonic":
                                               latched["requested_monotonic"]})
                except Exception:
                    pass
        else:
            shutdown.request()
    # Register the latch BEFORE recording the incarnation receipt: a
    # SIGTERM arriving after the receipt exists must meet the handler,
    # never the default disposition (a pre-handler death would lose the
    # durable latch receipt the lane proves worker disappearance with).
    signal.signal(signal.SIGTERM, _latch)
    signal.signal(signal.SIGINT, _latch)
    atomic_json(worker, {"version": 1, "nonce": nonce,
                         "identity": {k: identity[k] for k in
                                      ("pid", "uid", "ppid", "pgid", "birth")},
                         "started_monotonic": time.monotonic()})
    marker = scope/"exit.marker"
    try:
        while True:
            shutdown.checkpoint()
            if marker.exists():
                outcome = "exit_marker"
                break
            if shutdown.wait(_WORKER_POLL, cleanup=False) is False:
                continue
            outcome = "shutdown_requested"
            break
    except ShutdownRequested:
        outcome = "shutdown_requested"
    if outcome == "exit_marker":
        # One-time consumption: the marker is unlinked so KeepAlive's
        # second incarnation stays alive until the bounded bootout.
        try:
            marker.unlink()
            _sync_dir(scope)
        except OSError:
            pass
    record = {"version": 1, "nonce": nonce,
              "outcome": outcome,
              "requested": shutdown.requested,
              "deadline": shutdown.deadline,
              "completed_monotonic": time.monotonic(),
              "requested_monotonic": latched.get("requested_monotonic")}
    print(json.dumps(record, sort_keys=True, allow_nan=False), file=stdout)
    return record


def validate_evidence(record, *, operator_uid, expected=None):
    """Validate one evidence envelope and derive its checks independently.
    ``expected`` carries the cross-bound bindings (identity, epoch, source/
    policy/plist digests, supervision, shutdown contract) the envelope must
    equal. The producer booleans are never trusted — every check is derived
    from the values present. ``scope`` must be a known evidence class; a
    fixture record stays fixture scope forever.
    """
    if not isinstance(record, dict) or set(record) != EVIDENCE_KEYS:
        raise ValueError("canary_evidence_invalid")
    if (record["version"] != 1 or not _plain_int(record["version"])
            or record["scope"] not in EVIDENCE_SCOPES
            or not _plain_int(record["operator_uid"])
            or record["operator_uid"] != operator_uid
            or not _NONCE.fullmatch(record["nonce"] or "")
            or not _UUID.fullmatch(record["boot_identity"] or "")):
        raise ValueError("canary_evidence_invalid")
    _identity(record["controller_identity"])
    if not _EPOCH.fullmatch(record["controller_epoch"] or ""):
        raise ValueError("canary_evidence_invalid")
    for key in ("controller_digest", "policy_digest",
                "controller_plist_digest", "canary_plist_digest",
                "detail_digest"):
        if not _DIGEST.fullmatch(record[key] or ""):
            raise ValueError("canary_evidence_invalid")
    # The canary label is derived, never stated: it must equal the label
    # derived from the envelope's own nonce.
    if record["canary_label"] != canary_label(record["nonce"]):
        raise ValueError("canary_evidence_invalid")
    if not _LABEL.fullmatch(record["canary_label"] or ""):
        raise ValueError("canary_evidence_invalid")
    # Exactly two ordered incarnations with distinct births and run counts
    # 1 then 2 (KeepAlive's second distinct birth). Identity is strict;
    # `runs` is a separate observed key on the incarnation.
    born = record["incarnations"]
    if not isinstance(born, list) or len(born) != 2:
        raise ValueError("canary_evidence_invalid")
    for entry in born:
        if not isinstance(entry, dict) or set(entry) != {"pid", "uid", "ppid",
                "pgid", "birth", "runs"}:
            raise ValueError("canary_evidence_invalid")
        _identity({k: entry[k] for k in ("pid", "uid", "ppid", "pgid", "birth")})
    if (born[0]["birth"] == born[1]["birth"]
            or born[0]["runs"] != 1 or born[1]["runs"] != 2):
        raise ValueError("canary_evidence_invalid")
    if not isinstance(record["checks"], dict) or set(record["checks"]) != CHECK_KEYS:
        raise ValueError("canary_evidence_invalid")
    # The producer booleans must all be TRUE, not merely present: a false
    # or missing check is a failed attempt, never success evidence.
    if (any(type(value) is not bool for value in record["checks"].values())
            or any(value is not True for value in record["checks"].values())):
        raise ValueError("canary_evidence_invalid")
    if expected is not None:
        for key in ("controller_identity", "controller_epoch",
                    "controller_digest", "policy_digest",
                    "controller_plist_digest", "boot_identity", "nonce"):
            if record[key] != expected[key]:
                raise ValueError("canary_evidence_binding_invalid")
        if (canonical_digest(record["controller_supervision"]) != canonical_digest(expected["controller_supervision"])
                or canonical_digest(record["shutdown_contract"]) != canonical_digest(expected["shutdown_contract"])):
            raise ValueError("canary_evidence_binding_invalid")
    return record


def validate_detail(record, *, expected=None):
    """Validate one closed detail record and derive its step outcomes.

    Success is derived from the ordered per-step values (identity, runs,
    loaded timeout/environment digest, exit codes) and the observed
    shutdown timing — never from the outcome word alone.
    """
    if not isinstance(record, dict) or set(record) != DETAIL_KEYS:
        raise ValueError("canary_detail_invalid")
    if (record["version"] != 1 or not _plain_int(record["version"])
            or not _NONCE.fullmatch(record["nonce"] or "")
            or not _UUID.fullmatch(record["boot_identity"] or "")):
        raise ValueError("canary_detail_invalid")
    _identity(record["controller_identity"])
    if not _EPOCH.fullmatch(record["controller_epoch"] or ""):
        raise ValueError("canary_detail_invalid")
    if expected is not None:
        for key in ("nonce", "boot_identity", "controller_identity",
                    "controller_epoch"):
            if record[key] != expected[key]:
                raise ValueError("canary_detail_binding_invalid")
    steps = record["steps"]
    if (not isinstance(steps, list) or len(steps) != len(STEP_ORDER)
            or not all(isinstance(step, dict) and set(step) == STEP_KEYS
                       for step in steps)
            or [step["name"] for step in steps] != list(STEP_ORDER)):
        raise ValueError("canary_detail_invalid")
    previous = None
    incarnation_identity = {}
    for index, (step, rule) in enumerate(zip(steps, (STEP_APPLICABILITY[name] for name in STEP_ORDER))):
        if (not _finite(step["started_monotonic"]) or not _finite(step["completed_monotonic"])
                or not (step["started_monotonic"] <= step["completed_monotonic"])
                or (previous is not None and step["started_monotonic"] < previous)):
            raise ValueError("canary_detail_invalid")
        previous = step["completed_monotonic"]
        # Success is derived: EVERY step must have passed; a failed or
        # non-passed outcome is a blocked attempt, never success detail.
        if step["outcome"] != "passed":
            raise ValueError("canary_detail_invalid")
        if step["exit_code"] != rule["exit_code"]:
            raise ValueError("canary_detail_invalid")
        if rule["identity"] is None:
            if step["identity"] is not None or step["runs"] != rule["runs"]:
                raise ValueError("canary_detail_invalid")
        else:
            _identity(step["identity"])
            if step["runs"] != rule["runs"]:
                raise ValueError("canary_detail_invalid")
            # Cross-bind the two worker incarnations: every step citing
            # "first"/"second" carries the SAME identity as every other
            # step of that incarnation, so an unrelated shutdown identity
            # or mixed-worker detail never validates (plan: the detail's
            # steps are one attempt's coherent observations).
            key = "first" if rule["identity"] == "first" else "second"
            if key in incarnation_identity and canonical_digest(
                    incarnation_identity[key]) != canonical_digest(step["identity"]):
                raise ValueError("canary_detail_invalid")
            incarnation_identity[key] = step["identity"]
        if rule["loaded"]:
            if step["loaded_exit_seconds"] != 60 or not _DIGEST.fullmatch(
                    step["loaded_environment_digest"] or ""):
                raise ValueError("canary_detail_invalid")
        else:
            if (step["loaded_exit_seconds"] is not None
                    or step["loaded_environment_digest"] is not None):
                raise ValueError("canary_detail_invalid")
    shutdown = record["shutdown"]
    if not isinstance(shutdown, dict) or set(shutdown) != SHUTDOWN_KEYS:
        raise ValueError("canary_detail_invalid")
    _identity(shutdown["identity"])
    # The shutdown block observes the SECOND incarnation: its identity is
    # exactly the keepalive/bootout steps' second-incarnation identity.
    if canonical_digest(shutdown["identity"]) != canonical_digest(
            incarnation_identity.get("second", shutdown["identity"])):
        raise ValueError("canary_detail_invalid")
    if (shutdown["deadline_seconds"] != 50 or not _plain_int(shutdown["deadline_seconds"])
            or not _finite(shutdown["requested_monotonic"])
            or not _finite(shutdown["exit_observed_monotonic"])
            or not _finite(shutdown["cleanup_completed_monotonic"])
            or not (shutdown["requested_monotonic"] <= shutdown["exit_observed_monotonic"]
                    <= shutdown["cleanup_completed_monotonic"])
            or shutdown["cleanup_completed_monotonic"] > shutdown["requested_monotonic"] + 50):
        raise ValueError("canary_detail_invalid")
    return record


def scan_canary_namespace(state, *, wall=None, mono=None, boot=None):
    """Bounded no-arm startup recovery scan of the fixed canary namespace.

    Walks the COMPLETE fixed namespace under ``canary/``: the arm file,
    every consumed nonce scope, evidence and detail file — including
    partial atomic-rename temporaries. Every consumed scope is cross-checked
    against its terminal evidence/detail; an atomic rename alone is never
    proof of completion. Returns the validated state summary; any missing
    attempt, unrecognized file, partial write or unresolved child raises —
    the caller must block startup mutation, never clean up silently.

    ``wall``/``mono`` default to the real clocks; tests inject frozen ones.
    """
    import time as _time
    wall = _time.time() if wall is None else wall
    mono = _time.monotonic() if mono is None else mono
    root = Path(state)/"canary"
    if not root.exists():
        return {"armed": None, "scopes": {}}
    root = private_dir(root)
    recognized = {"intent.json"}
    consumed = {}
    entries = sorted(root.iterdir())
    if len(entries) > _BOUND_POPULATION:
        raise Fault("recovery_invalid")
    for entry in entries:
        name = entry.name
        if entry.is_symlink():
            raise Fault("recovery_invalid")
        if name.startswith(".intent.json."):
            # A partial atomic-rename temporary from a crashed write is
            # unresolved intent uncertainty: it blocks, never deletes.
            raise Fault("recovery_invalid")
        if name == "intent.json":
            recognized.add(name)
            continue
        if _NONCE.fullmatch(name or ""):
            scope = private_dir(entry)
            recognized.add(name)
            consumed[name] = _scan_scope(scope, root)
            continue
        # evidence/ and detail/ directories are part of the fixed namespace.
        if name in ("evidence", "detail"):
            private_dir(entry)
            recognized.add(name)
            continue
        raise Fault("recovery_invalid")
    arm = None
    if (root/"intent.json").exists():
        try:
            arm = _read_json(root/"intent.json")
            validate_arm(arm, operator_uid=os.getuid())
            # A live arm must still be fresh: dual-clock expiry, both
            # windows, AND the same kernel boot — an arm from a previous
            # boot is stale even inside its wall-clock window.
            from .runtime_identity import kernel_boot_identity
            if (not arm_is_fresh(arm, wall, mono)
                    or arm["boot_identity"] != (boot if boot is not None
                                                else kernel_boot_identity())):
                raise ValueError("canary_arm_stale")
        except Fault:
            raise
        except Exception:
            raise Fault("recovery_invalid") from None
    # The fixed evidence/detail directories are keyed by content digest; a
    # consumed nonce scope must be cross-checked against its terminal
    # evidence/detail by nonce binding, never by trusting a rename.
    evidence_by_nonce = {}
    if (root/"evidence").exists():
        for digest in _bounded_names(root/"evidence"):
            path = root/"evidence"/(digest+".json")
            try:
                # The filename digest IS the file-bytes hash: recompute it
                # from the actual bytes, never trust the name alone
                # (plan: digest of canonical bytes, revalidated on scan).
                from .runtime_identity import file_fact
                if file_fact(path)["sha256"] != digest:
                    raise ValueError("canary_evidence_invalid")
                record = _read_json(path)
                validate_evidence(record, operator_uid=os.getuid())
            except Fault:
                raise
            except Exception:
                raise Fault("recovery_invalid") from None
            if record["nonce"] not in evidence_by_nonce:
                evidence_by_nonce[record["nonce"]] = record
            else:
                # Two evidence records claiming one nonce is ambiguous.
                raise Fault("recovery_invalid")
    details = {}
    if (root/"detail").exists():
        for digest in _bounded_names(root/"detail"):
            path = root/"detail"/(digest+".json")
            try:
                if file_fact(path)["sha256"] != digest:
                    raise ValueError("canary_detail_invalid")
                details[digest] = validate_detail(_read_json(path))
            except Fault:
                raise
            except Exception:
                raise Fault("recovery_invalid") from None
    for nonce, scope in consumed.items():
        if arm is not None and arm["nonce"] == nonce:
            # The pending arm's scope exists already: consumed uncertainty.
            raise Fault("recovery_invalid")
        scope_arm = scope.get("arm")
        if scope_arm is None:
            raise Fault("recovery_invalid")
        try:
            validate_arm(scope_arm, operator_uid=os.getuid())
        except Fault:
            raise
        except Exception:
            raise Fault("recovery_invalid") from None
        if scope_arm["nonce"] != nonce:
            raise Fault("recovery_invalid")
        if nonce not in evidence_by_nonce:
            # A consumed scope without terminal evidence is an incomplete
            # attempt: block mutation, keep the records.
            raise Fault("recovery_invalid")
        evidence = evidence_by_nonce[nonce]
        # The scope's terminal evidence must bind the consumed arm's own
        # boot and generation: evidence produced against another boot or
        # changed policy/controller/plist sources never settles this
        # scope (clock/boot and source/plist mismatch refuse).
        for key in ("boot_identity", "policy_digest",
                    "controller_digest", "controller_plist_digest"):
            if evidence[key] != scope_arm[key]:
                raise Fault("recovery_invalid")
        # The referenced detail record must exist under its own digest and
        # bind the evidence: an omitted detail file, or a digest naming
        # another attempt's detail, never completes the scope.
        detail = details.get(evidence["detail_digest"])
        if detail is None:
            raise Fault("recovery_invalid")
        for key in ("nonce", "boot_identity", "controller_identity",
                    "controller_epoch"):
            if detail[key] != evidence[key]:
                raise Fault("recovery_invalid")
    return {"armed": arm, "scopes": consumed}


def _bounded_names(directory):
    """Bounded sorted file names (without suffix) inside a fixed directory."""
    entries = sorted(directory.iterdir())
    if len(entries) > _BOUND_POPULATION:
        raise Fault("recovery_invalid")
    names = []
    for entry in entries:
        if (entry.is_symlink() or not _DIGEST.fullmatch(entry.stem or "")
                or entry.suffix != ".json" or not entry.is_file()):
            raise Fault("recovery_invalid")
        names.append(entry.stem)
    return names


def _scan_scope(scope, root):
    """Cross-check one consumed nonce scope against its terminal records."""
    files = sorted(scope.iterdir())
    if len(files) > _BOUND_POPULATION:
        raise Fault("recovery_invalid")
    summary = {"files": [f.name for f in files], "evidence": None, "detail": None}
    for entry in files:
        name = entry.name
        if entry.is_symlink():
            raise Fault("recovery_invalid")
        if name.startswith("."):
            raise Fault("recovery_invalid")
        info = entry.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise Fault("recovery_invalid")
        if name == "intent.json":
            # The consumed arm itself, moved here by the one-time
            # consumption; validated with the scope by nonce binding.
            summary["arm"] = _read_json(entry)
            continue
        if name == "incarnation-1.json" or name == "incarnation-2.json":
            continue
        if name == "latch.json":
            continue
        if name == "exit.marker":
            summary["exit_marker"] = True
            continue
        if name == "diagnostic.json":
            # A blocked attempt's durable refusal diagnostic. It is not
            # terminal evidence: its presence means the scope is an
            # incomplete attempt, and the missing-evidence check below
            # refuses the scope (block, never clean up).
            summary["diagnostic"] = _read_json(entry)
            continue
        if name.startswith("local.restart-canary.") and name.endswith(".plist"):
            # The derived canary job's own generated plist, written by
            # the lane (T019) inside its scope. Its digest is bound in
            # the terminal evidence envelope (canary_plist_digest).
            summary["canary_plist"] = True
            continue
        raise Fault("recovery_invalid")
    return summary