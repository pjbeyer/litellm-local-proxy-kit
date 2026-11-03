"""Strict recovery record schemas and bounded private-state inventory.

This module validates records and persists child transitions. It does not admit
an operation or grant permission to spawn or signal. Callers must supply the
engine's trusted storage callbacks and perform cross-record admission before
interpreting an inventory as clear.
"""

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import time


_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BINDING = frozenset(("request_id", "kind", "controller_epoch", "policy_digest",
                      "controller_digest", "controller_plist_digest", "asset_digest",
                      "runtime_digest"))
_OPERATION = frozenset(("version", "binding", "scope", "phase", "outcome",
                        "primary_control_committed"))
_SPAWN = frozenset(("version", "spawn_id", "binding", "purpose", "state", "identity"))
_FENCE = frozenset(("version", "binding", "phase", "state"))
_IDENTITY = frozenset(("pid", "uid", "pgid", "birth"))
_PREPARED_REQUEST = frozenset(("request_id", "phase", "controller_epoch", "scope",
    "shared_state_side_effects", "uid", "label", "original_identity",
    "asset_digest", "runtime_digest", "policy_digest", "controller_digest",
    "candidate_binding", "lkg_binding", "primary_checks", "candidate_checks",
    "prepared_at", "prepared_monotonic", "expires_at", "expires_monotonic"))
_PHASES = frozenset(("preparing", "prepared", "blocked", "restart_pending",
    "intent_consumed", "stopping", "starting", "postchecking", "restarted",
    "rollback_stopping", "rollback_restoring", "rollback_starting",
    "rollback_postchecking", "rolled_back", "shutdown_pending",
    "manual_recovery_required", "canary_pending", "canary_running",
    "canary_passed", "canary_blocked"))
_OUTCOMES = frozenset(("prepared", "blocked", "restarted", "rolled_back",
                       "manual_recovery_required", "canary_passed", "canary_blocked"))
_PURPOSES = frozenset(("runtime-proof", "preflight", "credential-resolver",
    "candidate", "diagnostic", "service-stop", "service-start",
    "cleanup-diagnostic", "canary-query", "canary-bootstrap", "canary-bootout"))
_CANARY_ONLY_PURPOSES = frozenset(("canary-query", "canary-bootstrap", "canary-bootout"))
_CANARY_PURPOSES = _CANARY_ONLY_PURPOSES | {"cleanup-diagnostic"}
_PRE_FREEZE_PURPOSES = frozenset(("runtime-proof", "diagnostic"))
_CANARY_PHASES = frozenset(("canary_pending", "canary_running", "canary_passed",
                            "canary_blocked"))
# Null proxy bindings describe only conservative pre-freeze interruption states.
_NULL_PROXY_PHASE_OUTCOMES = {
    "preparing": frozenset((None,)),
    "blocked": frozenset((None, "blocked")),
    "shutdown_pending": frozenset((None,)),
    "manual_recovery_required": frozenset((None, "manual_recovery_required")),
}


class RecoveryError(Exception):
    """Closed diagnostic; never carries path or record contents."""

    code = "recovery_invalid"

    def __init__(self):
        super().__init__(self.code)


def _require(condition):
    if not condition:
        raise RecoveryError()


def _keys(value, expected):
    _require(type(value) is dict and value.keys() == expected)


