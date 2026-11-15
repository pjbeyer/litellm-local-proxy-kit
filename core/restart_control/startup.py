"""Startup canary orchestration (T019): exact-context launchd canary run.

Runs only inside normal ``serve`` after the shared Shutdown object,
Runtime/Controller construction and identity/supervision verification,
strictly before RPC admission. The lane is bounded by the consumed
arm's remaining lifetime; every launchctl call is fixed-argv, tracked
through the platform-canary synthetic operation and capped (query 10s,
bootstrap 30s, bootout 75s, KeepAlive observation 75s). No arm means
zero canary service-control activity. Any refusal, ambiguity, timeout
or latch leaves a blocked journal outcome plus a durable status
diagnostic — never success evidence, never a silent cleanup, never a
retry. Offline tests run against an explicit fake platform adapter with
real worker children; responses there are fake by construction.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import plistlib
import time
from pathlib import Path

from .canary import (canary_label, canary_scope, consume_arm,
                     scan_canary_namespace, validate_detail,
                     validate_evidence)
from .engine import (Fault, _read_json, _sync_fd, _sync_dir, canonical_bytes,
                     canonical_digest, private_dir)
from .process_identity import process_identity
from .runtime_identity import kernel_process_absent
from .shutdown import SHUTDOWN_CONTRACT, ShutdownRequested

# Per-step caps (plan §7). Caps, not a summed promise: every wait also
# honors the arm's remaining lifetime and the shared shutdown latch.
QUERY_CAP = 10.0
BOOTSTRAP_CAP = 30.0
BOOTOUT_CAP = 75.0
KEEPALIVE_CAP = 75.0
OBSERVE_POLL = 0.25


def _write_blob(path, blob):
    """Exclusive, private, synced creation of one file; never overwrite.

    The parent directory is verified private and symlink-free before
    the exclusive open, so the write cannot be redirected outside the
    state tree by a symlinked parent (the O_EXCL check on the final
    component alone does not protect the containing directory).
    """
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        _sync_dir(path.parent)


def startup_scan(state, *, wall=None, mono=None):
    """Bounded startup admission scan of the fixed canary namespace."""
    return scan_canary_namespace(state, wall=wall, mono=mono)


class _Lane:
    """One orchestrated canary attempt over a consumed arm.

    All launchctl work runs through the runtime's tracked helpers so
    every control child is journaled, bounded and owned. The synthetic
    platform-canary operation is durably pending before the first live
    call, running during control, and terminal only after proven
    cleanup. Step identities are kernel-observed and cross-checked
    against the worker's own receipts — never caller-stated.
    """

    def __init__(self, runtime, controller, shutdown, arm, scope):
        self.runtime = runtime
        self.controller = controller
        self.shutdown = shutdown
        self.arm = arm
        self.scope = scope
        self.nonce = arm["nonce"]
        self.label = canary_label(self.nonce)
        self.steps = []
        self.ticket = None
        self._plist_path = None
        self._plist_digest = None
        self._first_identity = None

    # -- journal lifecycle ---------------------------------------------------

    def _binding(self):
        facts = self.runtime.binding_facts()
        return {"request_id": self.nonce, "kind": "platform-canary",
                "controller_epoch": self.controller.epoch,
                "policy_digest": facts["policy_digest"],
                "controller_digest": facts["controller_digest"],
                "controller_plist_digest": facts["controller_plist_digest"],
                "asset_digest": None, "runtime_digest": None}

    def _journal(self, name, *args, **kwargs):
        from .recovery import RecoveryError
        try:
            return getattr(self.controller.recovery, name)(*args, **kwargs)
        except RecoveryError:
            raise Fault("recovery_invalid") from None
        except Fault:
            raise
        except Exception:
            raise Fault("recovery_invalid") from None

    def _remaining(self):
        """Arm lifetime remaining in wall seconds; never negative."""
        return max(0.0, self.arm["expires_at"] - time.time())

    # -- derived canary job ----------------------------------------------------

    def canary_job(self):
        """The exact derived disposable canary job (never caller-set)."""
        from .runtime import controller_job
        job = controller_job(self.runtime.policy, self.runtime.policy_path,
                             self.runtime.python)
        entrypoint = (Path(self.runtime.policy["controller_root"])
                      / "restart_controller.py")
        derived = dict(job)
        derived.update({
            "Label": self.label,
            "ProgramArguments": [self.runtime.python, "-I", str(entrypoint),
                                 "--policy", str(self.runtime.policy_path),
                                 "canary-worker", self.nonce],
            "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 30,
            "ExitTimeOut": SHUTDOWN_CONTRACT["supervisor_exit_seconds"]})
        return derived

    def _write_plist(self):
        import hashlib
        blob = plistlib.dumps(self.canary_job(), sort_keys=True)
        path = self.scope / (self.label + ".plist")
        if path.exists():
            raise Fault("recovery_invalid")
        _write_blob(path, blob)
        self._plist_path = path
        self._plist_digest = hashlib.sha256(blob).hexdigest()

    # -- tracked helpers ----------------------------------------------------------

    def _launchctl(self, purpose, argv, cap):
        """One fixed-argv launchctl control through the tracked runner."""
        self.shutdown.checkpoint()
        remaining = self._remaining()
        if remaining <= 0:
            raise Fault("canary_arm_expired")
        started = time.monotonic()
        self.runtime._capture(argv, timeout=min(cap, remaining),
                              control=True, purpose=purpose)
        return started, time.monotonic()

    def _job_state(self, job):
        """Observe the canary job through the bounded launchctl print.

        A nonzero print status or an observation exception is a REFUSAL,
        never proof of absence: permission failures and indeterminate
        observations must fail the lane rather than pass its cleanup
        (plan §4/§6). Only a zero-status parsed payload may prove a
        present job; absence is proven solely by the caller's explicit
        ``absent_ok`` check against the not-found signature.
        """
        self.shutdown.checkpoint()
        remaining = self._remaining()
        if remaining <= 0:
            raise Fault("canary_arm_expired")
        # The query cap is also bounded by the arm's remaining lifetime:
        # the lane never spends wall time past the arm envelope.
        state = self.runtime._job_state(self.label, expected=job,
                                        expected_path=self._plist_path,
                                        timeout=min(QUERY_CAP, remaining))
        if state is None:
            # The runtime's not-found signature (launchctl exit 113)
            # arrives here as None; the lane's contract raises the
            # explicit absence code so _cleanup can prove it.
            raise Fault("canary_job_absent")
        return state

    def _live_identity(self, pid, birth):
        """Kernel-observed worker identity: uid, launchd parentage, birth."""
        live = process_identity(pid)
        if (live["uid"] != os.getuid() or live["ppid"] != 1
                or live["birth"] != birth):
            raise Fault("canary_identity_invalid")
        return live

    def _env_digest(self, job):
        return canonical_digest(job["EnvironmentVariables"])

    def _read_receipt(self, name):
        try:
            return _read_json(self.scope / name)
        except FileNotFoundError:
            return None
        except Exception:
            raise Fault("recovery_invalid") from None

    # -- observation steps --------------------------------------------------------

    def _first_loaded(self):
        """Step: first incarnation loaded with exact parity, runs == 1."""
        started = time.monotonic()
        deadline = started + min(QUERY_CAP, self._remaining())
        job = self.canary_job()
        while True:
            self.shutdown.checkpoint()
            state = self._job_state(job)
            if (state is not None and state.get("parity")
                    and state.get("runs") == 1 and state.get("identity")):
                observed = state["identity"]
                receipt = self._read_receipt("incarnation-1.json")
                if (receipt is not None
                        and receipt.get("identity", {}).get("pid")
                        == observed["pid"]):
                    try:
                        live = self._live_identity(observed["pid"],
                                                   observed["birth"])
                    except Exception:
                        live = None
                    if live is not None:
                        self._first_identity = live
                        return {"name": "first_loaded",
                                "started_monotonic": started,
                                "completed_monotonic": time.monotonic(),
                                "outcome": "passed", "identity": live,
                                "runs": 1,
                                "loaded_exit_seconds":
                                    SHUTDOWN_CONTRACT[
                                        "supervisor_exit_seconds"],
                                "loaded_environment_digest":
                                    self._env_digest(job),
                                "exit_code": None}
            if time.monotonic() >= deadline:
                raise Fault("canary_first_load_failed")
            time.sleep(OBSERVE_POLL)

    def _exit_once(self, first_identity):
        """Step: request the worker's cooperative exit and observe it."""
        started = time.monotonic()
        marker = self.scope / "exit.marker"
        if marker.exists():
            raise Fault("recovery_invalid")
        _write_blob(marker, b"")
        deadline = started + min(QUERY_CAP, self._remaining())
        while True:
            self.shutdown.checkpoint()
            # Death is proven ONLY by the kernel's ESRCH absence signal;
            # an identity read failure is an observation failure that
            # keeps waiting (and eventually times out), never death.
            if kernel_process_absent(first_identity["pid"]):
                # The first incarnation exited; the worker consumed the
                # one-time marker itself (cooperative, exit 0).
                return {"name": "exit_once",
                        "started_monotonic": started,
                        "completed_monotonic": time.monotonic(),
                        "outcome": "passed",
                        "identity": first_identity, "runs": 1,
                        "loaded_exit_seconds": None,
                        "loaded_environment_digest": None,
                        "exit_code": None}
            if time.monotonic() >= deadline:
                raise Fault("canary_exit_failed")
            time.sleep(OBSERVE_POLL)

    def _keepalive(self):
        """Step: KeepAlive's second distinct birth, runs == 2, parity kept."""
        started = time.monotonic()
        deadline = started + min(KEEPALIVE_CAP, self._remaining())
        job = self.canary_job()
        first_birth = (self._first_identity or {}).get("birth")
        while True:
            self.shutdown.checkpoint()
            state = self._job_state(job)
            if (state is not None and state.get("parity")
                    and state.get("runs") == 2 and state.get("identity")):
                observed = state["identity"]
                receipt = self._read_receipt("incarnation-2.json")
                if (receipt is not None
                        and receipt.get("identity", {}).get("pid")
                        == observed["pid"]):
                    try:
                        live = self._live_identity(observed["pid"],
                                                   observed["birth"])
                    except Exception:
                        live = None
                    if (live is not None and live["birth"] != first_birth):
                        return {"name": "keepalive",
                                "started_monotonic": started,
                                "completed_monotonic": time.monotonic(),
                                "outcome": "passed", "identity": live,
                                "runs": 2,
                                "loaded_exit_seconds":
                                    SHUTDOWN_CONTRACT[
                                        "supervisor_exit_seconds"],
                                "loaded_environment_digest":
                                    self._env_digest(job),
                                "exit_code": None}
            if time.monotonic() >= deadline:
                raise Fault("canary_keepalive_failed")
            time.sleep(OBSERVE_POLL)

    def _bootout(self, second_identity):
        """Step: bounded bootout of the canary job (second identity bound)."""
        started, completed = self._launchctl(
            "canary-bootout",
            ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/" + self.label],
            BOOTOUT_CAP)
        return {"name": "bootout", "started_monotonic": started,
                "completed_monotonic": completed, "outcome": "passed",
                "identity": second_identity, "runs": 2,
                "loaded_exit_seconds": None,
                "loaded_environment_digest": None, "exit_code": 0}

    def _shutdown_detail(self, second_identity):
        """Observed shutdown timing from the worker's durable receipt."""
        receipt = self._read_receipt("latch.json")
        if receipt is None or "requested_monotonic" not in receipt:
            raise Fault("canary_shutdown_receipt_missing")
        # The second incarnation must be genuinely gone, not inferred
        # from bootout returning zero (plan: shutdown detail).
        deadline = time.monotonic() + min(QUERY_CAP, self._remaining())
        while True:
            self.shutdown.checkpoint()
            # Genuinely gone, proven only by the kernel's ESRCH absence
            # signal — never inferred from bootout returning zero or an
            # identity read failure (plan: shutdown detail).
            if kernel_process_absent(second_identity["pid"]):
                return {"identity": second_identity,
                        "requested_monotonic": receipt["requested_monotonic"],
                        "exit_observed_monotonic": time.monotonic(),
                        "cleanup_completed_monotonic": time.monotonic(),
                        "deadline_seconds":
                            SHUTDOWN_CONTRACT["shutdown_deadline_seconds"]}
            if time.monotonic() >= deadline:
                raise Fault("canary_shutdown_unresolved")
            time.sleep(OBSERVE_POLL)

    def _cleanup(self):
        """Step: prove the canary job absent after bootout.

        Absence is proven ONLY by the documented not-found observation
        (canary_job_absent from _job_state); an indeterminate observation
        is a refusal that fails cleanup, never a pass. The completion
        time is recorded only after the successful observation.
        """
        started = time.monotonic()
        deadline = started + min(QUERY_CAP, self._remaining())
        job = self.canary_job()
        while True:
            self.shutdown.checkpoint()
            try:
                self._job_state(job)
            except Fault as error:
                if error.code == "canary_job_absent":
                    return {"name": "cleanup",
                            "started_monotonic": started,
                            "completed_monotonic": time.monotonic(),
                            "outcome": "passed", "identity": None, "runs": 0,
                            "loaded_exit_seconds": None,
                            "loaded_environment_digest": None,
                            "exit_code": None}
                raise
            if time.monotonic() >= deadline:
                raise Fault("canary_cleanup_failed")
            time.sleep(OBSERVE_POLL)

    # -- evidence ---------------------------------------------------------------

    def _controller_identity(self):
        own = process_identity(os.getpid())
        return {k: own[k] for k in ("pid", "uid", "ppid", "pgid", "birth")}

    def _detail(self, shutdown_detail):
        detail = {"version": 1, "nonce": self.nonce,
                  "boot_identity": self.arm["boot_identity"],
                  "controller_identity": self._controller_identity(),
                  "controller_epoch": self.controller.epoch,
                  "steps": self.steps,
                  "shutdown": shutdown_detail}
        validate_detail(detail)
        # The loaded observations' environment digests must equal the
        # derived canary job's expected environment — never an ambient
        # or unrelated environment the worker happened to inherit.
        expected_digest = self._env_digest(self.canary_job())
        for step in self.steps:
            if step["loaded_environment_digest"] is not None and \
                    step["loaded_environment_digest"] != expected_digest:
                raise Fault("canary_identity_invalid")
        # The file bytes ARE the canonical serialization: the filename
        # digest and the qualification/scan hash checks both hash these
        # exact bytes (plan: detail at canary/detail/<detail_digest>.json,
        # digest of canonical bytes).
        blob = canonical_bytes(detail)
        detail_digest = hashlib.sha256(blob).hexdigest()
        detail_path = (Path(self.runtime.state) / "canary" / "detail"
                       / (detail_digest + ".json"))
        detail_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_blob(detail_path, blob)
        return detail, detail_digest

    def _evidence(self, first, second, detail_digest):
        from .runtime import controller_supervision
        facts = self.runtime.binding_facts()
        evidence = {"version": 1, "scope": "intended-controller",
                    "nonce": self.nonce,
                    "boot_identity": self.arm["boot_identity"],
                    "operator_uid": os.getuid(),
                    "controller_identity": self._controller_identity(),
                    "controller_epoch": self.controller.epoch,
                    "controller_digest": facts["controller_digest"],
                    "policy_digest": facts["policy_digest"],
                    "controller_plist_digest":
                        facts["controller_plist_digest"],
                    "controller_supervision": controller_supervision(
                        self.canary_job()),
                    "shutdown_contract": json.loads(json.dumps(
                        dict(SHUTDOWN_CONTRACT))),
                    "canary_label": self.label,
                    "canary_plist_digest": self._plist_digest,
                    "incarnations": [
                        {**first["identity"], "runs": 1},
                        {**second["identity"], "runs": 2}],
                    "checks": {"loaded_definition": True,
                               "bootstrap": True, "keepalive": True,
                               "bootout": True, "cleanup": True,
                               "shutdown_budget": True},
                    "detail_digest": detail_digest,
                    "issued_at": time.time(),
                    "issued_monotonic": time.monotonic()}
        # The evidence envelope is validated by the frozen schema before
        # it is ever written or trusted (plan §9). The file bytes ARE the
        # canonical serialization, so the filename digest and every later
        # hash check agree (plan: digest of canonical bytes).
        validate_evidence(evidence, operator_uid=os.getuid())
        blob = canonical_bytes(evidence)
        evidence_path = (Path(self.runtime.state) / "canary" / "evidence"
                         / (hashlib.sha256(blob).hexdigest() + ".json"))
        evidence_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_blob(evidence_path, blob)
        return evidence

    # -- orchestration -------------------------------------------------------

    def prepare(self):
        """Journal the synthetic operation durably before consumption.

        Plan §3: the durable platform-canary operation (begin → pending,
        then pending → running) exists BEFORE the arm is consumed, so
        the retained scope and its journal row appear together. Any
        failure here leaves the arm untouched — never a half-consumed
        attempt.
        """
        self.shutdown.checkpoint()
        self.ticket = self._journal("begin", self._binding(), "fixture")
        self.runtime.begin_operation(self.ticket)
        # canary_phase replaces the active ticket: keep the exact
        # refreshed handle for the runtime context and every later
        # journal call.
        self.ticket = self._journal("canary_phase", self.ticket,
                                    "canary_running")
        self.runtime.refresh_operation(self.ticket)

    def run(self):
        """Carry the consumed arm through the full bounded lane.

        Returns a terminal record: ``canary_passed`` with evidence, or
        ``canary_blocked`` with the refusal code. Never raises past
        its own lane and never retries a refused step.
        """
        try:
            if self.ticket is None:
                # Direct lane construction (test path): mirror the serve
                # order by preparing the journal here.
                self.prepare()
            self.shutdown.checkpoint()
            # bootstrap (plan §6): fixed argv against the derived plist.
            self._write_plist()
            started = time.monotonic()
            self._launchctl(
                "canary-bootstrap",
                ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}",
                 str(self._plist_path)], BOOTSTRAP_CAP)
            self.steps.append({"name": "bootstrap",
                               "started_monotonic": started,
                               "completed_monotonic": time.monotonic(),
                               "outcome": "passed", "identity": None,
                               "runs": None, "loaded_exit_seconds": None,
                               "loaded_environment_digest": None,
                               "exit_code": 0})
            first = self._first_loaded()
            self.steps.append(first)
            self.steps.append(self._exit_once(first["identity"]))
            second = self._keepalive()
            self.steps.append(second)
            self.steps.append(self._bootout(second["identity"]))
            shutdown_detail = self._shutdown_detail(second["identity"])
            self.steps.append(self._cleanup())
            # Evidence only after successful cleanup (plan §9).
            detail, detail_digest = self._detail(shutdown_detail)
            evidence = self._evidence(first, second, detail_digest)
            # Release the runtime operation context before the terminal
            # journal write clears the active ticket.
            with contextlib.suppress(Exception):
                self.runtime.release_credentials()
            self._journal("canary_terminal", self.ticket, "canary_passed")
            return {"outcome": "canary_passed", "nonce": self.nonce,
                    "label": self.label, "evidence": evidence,
                    "detail": detail}
        except (Fault, ShutdownRequested) as error:
            code = (error.code if isinstance(error, Fault)
                    else "controller_stopping")
            return self._blocked(code)
        except Exception:
            return self._blocked("internal_error")

    def _blocked(self, code):
        """Mark the attempt blocked durably, with a status diagnostic."""
        with contextlib.suppress(Exception):
            self.runtime.release_credentials()
        if self.ticket is not None:
            with contextlib.suppress(Exception):
                self._journal("canary_terminal", self.ticket,
                              "canary_blocked")
        diagnostic = {"version": 1, "nonce": self.nonce, "code": code,
                      "recorded_monotonic": time.monotonic()}
        try:
            _write_blob(self.scope / "diagnostic.json",
                        json.dumps(diagnostic, sort_keys=True,
                                   allow_nan=False).encode())
        except OSError:
            pass
        return {"outcome": "canary_blocked", "code": code,
                "nonce": self.nonce}


