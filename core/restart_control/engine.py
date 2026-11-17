"""Durable single-use restart state machine with no production backend."""

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path


CODES = frozenset("invalid_request busy state_io approval_missing approval_invalid approval_stale plan_stale unknown_request replayed interrupted clock_invalid unmanaged_listener ambiguous_listener primary_changed gate_failed candidate_failed candidate_cleanup_failed lkg_invalid runtime_invalid qualification_missing qualification_invalid service_control_denied service_control_timeout postcheck_failed rollback_failed internal_error controller_unavailable foreign_peer socket_exists controller_stopping recovery_invalid canary_binding_mismatch canary_arm_expired canary_identity_invalid canary_first_load_failed canary_exit_failed canary_keepalive_failed canary_cleanup_failed canary_shutdown_receipt_missing canary_shutdown_unresolved canary_job_absent".split())
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_APPROVAL_TIME = {"issued_at", "expires_at", "issued_monotonic", "expires_monotonic"}
_SYNC = getattr(fcntl, "F_FULLFSYNC", None)


class Fault(Exception):
    """A closed diagnostic with no backend text."""

    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in CODES else "internal_error"
        super().__init__(self.code)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise Fault("invalid_request")
        result[key] = value
    return result


def strict_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(Fault("invalid_request")))
    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise Fault("invalid_request") from None


def canonical_bytes(obj):
    """The one canonical serialization: compact, sorted, no NaN, UTF-8.

    Producer filenames, file bytes, qualification hash checks and scan
    recomputation all use these exact bytes, so the on-disk hash equals
    the canonical digest by construction.
    """
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise Fault("invalid_request") from None

def canonical_digest(obj):
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


# Transient atomic-rename temporary from atomic_json's staging step:
# ".<final-name>.<32-hex>". atomic_json stages exactly this shape next to
# the final file and renames it into place; a concurrent directory scan
# can observe it mid-write. It is a dotfile by construction, uid-owned,
# and disappears on rename or crash-cleanup — scanners treat a LIVE one
# as "not yet" (bounded re-scan), and one that persists past the scan
# window as a crashed-write uncertainty that blocks (never cleans).
_ATOMIC_TEMP = re.compile(r"^\.([A-Za-z0-9._-]+)\.([0-9a-f]{32})$")


def atomic_temp_name(name):
    """True when name is an atomic_json in-flight temporary for a known file."""
    match = _ATOMIC_TEMP.match(name or "")
    if match is None:
        return False
    return match.group(1) in ("intent.json", "latch.json", "diagnostic.json",
                              "mutation.json") or \
        match.group(1).startswith("local.restart-canary.") or \
        match.group(1).endswith(".plist") or \
        _token_like(match.group(1))


def _token_like(value):
    from .recovery import _token, _ID
    try:
        return _token(value, _ID)
    except Exception:
        return False


def _sync_fd(fd):
    if _SYNC is not None:
        fcntl.fcntl(fd, _SYNC)
    else:
        os.fsync(fd)

