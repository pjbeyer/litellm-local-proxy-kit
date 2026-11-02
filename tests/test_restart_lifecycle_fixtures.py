"""Genuine disposable-fixture lifecycle regressions for the restart controller.

FIXTURE SCOPE ONLY. Every child process here is a throwaway stdlib HTTP server
spawned under a private fixture root below TMPDIR, observed over real loopback
HTTP and through the real darwin process-identity ABI. The launchctl backend is
an explicit fake that only ever starts/stops its own retained fixture children
(graceful stdin control); it never runs launchctl and never signals any process
it did not spawn. No result in this module represents live launchd or LiteLLM
acceptance: every controller lifecycle result is asserted to carry
scope="fixture", and every HTTP acceptance body is labelled with fixture flags.
No production proxy, LaunchAgent, or credential is touched.
"""

import contextlib
import errno
import hashlib
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.restart_control import engine as engine_module
from core.restart_control import process_identity
from core.restart_control.engine import Controller, Fault, approval_binding, atomic_json
from core.restart_control.wire import client, serve


# Synthetic, non-secret fixture generation digests (fixture-scope only).
FIXTURE_ASSET_DIGEST = hashlib.sha256(b"fixture-asset-generation-v1").hexdigest()
FIXTURE_RUNTIME_DIGEST = hashlib.sha256(b"fixture-runtime-generation-v1").hexdigest()
FIXTURE_FREEZE = {
    "asset_digest": FIXTURE_ASSET_DIGEST,
    "runtime_digest": FIXTURE_RUNTIME_DIGEST,
    "policy_digest": hashlib.sha256(b"fixture-policy-v1").hexdigest(),
    "controller_digest": hashlib.sha256(b"fixture-controller-v1").hexdigest(),
}
FIXTURE_BINDING = {"asset_digest": FIXTURE_ASSET_DIGEST, "runtime_digest": FIXTURE_RUNTIME_DIGEST}


def _lkg_bytes(binding):
    return b"fixture-lkg-config:" + binding["asset_digest"].encode() + b":" + binding["runtime_digest"].encode()


CHILD_SOURCE = '''\
"""Disposable fixture HTTP child; never a real proxy backend."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = sys.argv[1] if len(sys.argv) > 1 else "ok"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"fixture": True, "mode": MODE})
        if self.path == "/ready":
            if MODE == "candidate-bad":
                return self._json(503, {"fixture": True, "ready": False})
            return self._json(200, {"fixture": True, "ready": True})
        if self.path == "/v1/tiny":
            if MODE == "bad-acceptance":
                return self._json(401, {"fixture": True, "error": "fixture-invalid-key"})
            return self._json(200, {"fixture": True, "completion": "fixture-ok"})
        return self._json(404, {"fixture": True})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    sys.stdout.write("READY %d\\n" % port)
    sys.stdout.flush()

    def control():
        while True:
            line = sys.stdin.readline()
            if not line or line.strip() == "shutdown":
                threading.Thread(target=server.shutdown).start()
                return

    threading.Thread(target=control, daemon=True).start()
    server.serve_forever()
    server.server_close()
    sys.stdout.write("EXITED\\n")
    sys.stdout.flush()


main()
'''


def _fixture_base():
    """Real-path directory under TMPDIR; resolving avoids symlinked ancestors."""
    raw = os.environ.get("TMPDIR") or tempfile.gettempdir()
    base = Path(raw).resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


class Child:
    """A retained fixture child with its genuine loopback port."""

    def __init__(self, proc, port, mode):
        self.proc, self.port, self.mode = proc, port, mode
        self.stopped = False

    def alive(self):
        return self.proc.poll() is None


