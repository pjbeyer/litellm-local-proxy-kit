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


CODES = frozenset("invalid_request busy state_io approval_missing approval_invalid approval_stale plan_stale unknown_request replayed interrupted clock_invalid unmanaged_listener primary_changed gate_failed candidate_failed candidate_cleanup_failed lkg_invalid runtime_invalid qualification_missing qualification_invalid service_control_denied service_control_timeout postcheck_failed rollback_failed internal_error controller_unavailable foreign_peer socket_exists".split())
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


def canonical_digest(obj):
    try:
        data = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise Fault("invalid_request") from None
    return hashlib.sha256(data).hexdigest()


def _sync_fd(fd):
    if _SYNC is not None:
        fcntl.fcntl(fd, _SYNC)
    else:
        os.fsync(fd)


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


class Controller:
    def __init__(self, state_dir, backend, uid=None, clock=time.time, monotonic=time.monotonic):
        self.state_dir = private_dir(state_dir)
        self.requests = private_dir(self.state_dir / "requests")
        self.approvals = private_dir(self.state_dir / "approvals")
        self.snapshots = private_dir(self.state_dir / "snapshots")
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
        try:
            fd = os.open(self._lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        except OSError:
            raise Fault("state_io") from None
        else:
            os.close(fd)
        _private_path(self._lock_path, False)

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

    def _prepare(self):
        wall, mono = self._time()
        request_id = uuid.uuid4().hex
        before = None
        plan = {"request_id": request_id, "phase": "preparing", "controller_epoch": self.epoch,
                "scope": "fixture" if getattr(self.backend, "fixture", False) else "production",
                "shared_state_side_effects": not getattr(self.backend, "fixture", False)}
        self._record(plan)
        try:
            before = self._inspect()
            if before["identity"]["uid"] != self.uid:
                raise Fault("primary_changed")
            frozen = self._freeze()
            self._call("check_gates")
            primary_checks = self._call("accept_primary", before["identity"])
            candidate_checks = self._call("rehearse")
            if not isinstance(primary_checks, dict) or not isinstance(candidate_checks, dict):
                raise Fault("candidate_failed")
            binding = {"asset_digest": frozen["asset_digest"], "runtime_digest": frozen["runtime_digest"]}
            snapshot_path = self.snapshots / request_id
            private_dir(snapshot_path)
            lkg_binding = self._call("snapshot_lkg", snapshot_path)
            if lkg_binding != binding:
                raise Fault("lkg_invalid")
            after = self._inspect()
            if after != before:
                raise Fault("primary_changed")
            if self._freeze() != frozen:
                raise Fault("plan_stale")
            if not before["managed"]:
                raise Fault("unmanaged_listener")
            wall, mono = self._time()
            plan.update({"phase": "prepared", "uid": self.uid, "label": self.backend.label,
                         "original_identity": before["identity"], **frozen,
                         "candidate_binding": binding, "lkg_binding": lkg_binding,
                         "primary_checks": primary_checks, "candidate_checks": candidate_checks,
                         "prepared_at": wall, "prepared_monotonic": mono,
                         "expires_at": wall + 300, "expires_monotonic": mono + 300})
            self._record(plan)
            return _result("prepared", **plan)
        except Fault as exc:
            if before is not None:
                try:
                    plan["original_preserved"] = self._inspect()["identity"] == before["identity"]
                except Fault:
                    plan["original_preserved"] = False
            plan.update(phase="blocked", code=exc.code)
            self._record(plan)
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

    def _manual(self, plan, code):
        failed_phase = plan["phase"]
        plan.update(code=code, failed_phase=failed_phase)
        # A failed durable write cannot authorize another service-control call.
        # Preserve the previous on-disk uncertainty and report manual recovery.
        with contextlib.suppress(Fault):
            self._phase(plan, "manual_recovery_required")
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
        self._call("qualify", plan)
        approval = self._approval(plan, wall, mono)
        if self._freeze() != {k: plan[k] for k in ("asset_digest", "runtime_digest", "policy_digest", "controller_digest")}:
            raise Fault("plan_stale")
        current = self._inspect()
        if current["identity"] != plan["original_identity"] or not current["managed"]:
            raise Fault("primary_changed")
        self._call("check_gates")
        snapshot_path = self.snapshots / request_id
        self._call("validate_lkg", snapshot_path, plan["lkg_binding"])
        self._call("accept_primary", current["identity"])
        wall, mono = self._time()
        self._approval(plan, wall, mono)
        if self._freeze() != {k: plan[k] for k in ("asset_digest", "runtime_digest", "policy_digest", "controller_digest")} or self._inspect() != current:
            raise Fault("plan_stale")
        self._call("qualify", plan)
        wall, mono = self._time()
        self._approval(plan, wall, mono)
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
        try:
            self._phase(plan, "stopping")
            self._call("qualify", plan)
            wall, mono = self._time()
            self._approval(plan, wall, mono, consumed=True)
            self._call("stop")
            self._phase(plan, "starting")
            self._call("start")
        except Fault as exc:
            return self._manual(plan, exc.code)
        try:
            self._phase(plan, "postchecking")
            accepted = self._accept_started(plan["original_identity"])
            plan["started"] = accepted
            self._phase(plan, "restarted")
            return _result("restarted", request_id=request_id, checks=accepted.get("checks", {}), scope=plan["scope"])
        except Fault as exc:
            if exc.code in {"service_control_denied", "service_control_timeout", "state_io"}:
                return self._manual(plan, exc.code)
        try:
            self._phase(plan, "rollback_stopping")
            self._call("stop")
            self._phase(plan, "rollback_restoring")
            self._call("restore_lkg", snapshot_path, plan["lkg_binding"])
            self._phase(plan, "rollback_starting")
            self._call("start")
            self._phase(plan, "rollback_postchecking")
            accepted = self._accept_started(plan["original_identity"])
            plan["rollback"] = accepted
            self._phase(plan, "rolled_back")
            return _result("rolled_back", request_id=request_id, checks=accepted.get("checks", {}), scope=plan["scope"])
        except Fault as exc:
            return self._manual(plan, exc.code)

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
            with self._locked():
                try:
                    return self._prepare() if op == "prepare" else self._restart(request)
                finally:
                    with contextlib.suppress(Exception):
                        self.backend.release_credentials()
        except Fault as exc:
            return _result("blocked", exc.code)
        except Exception:
            return _result("blocked", "internal_error")