def _token(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


@dataclass(frozen=True)
class RecoveryInventory:
    operations: dict
    spawns: dict
    fence: object
    requests: dict
    approvals: dict
    entry_count: int
    canary_uncertain: bool


@dataclass(frozen=True, eq=False)
class OperationTicket:
    """Exact journal snapshot; neither admission nor permission to start a child."""

    request_id: str
    binding: tuple
    operation_snapshot: str


@dataclass(frozen=True, eq=False)
class SpawnHandle:
    """Store-issued record locator; never signal or ownership authority."""

    spawn_id: str
    request_id: str
    binding: tuple
    purpose: str


class RecoveryStore:
    """Validate records and enumerate fixed state views within hard bounds."""

    def __init__(self, state_dir, *, read_json, atomic_json, private_path,
                 private_dir, sync_fd, initialize=False):
        _require(all(callable(fn) for fn in (read_json, atomic_json, private_path,
                                            private_dir, sync_fd)))
        _require(type(initialize) is bool)
        self._read_json = read_json
        self._atomic_json = atomic_json
        self._private_path = private_path
        self._private_dir = private_dir
        self._sync_fd = sync_fd
        self._tickets = set()
        self._handles = set()
        self._uncertain = False
        self.state_dir = Path(state_dir)
        try:
            self._checked_dir(self.state_dir)
            self.recovery = self.state_dir / "recovery"
            self.operations_dir = self.recovery / "operations"
            self.spawns_dir = self.recovery / "spawns"
            self.requests_dir = self.state_dir / "requests"
            self.approvals_dir = self.state_dir / "approvals"
            views = (self.recovery, self.operations_dir, self.spawns_dir,
                     self.requests_dir, self.approvals_dir)
            if initialize:
                with os.scandir(self.state_dir) as scan:
                    _require(next(scan, None) is None)
                for path in views:
                    _require(not os.path.lexists(path))
                    self._private_dir(path)
                    self._checked_dir(path)
                    self._sync_directory(path.parent)
                # The caller created state_dir; sync its entry before any
                # caller can use this newly initialized journal.
                parent_fd = os.open(self.state_dir.parent, os.O_RDONLY |
                                    getattr(os, "O_DIRECTORY", 0))
                try:
                    self._sync_fd(parent_fd)
                finally:
                    os.close(parent_fd)
            else:
                for path in views:
                    self._checked_dir(path)
        except Exception:
            raise RecoveryError() from None

    @property
    def uncertain(self):
        """Sticky same-process indication of an ambiguous journal write."""
        return self._uncertain

    def _sync_directory(self, path):
        self._checked_dir(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            self._sync_fd(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _binding_tuple(value):
        return tuple((key, value[key]) for key in sorted(_BINDING))

    def _operation_row(self, request_id):
        _require(_token(request_id, _ID))
        return self.validate_operation(
            self._checked_file(self.operations_dir / (request_id + ".json")),
            request_id=request_id)

    def operation_ticket(self, request_id):
        """Read one existing operation exactly; T006 still owns admission."""
        row = self._operation_row(request_id)
        ticket = OperationTicket(request_id, self._binding_tuple(row["binding"]),
                                 json.dumps(row, sort_keys=True, separators=(",", ":")))
        self._tickets.add(ticket)
        return ticket

    def _check_ticket(self, ticket):
        _require(type(ticket) is OperationTicket and ticket in self._tickets)
        row = self._operation_row(ticket.request_id)
        _require(ticket.binding == self._binding_tuple(row["binding"])
                 and ticket.operation_snapshot == json.dumps(
                     row, sort_keys=True, separators=(",", ":")))
        return row

    def _spawn_row(self, handle):
        _require(type(handle) is SpawnHandle and handle in self._handles)
        operation = self._operation_row(handle.request_id)
        _require(handle.binding == self._binding_tuple(operation["binding"]))
        row = self.validate_spawn(
            self._checked_file(self.spawns_dir / (handle.spawn_id + ".json")),
            spawn_id=handle.spawn_id)
        _require(row["binding"] == operation["binding"]
                 and row["purpose"] == handle.purpose)
        return row

    @contextmanager
    def _spawn_transition_lock(self):
        """Bounded process-shared lock for the entire spawn check/write transition."""
        fd = None
        try:
            self._checked_dir(self.state_dir)
            fd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY |
                         getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(fd)
            path_info = self.state_dir.lstat()
            _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                     and stat.S_IMODE(info.st_mode) == 0o700
                     and (info.st_dev, info.st_ino) == (path_info.st_dev, path_info.st_ino))
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RecoveryError()
                    time.sleep(0.01)
            yield
        except RecoveryError:
            raise
        except Exception:
            raise RecoveryError() from None
        finally:
            if fd is not None:
                os.close(fd)

    def _write_spawn(self, row):
        self.validate_spawn(row, spawn_id=row["spawn_id"])
        try:
            self._atomic_json(self.spawns_dir / (row["spawn_id"] + ".json"), row)
        except Exception:
            self._uncertain = True
            raise RecoveryError() from None
        # A reentrant uncertainty mark can run inside an injected writer.
        # Its sticky state must not be hidden by an otherwise successful write.
        _require(not self._uncertain or row["state"] == "uncertain")

    def _write_operation(self, row):
        self.validate_operation(row, request_id=row["binding"]["request_id"])
        try:
            self._atomic_json(self.operations_dir / (row["binding"]["request_id"] + ".json"), row)
        except Exception:
            self._uncertain = True
            raise RecoveryError() from None
        _require(not self._uncertain)

    def _refreshed_ticket(self, row):
        ticket = OperationTicket(row["binding"]["request_id"],
                                 self._binding_tuple(row["binding"]),
                                 json.dumps(row, sort_keys=True, separators=(",", ":")))
        self._tickets.add(ticket)
        return ticket

    def reopen_restart(self, ticket):
        """Persist a prepared proxy operation as pending before intent consumption."""
        with self._spawn_transition_lock():
            _require(not self._uncertain)
            row = self._check_ticket(ticket)
            _require(row["binding"]["kind"] == "proxy"
                     and row["binding"]["asset_digest"] is not None
                     and row["phase"] == "prepared" and row["outcome"] == "prepared"
                     and row["primary_control_committed"] is False)
            view = self.inventory()
            _require(view.fence is None)
            self._matching_approval(view, row, consumed=False, prepared=True)
            pending = {**row, "phase": "restart_pending", "outcome": None}
            self._write_operation(pending)
            return self._refreshed_ticket(pending)

    def arm_mutation(self, ticket):
        """Sync the single fixed proxy fence before primary control commitment."""
        with self._spawn_transition_lock():
            _require(not self._uncertain)
            row = self._check_ticket(ticket)
            _require(row["binding"]["kind"] == "proxy"
                     and row["binding"]["asset_digest"] is not None
                     and row["binding"]["runtime_digest"] is not None
                     and row["phase"] == "restart_pending"
                     and row["outcome"] is None
                     and row["primary_control_committed"] is False)
            _require(self.inventory().fence is None)
            fence = {"version": 1, "binding": row["binding"].copy(),
                     "phase": "restart_pending", "state": "armed"}
            try:
                self._atomic_json(self.recovery / "mutation.json", fence)
            except Exception:
                self._uncertain = True
                raise RecoveryError() from None
            _require(not self._uncertain)
            return ticket

    def mark_mutation_uncertain(self, ticket):
        """Retain a fenced operation after outside consumption or control failure."""
        _require(type(ticket) is OperationTicket and ticket in self._tickets)
        self._uncertain = True
        with self._spawn_transition_lock():
            row = self._check_ticket(ticket)
            _require(row["phase"] not in _OUTCOMES and row["outcome"] is None)
            fence = self.inventory().fence
            _require(fence is not None and fence["binding"] == row["binding"])
            if fence["state"] == "armed":
                try:
                    self._atomic_json(self.recovery / "mutation.json",
                                      {**fence, "state": "uncertain"})
                except Exception:
                    raise RecoveryError() from None

    def phase(self, ticket, phase, *, committed_action=None):
        """Index a nonterminal phase; a claimed Shutdown permit records commitment."""
        from .shutdown import Shutdown

        _require(type(phase) is str and phase in (
            "intent_consumed", "stopping", "starting", "postchecking",
            "rollback_stopping", "rollback_restoring", "rollback_starting",
            "rollback_postchecking", "manual_recovery_required"))
        with self._spawn_transition_lock():
            _require(not self._uncertain)
            row = self._check_ticket(ticket)
            view = self.inventory()
            fence = view.fence
            _require(row["binding"]["kind"] == "proxy"
                     and row["outcome"] is None
                     and row["phase"] in ("restart_pending", "intent_consumed",
                         "stopping", "starting", "postchecking", "rollback_stopping",
                         "rollback_restoring", "rollback_starting", "rollback_postchecking")
                     and fence is not None and fence["binding"] == row["binding"]
                     and fence["phase"] == row["phase"] and fence["state"] == "armed"
                     and view.approvals.get(ticket.request_id, (False,))[0] is True)
            self._matching_consumed(view, row)
            committed = row["primary_control_committed"]
            if committed_action is not None:
                _require(not committed and type(committed_action) is tuple
                         and len(committed_action) == 2
                         and type(committed_action[0]) is Shutdown)
                purpose = ("service-stop" if phase in ("stopping", "rollback_stopping")
                           else "service-start" if phase in ("starting", "rollback_starting")
                           else None)
                _require(purpose is not None
                         and getattr(committed_action[1], "purpose", None) == purpose)
                try:
                    _require(committed_action[0].claim(committed_action[1]) == purpose)
                except Exception:
                    raise RecoveryError() from None
                committed = True
            updated = {**row, "phase": phase,
                       "primary_control_committed": committed}
            self._write_operation(updated)
            try:
                self._atomic_json(self.recovery / "mutation.json",
                                  {**fence, "phase": phase,
                                   "state": "uncertain" if phase == "manual_recovery_required"
                                   else "armed"})
            except Exception:
                self._uncertain = True
                raise RecoveryError() from None
            _require(not self._uncertain)
            return self._refreshed_ticket(updated)

    def _matching_approval(self, view, row, *, consumed, prepared=False):
        """Validate an existing engine request and the approval at one stage."""
        from . import engine

        request_id = row["binding"]["request_id"]
        _require(request_id in view.requests and request_id in view.approvals
                 and view.approvals[request_id][0] is consumed)
        plan = self._checked_file(self.requests_dir / (request_id + ".json"))
        approval = self._checked_file(self.approvals_dir / (request_id +
                                      (".consumed" if consumed else ".json")))
        _require(type(plan) is dict and type(approval) is dict
                 and plan == view.requests[request_id]
                 and approval == view.approvals[request_id][1]
                 and plan.get("request_id") == request_id
                 and plan.get("scope") == row["scope"])
        if prepared:
            _require(plan.keys() == _PREPARED_REQUEST and plan["phase"] == "prepared")
        else:
            _require(_PREPARED_REQUEST <= plan.keys())
        _require(type(plan["phase"]) is str
                 and type(plan["shared_state_side_effects"]) is bool
                 and plan["shared_state_side_effects"] is (row["scope"] == "production")
                 and type(plan["uid"]) is int and plan["uid"] >= 0
                 and type(plan["label"]) is str and bool(plan["label"])
                 and engine._valid_identity(plan["original_identity"])
                 and plan["original_identity"]["uid"] == plan["uid"]
                 and type(plan["primary_checks"]) is dict
                 and type(plan["candidate_checks"]) is dict)
        for key in ("controller_epoch", "asset_digest", "runtime_digest",
                    "policy_digest", "controller_digest"):
            _require(_token(plan[key], _ID if key == "controller_epoch" else _DIGEST)
                     and plan[key] == row["binding"][key])
        frozen = {key: plan[key] for key in ("asset_digest", "runtime_digest")}
        _require(plan["candidate_binding"] == frozen and plan["lkg_binding"] == frozen
                 and type(plan["candidate_binding"]) is dict
                 and type(plan["lkg_binding"]) is dict)
        try:
            expected = engine.approval_binding(plan)
        except (KeyError, TypeError):
            raise RecoveryError() from None
        times = ("issued_at", "issued_monotonic", "expires_at",
                 "expires_monotonic")
        _require(approval.keys() == ({"version", "owner_uid"} | expected.keys() | set(times))
                 and type(approval["version"]) is int and approval["version"] == 1
                 and type(approval["owner_uid"]) is int
                 and approval["owner_uid"] == plan["uid"]
                 and all(type(approval[key]) in (int, float) and
                         math.isfinite(approval[key]) for key in times))
        _require(engine.canonical_digest({key: approval[key] for key in expected})
                 == engine.canonical_digest(expected))
        _require(all(type(plan.get(key)) in (int, float) and
                     math.isfinite(plan[key]) for key in
                     ("prepared_at", "prepared_monotonic", "expires_at",
                      "expires_monotonic")))
        _require(plan["prepared_at"] <= approval["issued_at"] < approval["expires_at"]
                 <= plan["expires_at"]
                 and plan["prepared_monotonic"] <= approval["issued_monotonic"]
                 < approval["expires_monotonic"] <= plan["expires_monotonic"]
                 and 0 < approval["expires_at"] - approval["issued_at"] <= 300
                 and 0 < approval["expires_monotonic"] - approval["issued_monotonic"] <= 300
                 and plan["prepared_at"] < plan["expires_at"]
                 and plan["prepared_monotonic"] < plan["expires_monotonic"])
        return plan

    def _matching_consumed(self, view, row):
        return self._matching_approval(view, row, consumed=True)

    def _accepted_terminal(self, view, row):
        """Reread accepted payload; the request is authority, journal is index."""
        from . import engine

        fence = view.fence
        _require(fence is not None and fence["binding"] == row["binding"]
                 and fence["phase"] == row["phase"] and fence["state"] == "armed"
                 and row["primary_control_committed"] is True
                 and all(child["state"] == "closed" for child in view.spawns.values()))
        plan = self._matching_consumed(view, row)
        terminal = plan.get("phase")
        _require((terminal == "restarted" and row["phase"] == "postchecking")
                 or (terminal == "rolled_back" and
                     row["phase"] == "rollback_postchecking"))
        _require(plan.keys() == (_PREPARED_REQUEST |
                 {"started" if terminal == "restarted" else "rollback"}))
        accepted = plan.get("started" if terminal == "restarted" else "rollback")
        _require(type(accepted) is dict and accepted.keys() == {"identity", "checks"}
                 and engine._valid_identity(accepted["identity"])
                 and type(accepted["checks"]) is dict
                 and engine._valid_identity(plan.get("original_identity"))
                 and accepted["identity"]["uid"] == plan.get("uid")
                 and accepted["identity"] != plan["original_identity"])
        return terminal

    def finish(self, ticket):
        """Index accepted terminal evidence, then unlink and sync the fence."""
        with self._spawn_transition_lock():
            _require(not self._uncertain)
            row = self._check_ticket(ticket)
            _require(row["outcome"] is None)
            view = self.inventory()
            terminal = self._accepted_terminal(view, row)
            updated = {**row, "phase": terminal, "outcome": terminal}
            self._write_operation(updated)
            try:
                # A changed or missing fence is never an unlink target.
                _require(self.validate_fence(self._checked_file(
                    self.recovery / "mutation.json")) == view.fence)
                os.unlink(self.recovery / "mutation.json")
                self._sync_directory(self.recovery)
            except Exception:
                self._uncertain = True
                raise RecoveryError() from None
            _require(not self._uncertain)
            return self._refreshed_ticket(updated)

    def spawn_intent(self, ticket, spawn_id, purpose):
        """Persist a child intent before the caller performs any external action."""
        with self._spawn_transition_lock():
            _require(not self._uncertain)
            operation = self._check_ticket(ticket)
            _require(operation["outcome"] is None
                     and operation["phase"] not in _OUTCOMES
                     and operation["phase"] != "shutdown_pending")
            _require(_token(spawn_id, _ID))
            _require(type(purpose) is str and purpose in _PURPOSES)
            # Also refuses stray atomic temp files, population overflow and bad views.
            view = self.inventory()
            _require(spawn_id not in view.spawns)
            row = {"version": 1, "spawn_id": spawn_id,
                   "binding": operation["binding"].copy(), "purpose": purpose,
                   "state": "intent", "identity": None}
            self.validate_spawn(row, spawn_id=spawn_id)
            _require(sum(item["binding"]["request_id"] == ticket.request_id
                         for item in view.spawns.values()) < 64)
            self._write_spawn(row)
            handle = SpawnHandle(spawn_id, ticket.request_id, ticket.binding, purpose)
            self._handles.add(handle)
            return handle

    def spawn_owned(self, handle, identity):
        """Record caller-acquired identity; no ownership from an unknown identity."""
        _require(type(handle) is SpawnHandle and handle in self._handles)
        try:
            with self._spawn_transition_lock():
                _require(not self._uncertain)
                row = self._spawn_row(handle)
                _require(row["state"] == "intent" and row["identity"] is None)
                self._validate_identity(identity)
                self._write_spawn({**row, "state": "owned", "identity": identity})
        except Exception:
            self._uncertain = True
            raise

    def spawn_closed(self, handle, identity, *, cleanup_proven):
        """Close only after the caller proves cleanup of the exact owned identity."""
        _require(type(handle) is SpawnHandle and handle in self._handles)
        _require(cleanup_proven is True)
        self._validate_identity(identity)
        try:
            with self._spawn_transition_lock():
                _require(not self._uncertain)
                row = self._spawn_row(handle)
                _require(row["state"] == "owned" and row["identity"] == identity)
                self._write_spawn({**row, "state": "closed"})
        except Exception:
            self._uncertain = True
            raise

    def mark_uncertain(self, handle):
        """Retain an interrupted intent or owned identity without granting control."""
        _require(type(handle) is SpawnHandle and handle in self._handles)
        self._uncertain = True
        with self._spawn_transition_lock():
            row = self._spawn_row(handle)
            _require(row["state"] in ("intent", "owned", "uncertain"))
            if row["state"] != "uncertain":
                self._write_spawn({**row, "state": "uncertain"})

    def validate_binding(self, value):
        _keys(value, _BINDING)
        _require(_token(value["request_id"], _ID)
                 and value["kind"] in ("proxy", "platform-canary")
                 and type(value["kind"]) is str
                 and _token(value["controller_epoch"], _ID))
        for field in ("policy_digest", "controller_digest", "controller_plist_digest"):
            _require(_token(value[field], _DIGEST))
        asset, runtime = value["asset_digest"], value["runtime_digest"]
        _require((asset is None and runtime is None)
                 or (_token(asset, _DIGEST) and _token(runtime, _DIGEST)))
        _require(value["kind"] != "platform-canary" or asset is None)
        return value

    def validate_operation(self, value, *, request_id=None):
        _keys(value, _OPERATION)
        self.validate_binding(value["binding"])
        _require(request_id is None or value["binding"]["request_id"] == request_id)
        _require(type(value["version"]) is int and value["version"] == 1
                 and type(value["scope"]) is str and value["scope"] in ("fixture", "production")
                 and type(value["phase"]) is str and value["phase"] in _PHASES
                 and (value["outcome"] is None or
                      (type(value["outcome"]) is str and value["outcome"] in _OUTCOMES))
                 and type(value["primary_control_committed"]) is bool)
        kind = value["binding"]["kind"]
        if kind == "platform-canary":
            _require(value["phase"] in _CANARY_PHASES
                     and value["outcome"] in (None, "canary_passed", "canary_blocked")
                     and value["primary_control_committed"] is False)
        else:
            _require(value["phase"] not in _CANARY_PHASES
                     and value["outcome"] not in ("canary_passed", "canary_blocked"))
            if value["binding"]["asset_digest"] is None:
                _require(value["outcome"] in
                         _NULL_PROXY_PHASE_OUTCOMES.get(value["phase"], ())
                         and value["primary_control_committed"] is False)
        return value

    def validate_spawn(self, value, *, spawn_id=None):
        _keys(value, _SPAWN)
        self.validate_binding(value["binding"])
        _require(type(value["version"]) is int and value["version"] == 1
                 and _token(value["spawn_id"], _ID)
                 and (spawn_id is None or value["spawn_id"] == spawn_id)
                 and type(value["purpose"]) is str and value["purpose"] in _PURPOSES
                 and type(value["state"]) is str
                 and value["state"] in ("intent", "owned", "closed", "uncertain"))
        kind = value["binding"]["kind"]
        if kind == "platform-canary":
            _require(value["purpose"] in _CANARY_PURPOSES)
        else:
            _require(value["purpose"] not in _CANARY_ONLY_PURPOSES)
            if value["binding"]["asset_digest"] is None:
                _require(value["purpose"] in _PRE_FREEZE_PURPOSES)
        identity = value["identity"]
        if identity is None:
            _require(value["state"] in ("intent", "uncertain"))
        else:
            self._validate_identity(identity)
        return value

    @staticmethod
    def _validate_identity(identity):
        _keys(identity, _IDENTITY)
        _require(type(identity["pid"]) is int and identity["pid"] > 0
                 and type(identity["uid"]) is int and identity["uid"] >= 0
                 and type(identity["pgid"]) is int and identity["pgid"] > 0)
        birth = identity["birth"]
        _require(type(birth) is list and len(birth) == 2
                 and type(birth[0]) is int and birth[0] > 0
                 and type(birth[1]) is int and 0 <= birth[1] < 1000000)

    def validate_fence(self, value):
        _keys(value, _FENCE)
        self.validate_binding(value["binding"])
        _require(type(value["version"]) is int and value["version"] == 1
                 and value["binding"]["kind"] == "proxy"
                 and value["binding"]["asset_digest"] is not None
                 and type(value["phase"]) is str and value["phase"] in _PHASES
                 and value["phase"] not in _CANARY_PHASES
                 and type(value["state"]) is str and value["state"] in ("armed", "uncertain"))
        return value

    def _checked_dir(self, path, *, optional=False):
        try:
            exists = self._private_path(path, True)
            if not exists:
                _require(optional)
                return False
            info = path.lstat()
            _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                     and stat.S_IMODE(info.st_mode) == 0o700)
            return True
        except Exception:
            raise RecoveryError() from None

    def _checked_file(self, path):
        try:
            _require(self._private_path(path, False))
            info = path.lstat()
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                     and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
            return self._read_json(path)
        except Exception:
            raise RecoveryError() from None

    def inventory(self):
        """Return bounded facts, never an admission or clearance decision."""
        self._checked_dir(self.state_dir)
        count = 0

        def entries(path, optional=False):
            nonlocal count
            if not self._checked_dir(path, optional=optional):
                return
            try:
                with os.scandir(path) as scan:
                    for entry in scan:
                        count += 1
                        _require(count <= 1024)
                        yield entry.name
            except Exception:
                raise RecoveryError() from None

        def id_name(name, suffix=".json"):
            _require(name.endswith(suffix) and _token(name[:-len(suffix)], _ID))
            return name[:-len(suffix)]

        operations = {}
        for name in entries(self.operations_dir):
            key = id_name(name)
            operations[key] = self.validate_operation(
                self._checked_file(self.operations_dir / name), request_id=key)
        spawns = {}
        per_operation = {}
        for name in entries(self.spawns_dir):
            key = id_name(name)
            row = self.validate_spawn(self._checked_file(self.spawns_dir / name), spawn_id=key)
            request_id = row["binding"]["request_id"]
            per_operation[request_id] = per_operation.get(request_id, 0) + 1
            _require(per_operation[request_id] <= 64)
            if request_id in operations:
                _require(row["binding"] == operations[request_id]["binding"])
            spawns[key] = row
        fence = None
        for name in entries(self.recovery):
            _require(name in ("operations", "spawns", "mutation.json"))
            if name == "mutation.json":
                fence = self.validate_fence(self._checked_file(self.recovery / name))
                request_id = fence["binding"]["request_id"]
                if request_id in operations:
                    _require(fence["binding"] == operations[request_id]["binding"])
        requests = {}
        for name in entries(self.requests_dir):
            key = id_name(name)
            requests[key] = self._checked_file(self.requests_dir / name)
        approvals = {}
        for name in entries(self.approvals_dir):
            if name.endswith(".consumed"):
                key = id_name(name, ".consumed")
            else:
                key = id_name(name)
            _require(key not in approvals)
            approvals[key] = (name.endswith(".consumed"),
                              self._checked_file(self.approvals_dir / name))
        canary = self.state_dir / "canary"
        canary_uncertain = False
        for name in entries(canary, optional=True):
            canary_uncertain = True
            path = canary / name
            if name == "intent.json":
                self._checked_file(path)
            elif name in ("detail", "evidence"):
                for child in entries(path):
                    _require(child.endswith(".json") and
                             _token(child[:-5], _DIGEST))
                    self._checked_file(path / child)
            elif _token(name, _ID):
                for child in entries(path):
                    # T016 has not frozen receipt names or their schemas.
                    # A scope entry is uncertainty, never clearance.
                    raise RecoveryError()
            else:
                raise RecoveryError()
        return RecoveryInventory(operations, spawns, fence, requests, approvals,
                                 count, canary_uncertain)