def _spawn(root, script, mode):
    proc = subprocess.Popen(
        [sys.executable, str(script), mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(root))
    deadline = time.monotonic() + 20
    line = b""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError("fixture child exited at startup: "
                                 + proc.stderr.read().decode("utf-8", "replace"))
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if ready:
            line = proc.stdout.readline()
            break
    if not line.startswith(b"READY "):
        with contextlib.suppress(Exception):
            proc.terminate()
        raise AssertionError("fixture child did not report readiness")
    return Child(proc, int(line.split()[1]), mode)


def _graceful(child, grace=5.0):
    """Graceful stdin-controlled shutdown of one retained fixture child only."""
    if child.proc.poll() is None and not child.stopped:
        child.stopped = True
        with contextlib.suppress(OSError, ValueError):
            child.proc.stdin.write(b"shutdown\n")
            child.proc.stdin.flush()
        with contextlib.suppress(OSError, ValueError):
            child.proc.stdin.close()
        try:
            child.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            raise AssertionError("fixture child ignored stdin shutdown")
    code = child.proc.poll()
    with contextlib.suppress(OSError, ValueError):
        child.proc.stdout.read()
    with contextlib.suppress(OSError, ValueError):
        child.proc.stderr.read()
    return code


def _http_get(port, path, timeout=3):
    """Genuine loopback HTTP GET to a fixture child; no redirects involved."""
    url = "http://127.0.0.1:%d%s" % (port, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        with contextlib.suppress(ValueError):
            return exc.code, json.loads(exc.read().decode("utf-8"))
        return exc.code, None
    except (urllib.error.URLError, OSError, TimeoutError):
        return None, None


class FakeLaunchctlBackend:
    """Explicitly fake launchctl backend owning only retained fixture children.

    All start/stop actions are stdin-controlled Popen operations on children this
    backend itself spawned and retained. Refusals take no process action at all.
    Nothing here invokes the real launchctl or any signal primitive.
    """

    label = "fixture.local.proxy"
    fixture = True

    def __init__(self, root, script):
        self.root, self.script = root, script
        self.calls = []
        self.children = []            # every retained fixture child
        self.service_child = None     # the child acting as the managed service
        self.limits = {}               # verb -> allowed count; beyond: refusal
        self.next_modes = []           # per-start modes, default "ok"
        self.candidate_mode = "ok"
        self.birth_usec_delta = 0
        self.qualify_hooks = {}        # qualify ordinal -> callable
        self.restored = None

    def _note(self, name):
        self.calls.append(name)

    def _refused(self, name):
        limit = self.limits.get(name)
        return limit is not None and self.calls.count(name) > limit

    def _identity(self, child):
        info = process_identity.process_identity(child.proc.pid)
        birth = [info["birth"][0], info["birth"][1] + self.birth_usec_delta]
        return {"pid": info["pid"], "uid": info["uid"], "birth": birth}

    def inspect(self):
        self._note("inspect")
        child = self.service_child
        if child is None or not child.alive():
            raise Fault("unmanaged_listener")
        return {"identity": self._identity(child), "managed": True}

    def freeze(self):
        self._note("freeze")
        return dict(FIXTURE_FREEZE)

    def check_gates(self):
        self._note("check_gates")

    def accept_primary(self, identity):
        self._note("accept_primary")
        status, body = _http_get(self.service_child.port, "/health")
        if status != 200 or not body.get("fixture"):
            raise Fault("postcheck_failed")
        status, body = _http_get(self.service_child.port, "/v1/tiny")
        if status != 200 or body.get("completion") != "fixture-ok":
            raise Fault("postcheck_failed")
        return {"fixture": True, "http": True, "primary": True}

    def rehearse(self):
        self._note("rehearse")
        candidate = _spawn(self.root, self.script, self.candidate_mode)
        self.children.append(candidate)
        try:
            status, _ = _http_get(candidate.port, "/ready")
            if status != 200:
                raise Fault("candidate_failed")
            status, body = _http_get(candidate.port, "/v1/tiny")
            if status != 200 or not body.get("fixture"):
                raise Fault("candidate_failed")
            return {"fixture": True, "candidate": True, "cleaned": True}
        finally:
            _graceful(candidate)

    def snapshot_lkg(self, path):
        self._note("snapshot_lkg")
        target = Path(path) / "lkg.fixture.conf"
        target.write_bytes(_lkg_bytes(FIXTURE_BINDING))
        os.chmod(target, 0o600)
        return dict(FIXTURE_BINDING)

    def validate_lkg(self, path, binding):
        self._note("validate_lkg")
        blob = (Path(path) / "lkg.fixture.conf").read_bytes()
        if blob != _lkg_bytes(binding) or binding != FIXTURE_BINDING:
            raise Fault("lkg_invalid")

    def restore_lkg(self, path, binding):
        self._note("restore_lkg")
        if self._refused("restore_lkg"):
            raise Fault("service_control_denied")
        self.validate_lkg(path, binding)
        self.restored = _lkg_bytes(FIXTURE_BINDING)

    def qualify(self, plan):
        ordinal = self.calls.count("qualify") + 1
        self._note("qualify")
        hook = self.qualify_hooks.get(ordinal)
        if hook is not None:
            hook()

    def stop(self):
        self._note("stop")
        if self._refused("stop"):
            raise Fault("service_control_denied")
        child = self.service_child
        if child is None or not child.alive():
            raise Fault("service_control_denied")
        code = _graceful(child)
        if code != 0:
            raise Fault("service_control_timeout")
        self.service_child = None

    def start(self):
        self._note("start")
        if self._refused("start"):
            raise Fault("service_control_denied")
        mode = self.next_modes.pop(0) if self.next_modes else "ok"
        child = _spawn(self.root, self.script, mode)
        self.children.append(child)
        self.service_child = child

    def accept_started(self):
        self._note("accept_started")
        child = self.service_child
        if child is None or not child.alive():
            raise Fault("postcheck_failed")
        status, body = _http_get(child.port, "/health")
        if status != 200 or not body.get("fixture"):
            raise Fault("postcheck_failed")
        status, body = _http_get(child.port, "/v1/tiny")
        if status != 200 or body.get("completion") != "fixture-ok":
            # Genuine fixture 401 invalid-key denial observed over loopback HTTP.
            raise Fault("postcheck_failed")
        return {"identity": self._identity(child),
                "checks": {"fixture": True, "http": True, "mode": child.mode}}

    def release_credentials(self):
        self._note("release_credentials")


class FixtureRig:
    """Per-test fixture world: real UDS transport, real HTTP children, fake launchctl."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="rc-fixture-", dir=str(_fixture_base())))
        self.script = self.root / "fixture_child.py"
        self.script.write_text(CHILD_SOURCE)
        self.wall = [1000.0]
        self.mono = [500.0]
        self.backend = FakeLaunchctlBackend(self.root, self.script)
        self.state = self.root / "state"
        self.controller = Controller(self.state, self.backend,
                                     clock=lambda: self.wall[0],
                                     monotonic=lambda: self.mono[0])
        self.primary = _spawn(self.root, self.script, "ok")
        self.backend.children.append(self.primary)
        self.backend.service_child = self.primary
        self.sock = self.root / "controller.sock"
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=serve,
                                       args=(self.sock, self.controller, self.stop_event),
                                       daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.sock.exists():
                break
            time.sleep(0.02)
        if not self.sock.exists():
            raise AssertionError("fixture controller socket never appeared")

    # -- transport ---------------------------------------------------------
    def op(self, request):
        return client(self.sock, request)

    def prepare(self):
        result = self.op({"op": "prepare"})
        assert result.get("status") == "prepared", result
        return result

    def restart(self, plan):
        return self.op({"op": "restart", "request_id": plan["request_id"],
                        "asset_digest": plan["asset_digest"]})

    # -- owner approval lives only in fixture private state ---------------
    def approve(self, plan, lifetime=100.0):
        approval = {"version": 1, "owner_uid": os.getuid(), **approval_binding(plan),
                    "issued_at": self.wall[0], "expires_at": self.wall[0] + lifetime,
                    "issued_monotonic": self.mono[0],
                    "expires_monotonic": self.mono[0] + lifetime}
        atomic_json(self.state / "approvals" / (plan["request_id"] + ".json"), approval)

    # -- durable fixture records ------------------------------------------
    def record(self, plan):
        return json.loads((self.state / "requests" / (plan["request_id"] + ".json")).read_text())

    def consumed(self, plan):
        return (self.state / "approvals" / (plan["request_id"] + ".consumed")).exists()

    def approval_file(self, plan):
        return (self.state / "approvals" / (plan["request_id"] + ".json")).exists()

    # -- teardown ----------------------------------------------------------
    def close(self):
        self.stop_event.set()
        self.thread.join(5)
        for child in list(self.backend.children):
            if child.alive():
                with contextlib.suppress(AssertionError, OSError, ValueError):
                    _graceful(child, grace=2)
            if child.alive():
                with contextlib.suppress(OSError, ValueError):
                    child.proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    child.proc.wait(timeout=2)
            for stream in (child.proc.stdin, child.proc.stdout, child.proc.stderr):
                with contextlib.suppress(OSError, ValueError):
                    stream.close()


@unittest.skipIf(sys.platform != "darwin",
                 "genuine birth/PID observation requires the darwin process ABI")
class RestartLifecycleFixtureTest(unittest.TestCase):
    """Fixture-scope lifecycle regressions: real HTTP children, real UDS, fake launchctl."""

    def setUp(self):
        probe = _fixture_base() / ("probe-%s.sock" % uuid.uuid4().hex)
        try:
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(probe))
        except OSError as exc:
            if exc.errno == errno.EPERM:
                raise unittest.SkipTest("sandbox denies Unix socket bind") from None
            raise
        finally:
            with contextlib.suppress(OSError):
                probe.unlink()
        self.rig = FixtureRig()
        self.addCleanup(self.rig.close)

    # -- helpers -----------------------------------------------------------
    def assert_no_service_control(self):
        for verb in ("stop", "start", "restore_lkg"):
            self.assertNotIn(verb, self.rig.backend.calls,
                             "service control ran: %r" % self.rig.backend.calls)

    def assert_fixture_labelled(self, result):
        self.assertEqual(result.get("scope"), "fixture", result)

    # -- required coverage -------------------------------------------------
    def test_fixture_successful_restart_over_real_transport(self):
        rig = self.rig
        plan = rig.prepare()
        self.assert_fixture_labelled(plan)
        self.assertEqual(rig.op({"op": "status"})["status"], "ok")
        rig.approve(plan)
        old = rig.backend.service_child
        old_identity = process_identity.process_identity(old.proc.pid)

        result = rig.restart(plan)
        self.assertEqual(result["status"], "restarted", result)
        self.assert_fixture_labelled(result)
        self.assertEqual(result["checks"]["fixture"], True)
        self.assertEqual(result["checks"]["http"], True)

        # Genuine stop of the retained fixture primary; genuine new child.
        self.assertEqual(old.proc.poll(), 0)
        new = rig.backend.service_child
        self.assertIsNot(new, old)
        self.assertTrue(new.alive())
        new_identity = process_identity.process_identity(new.proc.pid)
        self.assertNotEqual(new_identity["pid"], old_identity["pid"])
        self.assertNotEqual(new_identity["birth"], old_identity["birth"])
        self.assertEqual(new_identity["pid"], result and rig.record(plan)["started"]["identity"]["pid"])

        # Real loopback acceptance of the restarted fixture child.
        status, body = _http_get(new.port, "/health")
        self.assertEqual((status, body.get("fixture")), (200, True))
        status, body = _http_get(new.port, "/v1/tiny")
        self.assertEqual((status, body.get("completion")), (200, "fixture-ok"))

        self.assertEqual(rig.backend.calls.count("stop"), 1)
        self.assertEqual(rig.backend.calls.count("start"), 1)
        record = rig.record(plan)
        self.assertEqual(record["phase"], "restarted")
        self.assertEqual(record["scope"], "fixture")

        # Duplicate/replay never repeats service control.
        self.assertEqual(rig.restart(plan)["code"], "replayed")
        self.assertEqual(rig.backend.calls.count("stop"), 1)
        self.assertEqual(rig.backend.calls.count("start"), 1)
        self.assertTrue(rig.consumed(plan))

    def test_fixture_failed_postcheck_verified_rollback(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        old = rig.backend.service_child
        rig.backend.next_modes = ["bad-acceptance"]

        result = rig.restart(plan)
        self.assertEqual(result["status"], "rolled_back", result)
        self.assert_fixture_labelled(result)
        self.assertEqual(result["checks"]["fixture"], True)

        # The failing started child was genuinely stopped; rollback child is live.
        self.assertEqual(old.proc.poll(), 0)
        rolled = rig.backend.service_child
        self.assertTrue(rolled.alive())
        self.assertNotEqual(rolled.proc.pid, old.proc.pid)
        self.assertIsNotNone(rig.backend.restored)
        status, body = _http_get(rolled.port, "/v1/tiny")
        self.assertEqual((status, body.get("completion")), (200, "fixture-ok"))

        self.assertEqual(rig.backend.calls.count("stop"), 2)
        self.assertEqual(rig.backend.calls.count("start"), 2)
        self.assertEqual(rig.backend.calls.count("restore_lkg"), 1)
        record = rig.record(plan)
        self.assertEqual(record["phase"], "rolled_back")
        self.assertEqual(record["scope"], "fixture")
        self.assertEqual(rig.restart(plan)["code"], "replayed")

    def test_fixture_failed_rollback_ends_in_manual_recovery(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        rig.backend.next_modes = ["bad-acceptance", "bad-acceptance"]

        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "postcheck_failed")
        self.assertEqual(result["failed_phase"], "rollback_postchecking")
        self.assert_fixture_labelled(result)

        self.assertEqual(rig.backend.calls.count("stop"), 2)
        self.assertEqual(rig.backend.calls.count("start"), 2)
        self.assertEqual(rig.backend.calls.count("restore_lkg"), 1)
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["code"], "postcheck_failed")
        self.assertEqual(record["failed_phase"], "rollback_postchecking")
        # No further control after the failed rollback; replay is fenced.
        self.assertEqual(rig.restart(plan)["code"], "replayed")
        self.assertEqual(rig.backend.calls.count("stop"), 2)
        self.assertEqual(rig.backend.calls.count("start"), 2)

    def test_fixture_stop_refusal_before_any_disruption(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        rig.backend.limits["stop"] = 0

        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "service_control_denied")
        self.assertEqual(result["failed_phase"], "stopping")
        self.assert_fixture_labelled(result)

        self.assertNotIn("start", rig.backend.calls)
        self.assertNotIn("restore_lkg", rig.backend.calls)
        self.assertTrue(rig.backend.service_child.alive(), "refusal must not touch the child")
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["code"], "service_control_denied")
        self.assertEqual(rig.restart(plan)["code"], "replayed")
        self.assertEqual(rig.backend.calls.count("stop"), 1)

    def test_fixture_start_refusal_after_graceful_stop(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        rig.backend.limits["start"] = 0
        old = rig.backend.service_child

        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "service_control_denied")
        self.assertEqual(result["failed_phase"], "starting")
        self.assert_fixture_labelled(result)

        self.assertEqual(old.proc.poll(), 0, "stop genuinely happened before refusal")
        self.assertIsNone(rig.backend.service_child)
        self.assertNotIn("restore_lkg", rig.backend.calls)
        self.assertEqual(rig.backend.calls.count("start"), 1)
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["code"], "service_control_denied")
        self.assertEqual(rig.restart(plan)["code"], "replayed")
        self.assertEqual(rig.backend.calls.count("start"), 1)

    def test_fixture_rollback_stop_refusal_no_later_calls(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        rig.backend.next_modes = ["bad-acceptance"]
        rig.backend.limits["stop"] = 1  # allow the initial stop, refuse rollback's

        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "service_control_denied")
        self.assertEqual(result["failed_phase"], "rollback_stopping")
        self.assert_fixture_labelled(result)

        self.assertEqual(rig.backend.calls.count("stop"), 2)
        self.assertEqual(rig.backend.calls.count("start"), 1)
        self.assertNotIn("restore_lkg", rig.backend.calls)
        bad = rig.backend.service_child
        self.assertTrue(bad.alive(), "refused rollback stop must not touch the child")
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["code"], "service_control_denied")
        self.assertEqual(rig.restart(plan)["code"], "replayed")
        self.assertEqual(rig.backend.calls.count("stop"), 2)

    def test_fixture_rollback_restore_refusal_no_later_calls(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        rig.backend.next_modes = ["bad-acceptance"]
        rig.backend.limits["restore_lkg"] = 0

        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "service_control_denied")
        self.assertEqual(result["failed_phase"], "rollback_restoring")
        self.assert_fixture_labelled(result)

        self.assertEqual(rig.backend.calls.count("stop"), 2)
        self.assertEqual(rig.backend.calls.count("start"), 1)
        self.assertIsNone(rig.backend.restored, "refused restore took no action")
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["code"], "service_control_denied")
        self.assertEqual(rig.restart(plan)["code"], "replayed")
        self.assertEqual(rig.backend.calls.count("restore_lkg"), 1)

    def test_fixture_rollback_start_refusal_no_later_calls(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        rig.backend.next_modes = ["bad-acceptance"]
        rig.backend.limits["start"] = 1  # allow the initial start, refuse rollback's

        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "service_control_denied")
        self.assertEqual(result["failed_phase"], "rollback_starting")
        self.assert_fixture_labelled(result)

        self.assertEqual(rig.backend.calls.count("stop"), 2)
        self.assertEqual(rig.backend.calls.count("start"), 2)
        self.assertIsNotNone(rig.backend.restored)
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["code"], "service_control_denied")
        self.assertEqual(rig.restart(plan)["code"], "replayed")
        self.assertEqual(rig.backend.calls.count("start"), 2)

    def test_fixture_failed_candidate_preserves_primary(self):
        rig = self.rig
        primary = rig.backend.service_child
        before_identity = process_identity.process_identity(primary.proc.pid)
        rig.backend.candidate_mode = "candidate-bad"

        result = rig.op({"op": "prepare"})
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["code"], "candidate_failed")

        self.assert_no_service_control()
        record = rig.record(result)
        self.assertEqual(record["phase"], "blocked")
        self.assertEqual(record["code"], "candidate_failed")
        self.assertTrue(record["original_preserved"])
        self.assertEqual(record["scope"], "fixture")

        # Primary genuinely survived with stable birth identity and live HTTP.
        self.assertTrue(primary.alive())
        after_identity = process_identity.process_identity(primary.proc.pid)
        self.assertEqual(after_identity["pid"], before_identity["pid"])
        self.assertEqual(after_identity["birth"], before_identity["birth"])
        status, body = _http_get(primary.port, "/health")
        self.assertEqual((status, body.get("fixture")), (200, True))
        # The owned candidate was genuinely cleaned up via stdin control.
        candidate = rig.backend.children[1]
        self.assertEqual(candidate.proc.poll(), 0)

    def test_fixture_wrong_pid_observation_blocks_restart(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        old = rig.backend.service_child
        # Out-of-band fixture swap: the service identity is now genuinely new.
        replacement = _spawn(rig.root, rig.script, "ok")
        rig.backend.children.append(replacement)
        rig.backend.service_child = replacement
        _graceful(old)

        result = rig.restart(plan)
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["code"], "primary_changed")
        self.assert_no_service_control()
        self.assertTrue(replacement.alive(), "no stop may run for a changed primary")
        record = rig.record(plan)
        self.assertEqual(record["phase"], "prepared", "no phase mutation on identity block")
        self.assertFalse(rig.consumed(plan))
        self.assertTrue(rig.approval_file(plan))

    def test_fixture_wrong_birth_observation_blocks_restart(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        primary = rig.backend.service_child
        # Genuine PID, drifted birth: the observation no longer matches the plan.
        rig.backend.birth_usec_delta = 1

        result = rig.restart(plan)
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["code"], "primary_changed")
        self.assert_no_service_control()
        self.assertTrue(primary.alive())
        status, body = _http_get(primary.port, "/health")
        self.assertEqual((status, body.get("fixture")), (200, True))
        record = rig.record(plan)
        self.assertEqual(record["phase"], "prepared")
        self.assertFalse(rig.consumed(plan))

    def test_fixture_approval_expiry_during_final_freeze(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)  # expires at wall 1100 / mono 600
        # Second qualify sits between the freeze recheck and the final
        # approval recheck; expire the approval exactly there.
        rig.backend.qualify_hooks[2] = lambda: (rig.wall.__setitem__(0, 1140.0),
                                                rig.mono.__setitem__(0, 640.0))

        result = rig.restart(plan)
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["code"], "approval_stale")
        self.assert_no_service_control()
        # Intent was NOT consumed: no marker, approval record intact, plan prepared.
        self.assertFalse(rig.consumed(plan))
        self.assertTrue(rig.approval_file(plan))
        self.assertEqual(rig.record(plan)["phase"], "prepared")

        # A fresh owner approval (fixture private state) completes the restart.
        rig.approve(plan, lifetime=100.0)
        result = rig.restart(plan)
        self.assertEqual(result["status"], "restarted", result)
        self.assert_fixture_labelled(result)
        self.assertEqual(rig.backend.calls.count("stop"), 1)
        self.assertEqual(rig.backend.calls.count("start"), 1)
        self.assertEqual(rig.record(plan)["phase"], "restarted")

    def test_fixture_approval_expiry_after_intent_consumption(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        # Third qualify runs after intent_consumed is durably persisted and
        # immediately before the consumed-approval recheck ahead of stop.
        rig.backend.qualify_hooks[3] = lambda: (rig.wall.__setitem__(0, 1140.0),
                                               rig.mono.__setitem__(0, 640.0))

        result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "approval_stale")
        self.assertEqual(result["failed_phase"], "stopping")
        self.assert_fixture_labelled(result)

        # Consumed approval recheck fired BEFORE any service control.
        self.assertNotIn("stop", rig.backend.calls)
        self.assertNotIn("start", rig.backend.calls)
        self.assertTrue(rig.consumed(plan), "intent stays durably consumed")
        self.assertFalse(rig.approval_file(plan))
        record = rig.record(plan)
        self.assertEqual(record["phase"], "manual_recovery_required")
        self.assertEqual(record["code"], "approval_stale")
        self.assertEqual(record["failed_phase"], "stopping")
        # Never silently retried.
        self.assertEqual(rig.restart(plan)["code"], "replayed")
        self.assertNotIn("stop", rig.backend.calls)

    def test_fixture_state_io_after_first_service_control_call(self):
        rig = self.rig
        plan = rig.prepare()
        rig.approve(plan)
        old = rig.backend.service_child
        original_sync = engine_module._sync_fd

        def failing_sync(fd):
            # Fail every durable write once the first stop has been executed.
            if rig.backend.calls.count("stop") >= 1:
                raise OSError("fixture durable write failure")
            return original_sync(fd)

        with mock.patch.object(engine_module, "_sync_fd", side_effect=failing_sync):
            result = rig.restart(plan)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(result["code"], "state_io")
        self.assertEqual(result["request_id"], plan["request_id"])
        self.assert_fixture_labelled(result)

        # The stop genuinely happened; the start never ran.
        self.assertEqual(old.proc.poll(), 0)
        self.assertEqual(rig.backend.calls.count("stop"), 1)
        self.assertNotIn("start", rig.backend.calls)
        self.assertNotIn("restore_lkg", rig.backend.calls)
        self.assertIsNone(rig.backend.service_child)

        # Durable uncertainty is preserved: the last persisted phase is the
        # pre-start phase, and no guessed later phase was written.
        record = rig.record(plan)
        self.assertEqual(record["phase"], "stopping")
        self.assertNotIn("code", record)
        self.assertTrue(rig.consumed(plan))
        # No further calls even on replay.
        self.assertEqual(rig.restart(plan)["code"], "replayed")
        self.assertEqual(rig.backend.calls.count("stop"), 1)
        self.assertNotIn("start", rig.backend.calls)

    def test_fixture_results_are_labelled_not_live_backends(self):
        rig = self.rig
        # A full lifecycle result must self-describe as fixture scope, never
        # as live launchd or LiteLLM acceptance.
        plan = rig.prepare()
        self.assertEqual(plan["scope"], "fixture")
        self.assertTrue(plan["candidate_checks"]["fixture"])
        self.assertTrue(plan["primary_checks"]["fixture"])
        rig.approve(plan)
        result = rig.restart(plan)
        self.assertEqual(result["status"], "restarted")
        self.assertEqual(result["scope"], "fixture")
        self.assertTrue(result["checks"]["fixture"])
        self.assertTrue(result["checks"]["http"])
        self.assertEqual(rig.backend.label, "fixture.local.proxy")


if __name__ == "__main__":
    unittest.main()