"""Disposable fixtures for the restart state machine; no production backend."""

import json
import errno
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core.restart_control.engine import Controller, Fault, approval_binding, atomic_json, private_dir
from core.restart_control.wire import _serve_one, client, serve


D = "a" * 64


class FixtureBackend:
    label = "fixture.proxy"
    fixture = True

    def __init__(self):
        self.calls = []
        self.fail = None
        self.managed = True
        self.identity = {"pid": 123, "uid": os.getuid(), "birth": [1, 2]}

    def call(self, name, value=None):
        self.calls.append(name)
        if self.fail == name:
            raise Fault("service_control_denied" if name in {"stop", "start"} else "postcheck_failed")
        return value

    def inspect(self):
        return self.call("inspect", {"identity": self.identity.copy(), "managed": self.managed})

    def freeze(self):
        return self.call("freeze", dict.fromkeys(("asset_digest", "runtime_digest", "policy_digest", "controller_digest"), D))

    def check_gates(self):
        self.call("check_gates")

    def accept_primary(self, identity):
        return self.call("accept_primary", {"fixture": True})

    def rehearse(self):
        return self.call("rehearse", {"fixture": True, "cleaned": True})

    def snapshot_lkg(self, path):
        return self.call("snapshot_lkg", {"asset_digest": D, "runtime_digest": D})

    def validate_lkg(self, path, binding):
        self.call("validate_lkg")

    def qualify(self, plan):
        self.call("qualify")

    def stop(self):
        self.call("stop")

    def start(self):
        self.call("start")
        self.identity["pid"] += 1

    def accept_started(self):
        return self.call("accept_started", {"identity": self.identity.copy(), "checks": {"fixture": True}})

    def restore_lkg(self, path, binding):
        self.call("restore_lkg")

    def release_credentials(self):
        self.calls.append("release_credentials")


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backend = FixtureBackend()
        self.now = 1000.0
        self.mono = 500.0
        self.controller = Controller(self.root / "state", self.backend,
                                     clock=lambda: self.now, monotonic=lambda: self.mono)

    def prepare(self):
        result = self.controller.handle({"op": "prepare"})
        self.assertEqual(result["status"], "prepared", result)
        return result

    def approve(self, plan):
        approval = {"version": 1, "owner_uid": os.getuid(), **approval_binding(plan),
                    "issued_at": self.now, "expires_at": self.now + 100,
                    "issued_monotonic": self.mono, "expires_monotonic": self.mono + 100}
        atomic_json(self.root / "state" / "approvals" / (plan["request_id"] + ".json"), approval)

    def restart(self, plan):
        return self.controller.handle({"op": "restart", "request_id": plan["request_id"],
                                       "asset_digest": plan["asset_digest"]})

    def test_exact_schema_and_no_control(self):
        for value in ({"op": "prepare", "extra": 1}, {"op": "restart", "request_id": "x", "asset_digest": D},
                      {"op": "approve"}, [], {"op": True}):
            self.assertEqual(self.controller.handle(value)["code"], "invalid_request")
        self.assertEqual(self.backend.calls, [])

    def test_approval_required_then_success_and_replay(self):
        plan = self.prepare()
        self.assertEqual(self.restart(plan)["code"], "approval_missing")
        self.assertNotIn("stop", self.backend.calls)
        self.approve(plan)
        self.assertEqual(self.restart(plan)["status"], "restarted")
        self.assertEqual(self.backend.calls.count("stop"), 1)
        self.assertEqual(self.restart(plan)["code"], "replayed")
        self.assertEqual(self.backend.calls.count("stop"), 1)

    def test_epoch_expiry_and_unmanaged(self):
        plan = self.prepare()
        self.approve(plan)
        other = Controller(self.root / "state", self.backend, clock=lambda: self.now,
                           monotonic=lambda: self.mono)
        self.assertEqual(other.handle({"op": "restart", "request_id": plan["request_id"],
                                       "asset_digest": D})["code"], "plan_stale")
        self.mono += 301
        self.assertEqual(self.restart(plan)["code"], "plan_stale")
        self.backend.managed = False
        self.assertEqual(self.controller.handle({"op": "prepare"})["code"], "unmanaged_listener")
        self.assertNotIn("stop", self.backend.calls)

    def test_control_refusal_and_rollback(self):
        plan = self.prepare()
        self.approve(plan)
        self.backend.fail = "start"
        self.assertEqual(self.restart(plan)["status"], "manual_recovery_required")
        self.assertEqual(self.backend.calls.count("stop"), 1)
        self.assertEqual(self.backend.calls.count("start"), 1)
        self.assertNotIn("restore_lkg", self.backend.calls)

        self.backend.fail = None
        plan = self.prepare()
        self.approve(plan)
        original = self.backend.accept_started
        attempts = [0]
        def fail_once():
            attempts[0] += 1
            if attempts[0] == 1:
                raise Fault("postcheck_failed")
            return original()
        self.backend.accept_started = fail_once
        self.assertEqual(self.restart(plan)["status"], "rolled_back")
        self.assertEqual(attempts[0], 2)

    def test_preflight_failure_never_controls(self):
        for name in ("freeze", "check_gates", "accept_primary", "rehearse", "snapshot_lkg"):
            with self.subTest(name=name):
                self.backend.calls.clear()
                self.backend.fail = name
                result = self.controller.handle({"op": "prepare"})
                self.assertEqual(result["status"], "blocked")
                self.assertNotIn("stop", self.backend.calls)
                records = list((self.root / "state" / "requests").glob("*.json"))
                self.assertTrue(any(json.loads(p.read_text()).get("original_preserved") for p in records))
        self.backend.fail = None

    def test_approval_schema_and_freshness(self):
        plan = self.prepare()
        path = self.root / "state" / "approvals" / (plan["request_id"] + ".json")
        self.approve(plan)
        approval = json.loads(path.read_text())
        variants = [dict(approval, owner_uid=True), dict(approval, issued_at=self.now + 1),
                    dict(approval, extra=True), dict(approval, asset_digest="b" * 64),
                    dict(approval, expires_at=self.now + 301)]
        for value in variants:
            with self.subTest(value=value):
                atomic_json(path, value)
                self.assertIn(self.restart(plan)["code"], {"approval_invalid", "approval_stale"})
                self.assertNotIn("stop", self.backend.calls)
        path.write_text('{"version":1,"version":1}')
        self.assertEqual(self.restart(plan)["code"], "approval_invalid")
        self.assertNotIn("stop", self.backend.calls)

    def test_consumed_marker_and_unknown_request(self):
        plan = self.prepare()
        self.approve(plan)
        approval = self.root / "state" / "approvals" / (plan["request_id"] + ".json")
        approval.rename(approval.with_suffix(".consumed"))
        self.assertEqual(self.restart(plan)["code"], "replayed")
        self.assertNotIn("stop", self.backend.calls)
        self.assertEqual(self.controller.handle({"op": "restart", "request_id": "f" * 32,
                                                 "asset_digest": D})["code"], "unknown_request")

    def test_rollback_refusal_stops_sequence(self):
        plan = self.prepare()
        self.approve(plan)
        self.backend.accept_started = lambda: (_ for _ in ()).throw(Fault("postcheck_failed"))
        original = self.backend.stop
        count = [0]
        def stop_twice():
            count[0] += 1
            if count[0] == 2:
                raise Fault("service_control_denied")
            original()
        self.backend.stop = stop_twice
        self.assertEqual(self.restart(plan)["status"], "manual_recovery_required")
        self.assertEqual(count[0], 2)
        self.assertNotIn("restore_lkg", self.backend.calls)

    def test_socket_unavailable(self):
        with self.assertRaises(Fault) as caught:
            client(self.root / "absent.sock", {"op": "status"}, timeout=.1)
        self.assertEqual(caught.exception.code, "controller_unavailable")

    def test_socketpair_dispatch_and_bounds(self):
        for raw, code in ((b'{"op":"status"}\n', None),
                          (b'{"op":"status","op":"prepare"}\n', "invalid_request"),
                          (b'{"op":"status","x":NaN}\n', "invalid_request"),
                          (b"x" * 65537 + b"\n", "invalid_request")):
            with self.subTest(code=code):
                left, right = socket.socketpair()
                thread = threading.Thread(target=_serve_one, args=(right, self.controller), daemon=True)
                thread.start()
                with left:
                    left.sendall(raw)
                    answer = json.loads(left.recv(4096))
                thread.join(2)
                self.assertEqual(answer.get("code"), code)
        self.assertNotIn("rehearse", self.backend.calls)

    def test_status_during_prepare_and_busy_mutation(self):
        entered, release = threading.Event(), threading.Event()
        original = self.backend.rehearse
        def slow_rehearsal():
            entered.set()
            release.wait(2)
            return original()
        self.backend.rehearse = slow_rehearsal
        thread = threading.Thread(target=lambda: self.controller.handle({"op": "prepare"}), daemon=True)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.controller.handle({"op": "status"})["status"], "ok")
        self.assertEqual(self.controller.handle({"op": "prepare"})["code"], "busy")
        release.set()
        thread.join(2)

    def test_wrong_uid_and_identity_drift(self):
        self.backend.identity["uid"] = os.getuid() + 1
        self.assertEqual(self.controller.handle({"op": "prepare"})["code"], "primary_changed")
        self.assertNotIn("stop", self.backend.calls)
        self.backend.identity["uid"] = os.getuid()
        plan = self.prepare()
        self.approve(plan)
        self.backend.identity["birth"] = [1, 3]
        self.assertEqual(self.restart(plan)["code"], "primary_changed")
        self.assertNotIn("stop", self.backend.calls)

    def test_clock_regression_invalidates_epoch(self):
        plan = self.prepare()
        self.approve(plan)
        self.now -= 1
        self.assertEqual(self.restart(plan)["code"], "clock_invalid")
        self.now += 1
        self.assertEqual(self.restart(plan)["code"], "plan_stale")
        self.assertNotIn("stop", self.backend.calls)

    def test_stop_refusal_never_starts(self):
        plan = self.prepare()
        self.approve(plan)
        self.backend.fail = "stop"
        self.assertEqual(self.restart(plan)["status"], "manual_recovery_required")
        self.assertNotIn("start", self.backend.calls)
        self.assertEqual(self.restart(plan)["code"], "replayed")

    def test_owner_uid_guard_prevents_service_control(self):
        plan = self.prepare()
        self.approve(plan)
        path = self.root/'state'/'approvals'/(plan['request_id']+'.json')
        approval = json.loads(path.read_text())
        approval['owner_uid'] = os.getuid()+1
        atomic_json(path, approval)
        result = self.restart(plan)
        self.assertNotIn('stop', self.backend.calls, 'UNAUTHORIZED_CONTROL_CALL')
        self.assertEqual(result.get('code'), 'approval_invalid')

    def test_birth_identity_guard_prevents_service_control(self):
        plan = self.prepare()
        self.approve(plan)
        self.backend.identity['birth'] = [10, 11]
        result = self.restart(plan)
        self.assertNotIn('stop', self.backend.calls, 'UNAUTHORIZED_CONTROL_CALL')
        self.assertEqual(result.get('code'), 'primary_changed')

    def test_private_state_and_sync_failure(self):
        bad = self.root / "bad"
        bad.mkdir(mode=0o755)
        with self.assertRaises(Fault):
            private_dir(bad)
        link = self.root / "link"
        link.symlink_to(self.root / "state")
        with self.assertRaises(Fault):
            private_dir(link)
        target = self.root / "state" / "requests" / "sync.json"
        with mock.patch("core.restart_control.engine._sync_fd", side_effect=OSError("fixture sync failure")):
            with self.assertRaises(Fault) as caught:
                atomic_json(target, {"fixture": True})
        self.assertEqual(caught.exception.code, "state_io")
        self.assertFalse(target.exists())

    def test_socket_dispatch_and_bad_json(self):
        short = tempfile.TemporaryDirectory(prefix='rc-', dir=os.environ['TMPDIR'])
        self.addCleanup(short.cleanup)
        path = Path(short.name) / "controller.sock"
        try:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.bind(str(path))
            path.unlink()
        except OSError as exc:
            if exc.errno == errno.EPERM:
                self.skipTest("sandbox denies Unix socket bind")
            raise
        stop = threading.Event()
        thread = threading.Thread(target=serve, args=(path, self.controller, stop), daemon=True)
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(2)))
        for _ in range(100):
            if path.exists():
                break
            time.sleep(.01)
        self.assertEqual(client(path, {"op": "status"})["status"], "ok")
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(str(path))
            sock.sendall(b'{"op":"status","op":"prepare"}\n')
            self.assertEqual(json.loads(sock.recv(4096))["code"], "invalid_request")
        self.assertNotIn("rehearse", self.backend.calls)


if __name__ == "__main__":
    unittest.main()