def _sync_dir(path):
    """Sync one directory and always close its descriptor (no leaks)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        _sync_fd(fd)
    finally:
        os.close(fd)


def _private_path(path, directory):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise Fault("state_io")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if info.st_uid != os.getuid() or info.st_mode & 0o777 != (0o700 if directory else 0o600):
        raise Fault("state_io")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise Fault("state_io")
    return True


def private_dir(path):
    path = Path(path)
    try:
        if not _private_path(path, True):
            path.mkdir(mode=0o700)
            _private_path(path, True)
        return path
    except (OSError, ValueError) as exc:
        raise Fault("state_io") from None


def _read_json(path):
    try:
        if not _private_path(path, False):
            raise FileNotFoundError
        with open(path, "rb") as stream:
            raw = stream.read(65537)
            if len(raw) > 65536:
                raise Fault("state_io")
            return strict_json(raw)
    except FileNotFoundError:
        raise
    except Fault as exc:
        raise Fault("state_io") from None
    except OSError:
        raise Fault("state_io") from None


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        if not _private_path(path.parent, True):
            raise Fault("state_io")
        _private_path(path, False)
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                _sync_fd(stream.fileno())
            os.replace(tmp, path)
            dirfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                _sync_fd(dirfd)
            finally:
                os.close(dirfd)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            raise
    except (OSError, TypeError, ValueError, OverflowError):
        raise Fault("state_io") from None


def approval_binding(plan):
    return {key: plan[key] for key in ("request_id", "uid", "label", "original_identity",
            "asset_digest", "runtime_digest", "policy_digest", "controller_digest",
            "candidate_binding", "lkg_binding", "controller_epoch")}


def _valid_identity(identity):
    return (isinstance(identity, dict) and set(identity) == {"pid", "uid", "birth"}
            and type(identity["pid"]) is int and identity["pid"] > 0
            and type(identity["uid"]) is int and identity["uid"] >= 0
            and isinstance(identity["birth"], list) and len(identity["birth"]) == 2
            and all(type(x) is int and x >= 0 for x in identity["birth"]))


def _valid_freeze(value):
    fields = {"asset_digest", "runtime_digest", "policy_digest", "controller_digest"}
    return isinstance(value, dict) and set(value) == fields and all(
        isinstance(value[k], str) and _DIGEST.fullmatch(value[k]) for k in fields)


def _result(status, code=None, **fields):
    value = {"status": status, **fields}
    if code is not None:
        value["code"] = code
    return value


class FixtureLifecyclePort:
    """Explicit test-only admission port; concrete adapter lives with fixtures."""

    def __init__(self):
        from .shutdown import Shutdown
        self.shutdown = Shutdown()

    def require_clear(self):
        raise NotImplementedError

    def bind_backend(self, backend):
        raise NotImplementedError

    def spawn_intent(self, ticket, spawn_id, purpose):
        raise NotImplementedError

    def spawn_owned(self, handle, identity):
        raise NotImplementedError

    def spawn_closed(self, handle, identity, *, cleanup_proven):
        raise NotImplementedError

    def mark_uncertain(self, handle):
        raise NotImplementedError


class Controller:
    def __init__(self, state_dir, backend, uid=None, clock=time.time, monotonic=time.monotonic,
                 *, shutdown=None, test_lifecycle=None):
        from .recovery import RecoveryError, RecoveryStore
        from .shutdown import Shutdown

        if test_lifecycle is not None:
            if (not isinstance(test_lifecycle, FixtureLifecyclePort)
                    or getattr(backend, "fixture", False) is not True or shutdown is not None):
                raise Fault("invalid_request")
            shutdown = test_lifecycle.shutdown
        if type(shutdown) is not Shutdown:
            raise Fault("invalid_request")
        self.shutdown = shutdown
        self.state_dir = private_dir(state_dir)
        self.requests = self.state_dir / "requests"
        self.approvals = self.state_dir / "approvals"
        self.snapshots = self.state_dir / "snapshots"
        self.backend = backend
        self.uid = os.getuid() if uid is None else uid
        if type(self.uid) is not int or self.uid < 0 or not isinstance(backend.label, str):
            raise Fault("invalid_request")
        self.clock, self.monotonic = clock, monotonic
        self.epoch = uuid.uuid4().hex
        self._mutex = threading.Lock()
        self._last_wall = None
        self._last_mono = None
        self._lock_path = self.state_dir / "controller.lock"
        self._recovery_invalid = False
        if test_lifecycle is not None:
            self.recovery = test_lifecycle
            self.requests = private_dir(self.requests)
            self.approvals = private_dir(self.approvals)
            self.snapshots = private_dir(self.snapshots)
            test_lifecycle.bind_backend(backend)
            try:
                fd = os.open(self._lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                pass
            except OSError:
                raise Fault("state_io") from None
            else:
                os.close(fd)
            _private_path(self._lock_path, False)
        else:
            try:
                with os.scandir(self.state_dir) as scan:
                    initialize = next(scan, None) is None
                self.recovery = RecoveryStore(
                    self.state_dir, read_json=_read_json, atomic_json=atomic_json,
                    private_path=_private_path, private_dir=private_dir,
                    sync_fd=_sync_fd, initialize=initialize)
                if initialize:
                    self.snapshots = private_dir(self.snapshots)
                    fd = os.open(self._lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                    os.close(fd)
                if not _private_path(self.snapshots, True) or not _private_path(self._lock_path, False):
                    raise RecoveryError()
            except (OSError, RecoveryError, Fault):
                self.recovery = None
                self._recovery_invalid = True
                return
            try:
                backend.bind_lifecycle(shutdown, self.recovery)
            except Exception:
                raise Fault("runtime_invalid") from None

    def _time(self):
        wall, mono = self.clock(), self.monotonic()
        if (type(wall) not in (int, float) or type(mono) not in (int, float)
                or not math.isfinite(wall) or not math.isfinite(mono)
                or (self._last_wall is not None and wall < self._last_wall)
                or (self._last_mono is not None and mono < self._last_mono)):
            self.epoch = uuid.uuid4().hex
            raise Fault("clock_invalid")
        self._last_wall, self._last_mono = wall, mono
        return wall, mono

    @contextlib.contextmanager
    def _locked(self):
        if not self._mutex.acquire(False):
            raise Fault("busy")
        fd = None
        try:
            fd = os.open(self._lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            _private_path(self._lock_path, False)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Fault("busy") from None
            yield
        except OSError:
            raise Fault("state_io") from None
        finally:
            if fd is not None:
                os.close(fd)
            self._mutex.release()

    def _call(self, name, *args):
        try:
            return getattr(self.backend, name)(*args)
        except Fault as exc:
            raise Fault(exc.code) from None
        except Exception:
            raise Fault("internal_error") from None

    def _inspect(self):
        value = self._call("inspect")
        if not isinstance(value, dict) or set(value) != {"identity", "managed"} or not _valid_identity(value["identity"]) or type(value["managed"]) is not bool:
            raise Fault("internal_error")
        return value

    def _freeze(self):
        value = self._call("freeze")
        if not _valid_freeze(value):
            raise Fault("runtime_invalid")
        return value

    def _record(self, plan):
        atomic_json(self.requests / (plan["request_id"] + ".json"), plan)

    def _status(self):
        if self._recovery_invalid:
            return _result("blocked", "recovery_invalid")
        try:
            self.recovery.require_clear()
        except Exception:
            return _result("blocked", "recovery_invalid")
        recent = []
        try:
            paths = sorted(self.requests.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
            for path in paths:
                record = _read_json(path)
                recent.append({"request_id": record.get("request_id"), "phase": record.get("phase"),
                               "asset_digest": record.get("asset_digest")})
        except (OSError, Fault):
            raise Fault("state_io") from None
        return _result("ok", epoch=self.epoch, recent=recent)

    def _admit(self):
        """Latch admission between backend steps; never wrap the stop away."""
        from .shutdown import ShutdownRequested
        try:
            self.shutdown.checkpoint()
        except ShutdownRequested:
            raise Fault("controller_stopping") from None

    def _journal(self, name, *args, **kwargs):
        """One closed mapping from journal failure to a recovery Fault."""
        from .recovery import RecoveryError
        try:
            return getattr(self.recovery, name)(*args, **kwargs)
        except Fault:
            raise
        except RecoveryError:
            raise Fault("recovery_invalid") from None
        except Exception:
            raise Fault("recovery_invalid") from None

    def _begin_backend_operation(self, ticket):
        """Give a lifecycle-aware backend the exact active journal context."""
        starter = getattr(self.backend, "begin_operation", None)
        if starter is None:
            return
        try:
            starter(ticket)
        except Fault:
            raise
        except Exception:
            raise Fault("recovery_invalid") from None

    def _prepare(self):
        wall, mono = self._time()
        request_id = uuid.uuid4().hex
        before = None
        ticket = None
        plan = {"request_id": request_id, "phase": "preparing", "controller_epoch": self.epoch,
                "scope": "fixture" if getattr(self.backend, "fixture", False) else "production",
                "shared_state_side_effects": not getattr(self.backend, "fixture", False)}
        try:
            self._admit()
            before = self._inspect()
            if before["identity"]["uid"] != self.uid:
                raise Fault("primary_changed")
            facts = getattr(self.backend, "binding_facts", None)
            if facts is not None:
                facts = self._call("binding_facts")
                if (not isinstance(facts, dict)
                        or set(facts) != {"policy_digest", "controller_digest", "controller_plist_digest"}
                        or not all(isinstance(facts[k], str) and _DIGEST.fullmatch(facts[k]) for k in facts)):
                    raise Fault("runtime_invalid")
            if facts is not None:
                binding = {"request_id": request_id, "kind": "proxy",
                           "controller_epoch": self.epoch,
                           "policy_digest": facts["policy_digest"],
                           "controller_digest": facts["controller_digest"],
                           "controller_plist_digest": facts["controller_plist_digest"],
                           "asset_digest": None, "runtime_digest": None}
                # The preparing operation is durable before any helper can
                # exist and before its request record: the store requires a
                # request to be covered by an operation row.
                ticket = self._journal("begin", binding, plan["scope"])
                self._begin_backend_operation(ticket)
            self._record(plan)
            frozen = self._freeze()
            self._admit()
            self._call("check_gates")
            primary_checks = self._call("accept_primary", before["identity"])
            self._admit()
            candidate_checks = self._call("rehearse")
            if not isinstance(primary_checks, dict) or not isinstance(candidate_checks, dict):
                raise Fault("candidate_failed")
            binding2 = {"asset_digest": frozen["asset_digest"], "runtime_digest": frozen["runtime_digest"]}
            snapshot_path = self.snapshots / request_id
            private_dir(snapshot_path)
            self._admit()
            lkg_binding = self._call("snapshot_lkg", snapshot_path)
            if lkg_binding != binding2:
                raise Fault("lkg_invalid")
            self._admit()
            after = self._inspect()
            if after != before:
                raise Fault("primary_changed")
            self._admit()
            if self._freeze() != frozen:
                raise Fault("plan_stale")
            if not before["managed"]:
                raise Fault("unmanaged_listener")
            wall, mono = self._time()
            plan.update({"phase": "prepared", "uid": self.uid, "label": self.backend.label,
                         "original_identity": before["identity"], **frozen,
                         "candidate_binding": binding2, "lkg_binding": lkg_binding,
                         "primary_checks": primary_checks, "candidate_checks": candidate_checks,
                         "prepared_at": wall, "prepared_monotonic": mono,
                         "expires_at": wall + 300, "expires_monotonic": mono + 300})
            # Freeze both digests once while the on-disk request is still
            # the minimal preparing record, then land the prepared record
            # against the frozen row: the production store admits a
            # prepared request only after the binding freeze, and
            # bind_frozen itself validates the minimal preparing record.
            ticket = self._journal("bind_frozen", ticket,
                                   frozen["asset_digest"], frozen["runtime_digest"])
            self._record(plan)
            self._journal("finish", ticket)
            return _result("prepared", **plan)
        except Fault as exc:
            if before is not None:
                try:
                    plan["original_preserved"] = self._inspect()["identity"] == before["identity"]
                except Fault:
                    plan["original_preserved"] = False
            plan.update(phase="blocked", code=exc.code)
            if ticket is not None:
                # Only a journaled request may carry a durable blocked
                # record: the store requires every request to be covered
                # by an operation row, so recording a request that never
                # reached `begin` would leave an orphan that permanently
                # blocks recovery. The refusal result still carries the
                # code to the caller.
                self._record(plan)
                # Zero-control interruption: close the preparing stage against
                # the durable blocked request; a failed close leaves the row
                # pending for operator reconciliation, never a silent retry.
                with contextlib.suppress(Exception):
                    self._journal("finish", ticket)
                with contextlib.suppress(Exception):
                    self._journal("end_operation", ticket)
            return _result("blocked", exc.code, request_id=request_id,
                           original_preserved=plan.get("original_preserved", False), scope=plan["scope"])

    def _approval(self, plan, wall, mono, consumed=False):
        path = self.approvals / (plan["request_id"] + (".consumed" if consumed else ".json"))
        try:
            approval = _read_json(path)
        except FileNotFoundError:
            raise Fault("approval_missing") from None
        except Fault:
            raise Fault("approval_invalid") from None
        if not isinstance(approval, dict) or set(approval) != ({"version", "owner_uid"} | set(approval_binding(plan)) | _APPROVAL_TIME):
            raise Fault("approval_invalid")
        if type(approval["version"]) is not int or approval["version"] != 1 or type(approval["owner_uid"]) is not int or approval["owner_uid"] != self.uid:
            raise Fault("approval_invalid")
        if canonical_digest({k: approval[k] for k in approval_binding(plan)}) != canonical_digest(approval_binding(plan)):
            raise Fault("approval_invalid")
        if any(type(approval[k]) not in (int, float) or not math.isfinite(approval[k]) for k in _APPROVAL_TIME):
            raise Fault("approval_invalid")
        if not (plan["prepared_at"] <= approval["issued_at"] <= wall < approval["expires_at"] <= plan["expires_at"]
                and plan["prepared_monotonic"] <= approval["issued_monotonic"] <= mono < approval["expires_monotonic"] <= plan["expires_monotonic"]
                and 0 < approval["expires_at"] - approval["issued_at"] <= 300
                and 0 < approval["expires_monotonic"] - approval["issued_monotonic"] <= 300):
            raise Fault("approval_stale")
        return path

    def _phase(self, plan, phase):
        plan["phase"] = phase
        self._record(plan)

    def _manual(self, plan, ticket, code):
        failed_phase = plan["phase"]
        plan.update(code=code, failed_phase=failed_phase)
        # A failed durable write cannot authorize another service-control call.
        # Preserve the previous on-disk uncertainty and report manual recovery.
        with contextlib.suppress(Fault):
            self._phase(plan, "manual_recovery_required")
        with contextlib.suppress(Exception):
            self._journal("phase", ticket, "manual_recovery_required")
        return _result("manual_recovery_required", code, request_id=plan["request_id"],
                       failed_phase=failed_phase, scope=plan["scope"])

    def _accept_started(self, original_identity):
        value = self._call("accept_started")
        if (not isinstance(value, dict) or set(value) != {"identity", "checks"}
                or not _valid_identity(value["identity"]) or value["identity"]["uid"] != self.uid
                or value["identity"] == original_identity or not isinstance(value["checks"], dict)):
            raise Fault("postcheck_failed")
        return value

    def _restart(self, request):
        wall, mono = self._time()
        request_id = request["request_id"]
        try:
            plan = _read_json(self.requests / (request_id + ".json"))
        except FileNotFoundError:
            raise Fault("unknown_request") from None
        if not isinstance(plan, dict) or plan.get("request_id") != request_id:
            raise Fault("state_io")
        if (plan.get("phase") != "prepared" or (self.approvals / (request_id + ".consumed")).exists()):
            raise Fault("replayed")
        if plan.get("controller_epoch") != self.epoch or wall >= plan.get("expires_at", -1) or mono >= plan.get("expires_monotonic", -1):
            raise Fault("plan_stale")
        if request["asset_digest"] != plan.get("asset_digest"):
            raise Fault("plan_stale")
        # A checked refusal (missing/invalid/stale approval) must not poison
        # the durable prepared record: validate fully before any journal
        # reopen; reopen then revalidates the unconsumed record itself.
        approval = self._approval(plan, wall, mono)
        self._admit()
        self._call("qualify", plan)
        if self._freeze() != {k: plan[k] for k in ("asset_digest", "runtime_digest", "policy_digest", "controller_digest")}:
            raise Fault("plan_stale")
        current = self._inspect()
        if current["identity"] != plan["original_identity"] or not current["managed"]:
            raise Fault("primary_changed")
        self._admit()
        self._call("check_gates")
        snapshot_path = self.snapshots / request_id
        self._call("validate_lkg", snapshot_path, plan["lkg_binding"])
        self._call("accept_primary", current["identity"])
        wall, mono = self._time()
        self._approval(plan, wall, mono)
        if self._freeze() != {k: plan[k] for k in ("asset_digest", "runtime_digest", "policy_digest", "controller_digest")} or self._inspect() != current:
            raise Fault("plan_stale")
        self._admit()
        self._call("qualify", plan)
        wall, mono = self._time()
        self._approval(plan, wall, mono)
        # Reopen the durable prepared operation as restart_pending under the
        # mutation lock only after every read-only validation holds: from
        # here a refusal is interruption, and the journal gates control.
        ticket = self._journal("operation_ticket", request_id)
        ticket = self._journal("reopen_restart", ticket)
        self._begin_backend_operation(ticket)
        # Arm the fence before the first primary-control commitment; sync the
        # fence and the consumed approval before any control action.
        self._journal("arm_mutation", ticket)
        consumed = self.approvals / (request_id + ".consumed")
        try:
            os.replace(approval, consumed)
            fd = os.open(self.approvals, os.O_RDONLY)
            try:
                _sync_fd(fd)
            finally:
                os.close(fd)
        except OSError:
            raise Fault("state_io") from None
        self._phase(plan, "intent_consumed")
        ticket = self._journal("phase", ticket, "intent_consumed")
        committed = False
        try:
            self._phase(plan, "stopping")
            permit = self._commit_control("service-stop")
            ticket = self._journal("phase", ticket, "stopping",
                                   committed_action=(self.shutdown, permit))
            committed = True
            self._admit()
            self._call("qualify", plan)
            wall, mono = self._time()
            self._approval(plan, wall, mono, consumed=True)
            self._call("stop")
            self._phase(plan, "starting")
            ticket = self._journal("phase", ticket, "starting")
            self._call("start")
        except Fault as exc:
            return self._manual(plan, ticket, exc.code)
        try:
            self._phase(plan, "postchecking")
            ticket = self._journal("phase", ticket, "postchecking")
            accepted = self._accept_started(plan["original_identity"])
            plan["started"] = accepted
            self._phase(plan, "restarted")
            self._finish_accepted(ticket, plan, "restarted")
            return _result("restarted", request_id=request_id, checks=accepted.get("checks", {}), scope=plan["scope"])
        except Fault as exc:
            if exc.code in {"service_control_denied", "service_control_timeout", "state_io"}:
                return self._manual(plan, ticket, exc.code)
        try:
            self._phase(plan, "rollback_stopping")
            if not committed:
                permit = self._commit_control("service-stop")
                ticket = self._journal("phase", ticket, "rollback_stopping",
                                       committed_action=(self.shutdown, permit))
            else:
                ticket = self._journal("phase", ticket, "rollback_stopping")
            self._call("stop")
            self._phase(plan, "rollback_restoring")
            ticket = self._journal("phase", ticket, "rollback_restoring")
            self._call("restore_lkg", snapshot_path, plan["lkg_binding"])
            self._phase(plan, "rollback_starting")
            ticket = self._journal("phase", ticket, "rollback_starting")
            self._call("start")
            self._phase(plan, "rollback_postchecking")
            ticket = self._journal("phase", ticket, "rollback_postchecking")
            accepted = self._accept_started(plan["original_identity"])
            plan["rollback"] = accepted
            self._phase(plan, "rolled_back")
            self._finish_accepted(ticket, plan, "rolled_back")
            return _result("rolled_back", request_id=request_id, checks=accepted.get("checks", {}), scope=plan["scope"])
        except Fault as exc:
            return self._manual(plan, ticket, exc.code)

    def _commit_control(self, purpose):
        """One-permit commitment for the first primary control action."""
        from .shutdown import ShutdownRequested
        try:
            return self.shutdown.commit(purpose)
        except ShutdownRequested:
            raise Fault("controller_stopping") from None

    def _finish_accepted(self, ticket, plan, terminal):
        """Close the journaled restart and clear the fence after acceptance.

        RecoveryStore.phase() rejects terminal phases by design; the accepted
        terminal transition is finish()'s alone: it rechecks the complete
        engine request, the consumed approval and closed children before
        clearing the fence.
        """
        plan["phase"] = terminal
        self._record(plan)
        self._journal("finish", ticket)

    def handle(self, request):
        if not isinstance(request, dict) or type(request.get("op")) is not str:
            return _result("blocked", "invalid_request")
        op = request["op"]
        if op in {"status", "prepare"}:
            valid = set(request) == {"op"}
        elif op == "restart":
            valid = (set(request) == {"op", "request_id", "asset_digest"}
                     and type(request["request_id"]) is str and bool(_ID.fullmatch(request["request_id"]))
                     and type(request["asset_digest"]) is str and bool(_DIGEST.fullmatch(request["asset_digest"])))
        else:
            valid = False
        if not valid:
            return _result("blocked", "invalid_request")
        try:
            if op == "status":
                return self._status()
            if self._recovery_invalid:
                raise Fault("recovery_invalid")
            with self._locked():
                from .shutdown import ShutdownRequested
                try:
                    self.shutdown.checkpoint()
                except ShutdownRequested:
                    raise Fault("controller_stopping") from None
                try:
                    self.recovery.require_clear()
                except Exception:
                    raise Fault("recovery_invalid") from None
                try:
                    return self._prepare() if op == "prepare" else self._restart(request)
                finally:
                    with contextlib.suppress(Exception):
                        self.backend.release_credentials()
        except Fault as exc:
            return _result("blocked", exc.code)
        except Exception:
            return _result("blocked", "internal_error")