def run_startup_canary(runtime, controller, shutdown, *, wall=None, mono=None):
    """Serve-time entry: consume one valid arm and run the bounded lane.

    Returns ``None`` when no arm exists (zero canary activity). An
    invalid namespace, binding mismatch, or refused arm returns a
    blocked record; mutations stay blocked through the journal and the
    retained scope files — the serve process itself keeps running with
    status RPC available.

    Plan §3 ordering: the exact-context verification and the durable
    platform-canary operation exist BEFORE the arm is consumed, so a
    crash between consumption and the live attempt never leaves an
    orphan consumed scope without its synthetic operation row.
    """
    state = Path(runtime.state)
    try:
        scan = startup_scan(state, wall=wall, mono=mono)
    except Fault as error:
        return _diagnostic_only(state, None, error.code)
    arm = scan["armed"]
    if arm is None:
        return None
    # The arm must bind exactly this generation's loaded sources.
    facts = runtime.binding_facts()
    if any(arm[key] != facts[key] for key in
           ("policy_digest", "controller_digest",
            "controller_plist_digest")):
        return _diagnostic_only(state, arm, "canary_binding_mismatch")
    # Exact controller context (plan §3): a serve process that is not
    # the actual managed launchd child — PPID 1 with the loaded
    # controller definition — cannot run or qualify the canary lane.
    from .runtime_identity import kernel_boot_identity
    from .process_identity import process_identity as _own_identity
    own = _own_identity(os.getpid())
    if own["ppid"] != 1 or own["uid"] != os.getuid():
        return _diagnostic_only(state, arm, "canary_identity_invalid")
    if arm["boot_identity"] != kernel_boot_identity():
        return _diagnostic_only(state, arm, "canary_binding_mismatch")
    # The durable platform-canary operation is pending BEFORE the arm
    # is consumed (plan §3): a crash after consumption leaves both the
    # retained scope and its journal row for operator reconciliation.
    scope = canary_scope(state, arm["nonce"])
    lane = _Lane(runtime, controller, shutdown, arm, scope)
    lane.prepare()
    try:
        consumed = consume_arm(state, operator_uid=os.getuid())
    except Fault as error:
        return _diagnostic_only(state, arm, error.code)
    return lane.run()


def _diagnostic_only(state, arm, code):
    """Persist a blocked diagnostic without touching live control."""
    nonce = arm.get("nonce") if isinstance(arm, dict) else None
    diagnostic = {"version": 1, "nonce": nonce, "code": code,
                  "recorded_monotonic": time.monotonic()}
    try:
        root = Path(state) / "canary"
        if root.exists():
            _write_blob(root / "diagnostic.json",
                        json.dumps(diagnostic, sort_keys=True,
                                   allow_nan=False).encode())
    except OSError:
        pass
    return {"outcome": "canary_blocked", "code": code, "nonce": nonce}