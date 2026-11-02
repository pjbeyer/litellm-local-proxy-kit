"""Offline + fixture-driven tests for the proxy lifecycle guard.

Three layers, all with NO production contact and NO signaling of real PIDs:
1. Pure matcher positives/negatives with a fake injected discovery
   (fixture PIDs only). Bad config => block execution tools only.
2. Config schema tests: strict validation of the expected JSON schema.
3. A real loopback listener preservation fixture: a real child interpreter
   listens on an ephemeral port; the guard's REAL read-only discovery
   (lsof + ps) resolves it; blocked-kill verification runs against those
   discovered fixture PIDs only. The listener is closed, never signaled.

No literal production PID appears anywhere in this file. No private paths,
credentials, or identifiers appear in test vectors.
"""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

MODULE_PATH = (Path(__file__).resolve().parents[1] /
               "integrations/hermes/proxy-lifecycle-guard/guard.py")
_spec = importlib.util.spec_from_file_location("lifecycle_guard_for_test", MODULE_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Unable to load lifecycle guard module for tests")
guard = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = guard
_spec.loader.exec_module(guard)

FIXTURE_PID = 4711  # fixture PID only; never a real process
FIXTURE_PORT = 4000

# Subprocess use in this file is list-argv only (no shell=True): every
# command is a fixed argv with no user-controlled interpolation.


def write_policy(tmp_path, port=FIXTURE_PORT,
                 labels=("example.litellm",),
                 sessions=("fixture-session",)):
    p = Path(tmp_path) / "policy.json"
    p.write_text(json.dumps({
        "version": 1, "port": int(port),
        "labels": list(labels),
        "protected_sessions": list(sessions),
    }), encoding="utf-8")
    return p


class GuardHarness(unittest.TestCase):
    """evaluate() with an injected fake discovery (fixture PIDs only)."""

    def setUp(self):
        self.fake_snapshot = guard.Snapshot(
            pids=frozenset({FIXTURE_PID}), pgids=frozenset({FIXTURE_PID}),
            comms=frozenset({"litellm"}), args=("litellm --port 4000",))
        self.discovery_calls = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy = write_policy(self.tmp.name)

    def discover_fake(self, port):
        self.discovery_calls.append(port)
        return self.fake_snapshot

    def ev(self, tool, args, policy=None, **kw):
        policy = policy or write_policy(self.tmp.name, **kw)
        return guard.evaluate(tool, args, policy, discover=self.discover_fake)

    def assert_blocked(self, tool, args, **kw):
        result = self.ev(tool, args, **kw)
        self.assertIsNotNone(result, f"expected block for {tool}: {args!r}")
        self.assertEqual(result.get("action"), "block")
        msg = result.get("message", "")
        # Fixed sanitized message: no args, paths, PIDs, or exception detail.
        self.assertEqual(msg, guard.BLOCK_MESSAGE)
        self.assertNotRegex(msg, r"\d{3,}")
        self.assertNotIn("Traceback", msg)
        return result

    def assert_allowed(self, tool, args, **kw):
        result = self.ev(tool, args, **kw)
        self.assertIsNone(result, f"expected allow for {tool}: {args!r}")


class TestGuardBasics(GuardHarness):

    # -- read-only tools never blocked, any config state ---------------
    def test_readonly_tools_pass_even_without_config(self):
        missing = Path(self.tmp.name) / "nope.json"
        for tool, args in [
            ("read_file", {"path": "x", "pattern": "kill"}),
            ("search_files", {"pattern": "kill 4711"}),
        ]:
            self.assertIsNone(guard.evaluate(tool, args, missing,
                                             discover=self.discover_fake))

    # -- terminal: allowed commands -------------------------------------
    def test_allows_harmless_commands(self):
        for cmd in [
            "echo hello",
            "ls -la /tmp",
            "git status",
            "curl -s http://localhost:4000/health/liveness",
            "true",
            "printf 'litellm\\n'",  # mention alone is not a kill
            "grep litellm README.md",
            "/bin/ps aux | grep -i litellm",  # read-only ps
            "/usr/sbin/lsof -nP -iTCP:4000 -sTCP:LISTEN",  # read-only lsof
            "kill -0 4711",  # read-only liveness probe
        ]:
            self.assert_allowed("terminal", {"command": cmd})

    def test_allows_unrelated_literal_pid(self):
        # literal PID determinately absent from the (fake) discovery snapshot
        self.assert_allowed("terminal", {"command": "kill 99999"})
        self.assert_allowed("terminal", {"command": "kill -9 99999"})

    def test_refuses_dynamic_other_port_kill(self):
        # A process may own more than one port; another port is not PID proof.
        self.assert_blocked("terminal", {"command": "kill $(lsof -ti:8123)"})

    def test_allows_read_only_launchctl(self):
        for cmd in [
            "launchctl print gui/501/example.litellm",
            "launchctl list | grep litellm",
            "launchctl blame gui/501/example.litellm",
        ]:
            self.assert_allowed("terminal", {"command": cmd})

    # -- terminal: blocked kill patterns --------------------------------
    def test_blocks_snapshot_pid_kill(self):
        for cmd in [
            "kill 4711",
            "kill -9 4711",
            "kill -TERM 4711",
            "kill -s TERM 4711",
            "kill -s 9 4711",
            "kill -n 15 4711",
            "kill -- 4711",
            "sudo kill 4711",
            "sudo -n kill -9 4711",
            "env -i kill -9 4711",
            "nohup kill -9 4711 &",
        ]:
            self.assert_blocked("terminal", {"command": cmd})

    def test_blocks_process_group_kill(self):
        for cmd in ["kill -9 -4711", "kill -- -4711", "kill -TERM -4711"]:
            self.assert_blocked("terminal", {"command": cmd})

    def test_blocks_lsof_pipeline_kills(self):
        for cmd in [
            "kill $(lsof -t -nP -iTCP:4000 -sTCP:LISTEN)",
            "kill -9 $(lsof -ti:4000)",
            "kill $(lsof -t -i :4000)",
            "kill -9 $(/usr/sbin/lsof -t -nP -iTCP:4000 -sTCP:LISTEN)",
            "kill -9 `lsof -ti:4000`",
            "xargs kill < <(lsof -t -iTCP:4000)",
            "lsof -t -nP -iTCP:4000 -sTCP:LISTEN | xargs kill -9",
            "lsof -nP -iTCP:4000 | awk 'NR>1{print $2}' | xargs kill",
            "kill $(lsof -ti:4000) 2>/dev/null",
            "kill $(lsof -ti tcp:4000)",
        ]:
            self.assert_blocked("terminal", {"command": cmd})

    def test_blocks_fuser_port_kill(self):
        self.assert_blocked("terminal", {"command": "fuser -k 4000/tcp"})
        self.assert_allowed("terminal", {"command": "fuser 4000/tcp"})

    def test_blocks_pkill_and_killall(self):
        for cmd in [
            "pkill -9 litellm",
            "pkill -f litellm",
            "pkill -f 'litellm.*proxy'",
            "pkill -f example.litellm",
            "killall litellm",
            "sudo pkill -f litellm",
        ]:
            self.assert_blocked("terminal", {"command": cmd})

    def test_blocks_broad_selector_pkill(self):
        # broad interpreter/server selectors could kill the live proxy
        for cmd in [
            "pkill -f python",
            "pkill python3",
            "pkill -f python3.15",
            "killall python3",
            "pkill -f uvicorn",
            "pkill node",
            "pkill -u _dev",
            "pkill --uid 1000",
        ]:
            self.assert_blocked("terminal", {"command": cmd})
        # narrow unrelated pattern is fine
        self.assert_allowed("terminal",
                            {"command": "pkill -f my-unrelated-tool"})
        self.assert_allowed("terminal", {"command": "pkill -f python-foobar"})
        self.assert_allowed("terminal", {"command": "killall Firefox"})

    def test_blocks_launchctl_mutators(self):
        for cmd in [
            "launchctl bootout gui/501/example.litellm",
            "launchctl bootstrap gui/501 /tmp/com.example.litellm.plist",
            "launchctl kickstart -k gui/501/example.litellm",
            "launchctl kill TERM gui/501/example.litellm",
            "launchctl unload ~/Library/LaunchAgents/com.example.litellm.plist",
            "launchctl load ~/Library/LaunchAgents/com.example.litellm.plist",
            "launchctl stop example.litellm",
            "launchctl remove example.litellm",
            "sudo launchctl bootout gui/501/example.litellm",
            "launchctl bootout gui/$(id -u)",
            "launchctl bootout gui/501",  # whole GUI domain
        ]:
            self.assert_blocked("terminal", {"command": cmd})

    def test_blocks_brew_services(self):
        for cmd in [
            "brew services stop litellm",
            "brew services restart litellm",
            "brew services kill litellm",
            "brew services stop --all",
        ]:
            self.assert_blocked("terminal", {"command": cmd})
        self.assert_allowed("terminal", {"command": "brew services list"})
        self.assert_allowed("terminal", {"command": "brew services stop redis"})

    def test_blocks_compound_commands_any_segment(self):
        for cmd in [
            "echo hi && kill -9 4711",
            "echo hi; kill 4711; echo bye",
            "echo hi || kill 4711",
            "printf a\nkill -9 $(lsof -ti:4000)",
            "kill 4711 && echo done",
            "true & kill -9 -4711",
        ]:
            self.assert_blocked("terminal", {"command": cmd})

    # -- indeterminate / conservative refusal ---------------------------
    def test_blocks_indeterminate_targets(self):
        for cmd in [
            "kill $PID",                  # shell variable target
            "kill ${PROXY_PID}",
            "kill $(cat /tmp/pidfile)",   # unresolvable substitution
        ]:
            self.assert_blocked("terminal", {"command": cmd})

    def test_conservative_refusal_on_discovery_failure(self):
        # discovery returns None => lifecycle candidate refused
        def failing_discover(port):
            return None
        result = guard.evaluate("terminal", {"command": "kill 4711"},
                                self.policy, discover=failing_discover)
        self.assertIsNotNone(result)

    # -- Python code scanning ---------------------------------------------
    def test_blocks_os_kill_inline_python(self):
        for code in [
            "import os\nos.kill(4711, 9)",
            "import os\nos.killpg(4711, 9)",
            "import os, signal\nos.kill( 4711, signal.SIGKILL )",
        ]:
            self.assert_blocked("execute_code", {"code": code})

    def test_blocks_subprocess_kill_argv(self):
        self.assert_blocked("execute_code",
                            {"code": "subprocess.run(['kill', '-9', '4711'])"})
        self.assert_blocked("execute_code",
                            {"code": "subprocess.run(['sudo','kill','4711'])"})
        self.assert_blocked("execute_code",
                            {"code": "subprocess.run(['pkill','-f','litellm'])"})
        self.assert_blocked("execute_code",
                            {"code": "subprocess.run(['killall','python3'])"})

    def test_blocks_shell_string_in_python(self):
        self.assert_blocked("execute_code", {"code": textwrap.dedent("""\
            import subprocess
            # shell=True with a fixed fixture command (no user input)
            subprocess.run('kill $(lsof -ti:4000)', shell=True)""")})
        self.assert_blocked("browser_exec",
                            {"code": "code = 'kill -9 4711'\nrun(code)"})

    def test_allows_benign_python(self):
        self.assert_allowed("execute_code",
                            {"code": "print('hello')\nsum(range(10))"})
        self.assert_allowed("execute_code",
                            {"code": "import subprocess\nsubprocess.run(['ls','-la'])"})
        self.assert_allowed("execute_code",
                            {"code": "import psutil\np = psutil.Process(4711)\nprint(p.name())"})

    # -- deferred tool_call recursion --------------------------------------
    def test_tool_call_recursion_blocks(self):
        for call in [
            {"name": "terminal", "arguments": {"command": "kill -9 4711"}},
            {"name": "execute_code", "arguments": {"code": "os.kill(4711, 9)"}},
            {"name": "terminal", "arguments": {"command": "launchctl bootout gui/501/example.litellm"}},
            {"name": "process_manage", "arguments": {"action": "kill", "session_id": "fixture-session"}},
            {"name": "process_manage", "arguments": {"action": "write", "session_id": "fixture-session"}},
            {"name": "tool_call", "arguments": {"calls": [
                {"name": "terminal", "arguments": {"command": "kill -9 4711"}}]}},
        ]:
            self.assert_blocked("tool_call", {"calls": [call]})

    def test_tool_call_recursion_allows(self):
        for call in [
            {"name": "terminal", "arguments": {"command": "echo ok"}},
            {"name": "process_manage", "arguments": {"action": "status", "session_id": "fixture-session"}},
            {"name": "process_manage", "arguments": {"action": "wait", "session_id": "fixture-session"}},
            {"name": "process_manage", "arguments": {"action": "kill", "session_id": "other-session"}},
            {"name": "tool_call", "arguments": {"calls": [
                {"name": "terminal", "arguments": {"command": "ls"}}]}},
        ]:
            self.assert_allowed("tool_call", {"calls": [call]})

    def test_tool_call_malformed_blocks(self):
        # malformed tool_call payloads fail closed
        for args in [{"calls": "not-a-list"}, {"calls": [{"name": "terminal"}]},
                     {"calls": [{"name": "terminal", "arguments": None}]}, {}]:
            result = guard.evaluate("tool_call", args, self.policy,
                                    discover=self.discover_fake)
            self.assertIsNotNone(result, args)

    def test_tool_call_double_wrapped_string_unwrapped(self):
        # A caller may JSON-encode the whole
        # {"calls": [...]} object AS the `calls` string argument. The guard
        # must unwrap once and evaluate the inner calls on their merits —
        # NOT block them as a nameless single call. Innocent content inside
        # a double-wrapped string envelope must pass.
        inner_call = {"name": "terminal", "arguments": {"command": "echo ok"}}
        double = json.dumps({"calls": [inner_call]})
        self.assert_allowed("tool_call", {"calls": double})
        # ... and a genuine kill inside a double-wrapped string must STILL
        # block (the unwrap must not open a bypass).
        bad_call = {"name": "terminal", "arguments": {"command": "kill -9 4711"}}
        double_bad = json.dumps({"calls": [bad_call]})
        self.assert_blocked("tool_call", {"calls": double_bad})
        # nested double-wrap: the inner calls value is ITSELF a JSON object
        # string — unwrap loop must handle it (one level of nesting here).
        nested = json.dumps({"calls": json.dumps({"calls": [inner_call]})})
        self.assert_allowed("tool_call", {"calls": nested})

    def test_tool_call_nameless_object_string_still_blocks(self):
        # A dict-shaped string WITHOUT a calls key stays fail-closed.
        nameless = json.dumps({"arguments": {"command": "echo ok"}})
        self.assert_blocked("tool_call", {"calls": nameless})

    # -- process_manage ------------------------------------------------------
    def test_process_manage_matrix(self):
        blocked = [("kill", "fixture-session"), ("write", "fixture-session"),
                   ("submit", "fixture-session"), ("interrupt", "fixture-session")]
        for action, session in blocked:
            self.assert_blocked("process_manage",
                               {"action": action, "session_id": session})
        allowed = [("status", "fixture-session"), ("list", None),
                   ("wait", "fixture-session"), ("poll", "fixture-session"),
                   ("kill", "unrelated-session"), ("write", "unrelated-session")]
        for action, session in allowed:
            self.assert_allowed("process_manage",
                                {"action": action, "session_id": session})

    def test_process_manage_mutating_without_session_blocked(self):
        self.assert_blocked("process_manage", {"action": "kill"})
        self.assert_blocked("process_manage", {"action": "write", "session_id": ""})

    def test_process_manage_readonly_passes_with_bad_config(self):
        missing = Path(self.tmp.name) / "absent.json"
        self.assertIsNone(guard.evaluate(
            "process_manage", {"action": "status", "session_id": "fixture-session"},
            missing, discover=self.discover_fake))
        self.assertIsNotNone(guard.evaluate(
            "process_manage", {"action": "kill", "session_id": "fixture-session"},
            missing, discover=self.discover_fake))

    # -- bad config fails closed for execution tools -------------------------
    def test_bad_config_blocks_execution_only(self):
        cases = {"missing": Path(self.tmp.name) / "missing.json"}
        partial = Path(self.tmp.name) / "bad_schema.json"
        partial.write_text(json.dumps({"version": 1, "port": FIXTURE_PORT}))
        cases["partial"] = partial
        invalid = Path(self.tmp.name) / "bad_type.json"
        invalid.write_text("not json at all {{{")
        cases["invalid_json"] = invalid
        version = Path(self.tmp.name) / "bad_version.json"
        version.write_text(json.dumps({"version": 2, "port": FIXTURE_PORT,
                                       "labels": [], "protected_sessions": []}))
        cases["version"] = version
        extra = Path(self.tmp.name) / "extra.json"
        extra.write_text(json.dumps({"version": 1, "port": FIXTURE_PORT,
                                     "labels": [], "protected_sessions": [],
                                     "bypass": True}))
        cases["extra_keys"] = extra
        for label, path in cases.items():
            for tool, args in [("terminal", {"command": "echo hi"}),
                               ("execute_code", {"code": "print(1)"}),
                               ("browser_exec", {"code": "print(1)"}),
                               ("tool_call", {"calls": [{"name": "terminal",
                                       "arguments": {"command": "echo"}}]}),
                               ("process_manage", {"action": "kill",
                                                   "session_id": "fixture-session"})]:
                result = guard.evaluate(tool, args, path, discover=self.discover_fake)
                self.assertIsNotNone(result, f"{label}/{tool} should block")
                self.assertEqual(result.get("message"), guard.BLOCK_MESSAGE)
            # read-only tools unaffected by bad config
            self.assertIsNone(guard.evaluate("read_file", {"path": "x"}, path))

    # -- sanitized block message -------------------------------------------
    def test_block_message_sanitized(self):
        result = self.assert_blocked("terminal", {"command": "kill -9 4711"})
        msg = result["message"]
        self.assertIn("proxy lifecycle guard", msg)
        self.assertNotRegex(msg, r"(?i)traceback|exception|/Users/|/home/|4711")
        self.assertTrue(guard.BLOCK_MESSAGE.isascii())


class TestPolicySchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _write(self, obj):
        p = self.dir / "policy.json"
        p.write_text(json.dumps(obj), encoding="utf-8")
        return p

    def test_valid_policy(self):
        p = self._write({"version": 1, "port": FIXTURE_PORT,
                         "labels": ["example.litellm"],
                         "protected_sessions": ["fixture-session"]})
        pol = guard.load_policy(p)
        self.assertEqual(pol.port, FIXTURE_PORT)
        self.assertEqual(pol.labels, frozenset({"example.litellm"}))
        self.assertEqual(pol.protected_sessions, frozenset({"fixture-session"}))

    def test_rejects_bad_policies(self):
        bad_policies = [
            "not-a-dict",
            ["list"],
            {"version": 2, "port": FIXTURE_PORT, "labels": [], "protected_sessions": []},
            {"version": 1, "port": "4000", "labels": [], "protected_sessions": []},
            {"version": 1, "port": True, "labels": [], "protected_sessions": []},
            {"version": 1, "port": 0, "labels": [], "protected_sessions": []},
            {"version": 1, "port": 70000, "labels": [], "protected_sessions": []},
            {"version": 1, "port": FIXTURE_PORT, "labels": "x", "protected_sessions": []},
            {"version": 1, "port": FIXTURE_PORT, "labels": [1], "protected_sessions": []},
            {"version": 1, "port": FIXTURE_PORT, "labels": [""], "protected_sessions": []},
            {"version": 1, "port": FIXTURE_PORT, "labels": [], "protected_sessions": {}},
            {"version": 1, "port": FIXTURE_PORT, "labels": [], "protected_sessions": [], "x": 1},
            {"port": FIXTURE_PORT, "labels": [], "protected_sessions": []},
            {"version": 1, "labels": [], "protected_sessions": []},
        ]
        for obj in bad_policies:
            p = self._write(obj)
            with self.assertRaises(guard.PolicyError, msg=repr(obj)):
                guard.load_policy(p)

    def test_missing_file_raises(self):
        with self.assertRaises(guard.PolicyError):
            guard.load_policy(self.dir / "absent.json")


LISTENER_CODE = textwrap.dedent("""\
    import socket, sys, time, select
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    print(srv.getsockname()[1], flush=True)
    deadline = time.time() + 120
    try:
        srv.settimeout(0.2)
        while time.time() < deadline:
            if select.select([sys.stdin], [], [], 0)[0]:
                break
            try:
                conn, _ = srv.accept()
                conn.close()
            except socket.timeout:
                continue
    finally:
        srv.close()
""")


class TestLoopbackListenerPreservationFixture(unittest.TestCase):
    """REAL read-only discovery against a REAL loopback listener.

    A real child interpreter listens on an ephemeral port. The guard's
    actual lsof/ps discovery (read-only, output never printed) resolves
    the listener. We verify:
    - discovery finds the fixture listener PID (a distinct child process),
    - killing that PID (or its PGID) is blocked,
    - killing a literal unrelated PID is allowed,
    - after closing the listener, discovery answers determinately empty
      and unrelated literal kills stay allowed while port-targeted
      substitutions stay blocked.

    The listener is only ever asked to exit and waited on — never signaled.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = Path(self.tmp.name) / "listener.py"
        self.script.write_text(LISTENER_CODE, encoding="utf-8")
        self.child = subprocess.Popen(
            [sys.executable, str(self.script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._stop_child)
        self.port = int(self.child.stdout.readline().strip())
        self.policy = write_policy(self.tmp.name, port=self.port)

    def _stop_child(self):
        # Request an owned fixture shutdown over stdin; no process signals.
        if self.child.poll() is None:
            self.child.communicate("stop\n", timeout=10)
        else:
            self.child.wait(timeout=10)

    def _snapshot(self):
        return guard.discover_snapshot(self.port, timeout=8.0)

    def test_discovery_finds_fixture_listener(self):
        snap = self._snapshot()
        self.assertIsNotNone(snap, "lsof/ps discovery unavailable in test env")
        self.assertEqual(len(snap.pids), 1)
        self.assertNotIn(os.getpid(), snap.pids,
                         "discovery must resolve the child, not this process")
        pid = next(iter(snap.pids))
        self.assertEqual(pid, self.child.pid)

    def test_guard_blocks_kill_of_discovered_listener_pid(self):
        snap = self._snapshot()
        self.assertIsNotNone(snap)
        pid = next(iter(snap.pids))
        pgid = next(iter(snap.pgids))
        for cmd in (f"kill -9 {pid}", f"kill {pid}", f"kill -TERM -- -{pgid}"):
            result = guard.evaluate("terminal", {"command": cmd}, self.policy)
            self.assertIsNotNone(result, cmd)
            self.assertEqual(result.get("message"), guard.BLOCK_MESSAGE)

    def test_guard_allows_unrelated_kill_while_listener_live(self):
        snap = self._snapshot()
        self.assertIsNotNone(snap)
        unrelated = 99999  # literal PID determinately absent from snapshot
        self.assertNotIn(unrelated, snap.pids)
        self.assertIsNone(guard.evaluate(
            "terminal", {"command": f"kill {unrelated}"}, self.policy))

    def test_empty_determinate_answer_after_close(self):
        # Explicit fixture protocol closes the listener without signaling it.
        self._stop_child()
        deadline = time.time() + 15
        snap = None
        while time.time() < deadline:
            snap = self._snapshot()
            if snap is not None and not snap.pids:
                break
            time.sleep(0.3)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.pids, frozenset(),
                         "closed listener must yield a determinate empty snapshot")
        # unrelated literal kill stays allowed with empty snapshot
        self.assertIsNone(guard.evaluate(
            "terminal", {"command": "kill 99999"}, self.policy))
        # port-targeted substitution still blocked (mentions configured port)
        self.assertIsNotNone(guard.evaluate(
            "terminal", {"command": f"kill $(lsof -ti:{self.port})"}, self.policy))

    def test_block_message_stays_sanitized_in_live_discovery(self):
        result = guard.evaluate(
            "terminal", {"command": "kill $(lsof -ti:%d)" % self.port},
            self.policy)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("message"), guard.BLOCK_MESSAGE)
        self.assertNotIn(str(self.port), result.get("message", ""))


class TestBounds(unittest.TestCase):
    """Matcher stays fast and bounded under adversarial input."""

    def _policy(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return write_policy(tmp.name)

    def test_huge_input_blocked_quickly(self):
        policy = self._policy()
        start = time.monotonic()
        for text in ["kill 4711; " * 20000, "a" * 300000, "kill $((1+1))" * 5000]:
            result = guard.evaluate("terminal", {"command": text}, policy)
            self.assertIsNotNone(result)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 10.0, f"bounded scan too slow: {elapsed:.1f}s")

    def test_non_string_command_blocked(self):
        policy = self._policy()
        for bad in (None, 123, ["kill", "4711"], {"a": 1}):
            self.assertIsNotNone(guard.evaluate("terminal", {"command": bad}, policy))

    def test_missing_command_key_blocks(self):
        # malformed execution-tool payload: fail closed, never open
        policy = self._policy()
        self.assertIsNotNone(guard.evaluate("terminal", {}, policy))


class TestParentRegressionGaps(GuardHarness):
    def test_quoted_pids_and_inline_wrappers(self):
        for command in [
            'kill -9 "4711"', "exec kill -9 4711", "command kill 4711",
            "builtin kill 4711", "FLAG=fixture kill 4711",
            "bash -lc 'kill -9 4711'", 'sh -c "kill -TERM 4711"',
            "python3 -c 'import os; os.kill(4711, 9)'",
            "(kill -9 4711)", "kill -9 -- -1", "kill -TERM 0",
        ]:
            self.assert_blocked('terminal', {'command': command})
        self.assert_allowed('terminal', {'command': 'echo "kill -9 4711"'})
        self.assert_allowed('terminal', {'command': "kill -0 $(lsof -ti:4000)"})
        self.assert_allowed('terminal', {'command': "python3 -c 'print(1)'"})

    def test_pattern_discovery_failure_is_not_safe(self):
        result = guard.evaluate('terminal', {'command': 'pkill -f narrow-pattern'},
                                self.policy, discover=lambda _: None)
        self.assertIsNotNone(result)
        self.assert_blocked('terminal', {'command': "pkill -f '^.*'"})

    def test_malformed_discovery_is_not_empty(self):
        from unittest.mock import patch
        def completed(stdout, returncode=0, stderr=''):
            return subprocess.CompletedProcess([], returncode, stdout, stderr)
        for outputs in [
            [completed('not-a-pid')],
            [completed('')],
            [completed('', 1, 'permission denied')],
            [completed('4711'), completed('malformed detail')],
        ]:
            with patch.object(guard.subprocess, 'run', side_effect=outputs):
                self.assertIsNone(guard.discover_snapshot(4000))

    def test_review_substitutions_and_path_qualified_argv(self):
        for command in [
            'echo "$(kill -9 4711)"', 'printf "%s" `kill 4711`',
            'echo "$(echo "$(kill -TERM 4711)")"',
            'echo $(true) $(kill 4711)',
        ]:
            self.assert_blocked('terminal', {'command': command})
        for code in [
            "subprocess.run(['/bin/kill', '-9', '4711'])",
            "subprocess.run(['/usr/bin/pkill', '-f', 'litellm'])",
            "subprocess.run(['/bin/launchctl', 'bootout', 'gui/501/example.litellm'])",
        ]:
            self.assert_blocked('execute_code', {'code': code})
        for command in ["echo '$(kill -9 4711)'", r'echo \$(kill)',
                        "echo $(date)", 'echo "$(printf hello)"']:
            self.assert_allowed('terminal', {'command': command})

    def test_deep_review_handle_and_pipeline_regressions(self):
        for command in [
            'env -u FOO kill 4711', 'env --unset FOO kill 4711',
            'sudo -u fixture kill 4711', 'command -p kill 4711',
            'pkill -f l.tellm', 'echo 4711 | xargs kill',
            'printf 4711 | xargs /bin/kill -9', 'kill $(lsof -t -p 4711)',
        ]:
            self.assert_blocked('terminal', {'command': command})
        for session in ('proc_fixture123', 'proc_fixture', 'fixture123', 'fixture'):
            for action in ('kill', 'write', 'submit', 'close'):
                self.assert_blocked('process_manage', {'action': action, 'session_id': session},
                                    sessions=('proc_fixture123',))
        self.assert_allowed('terminal', {'command': 'env -u FOO printf hello'})
        self.assert_allowed('process_manage', {'action': 'close', 'session_id': 'unrelated'})

    def test_signed_pid_scan_limits_and_normalized_deferred_calls(self):
        self.assert_blocked('terminal', {'command': 'kill -9 +4711'})
        self.assert_allowed('terminal', {'command': 'kill +99999'})
        code = '\n'.join('x = "fixture-%d"' % i for i in range(81))
        self.assert_blocked('execute_code', {'code': code + '\nos.system("kill 4711")'})
        for call in [
            {'name': 'process_manage', 'arguments': {'action': 'poll', 'session_id': 'fixture-session'}},
            {'calls': json.dumps([{'name': 'process_manage', 'arguments': {'action': 'poll'}}])},
            {'calls': {'name': 'process_manage', 'arguments': json.dumps({'action': 'list'})}},
        ]:
            self.assert_allowed('tool_call', call)
        for call in [
            {'name': 'process_manage', 'arguments': {'action': 'handoff', 'session_id': 'fixture-session'}},
            {'calls': json.dumps([{'name': 'terminal', 'arguments': json.dumps({'command': 'kill +4711'})}])},
        ]:
            self.assert_blocked('tool_call', call)

    def test_process_substitutions_and_parsed_python_strings(self):
        # Analysis-only code strings: never execute these shell/system fixtures.
        for command in ['cat <(kill -9 4711)', 'tee >(kill 4711)',
                        'cat <(echo $(kill 4711))']:
            self.assert_blocked('terminal', {'command': command})
        for code in [
            'subprocess.run("""echo ok' + chr(10) + 'kill -9 4711""", shell=True)',
            "subprocess.run(('/bin/kill', '-9', 4711))",
            "subprocess.run(['/bin/kill', '-9', target_pid])",
            'os.system(f"kill -9 {target_pid}")',
            'os.system(' + repr('echo ' + 'x' * 500 + '; kill 4711') + ')',
        ]:
            self.assert_blocked('execute_code', {'code': code})
        self.assert_allowed('terminal', {'command': 'cat <(printf hello)'})
        self.assert_allowed('execute_code', {'code': 'print("""multiline\\nhello""")'})
        self.assert_allowed('execute_code', {'code': 'print(f"hello {name}")'})

    def test_adapter_registers_and_calls_real_guard(self):
        from unittest.mock import Mock
        name = 'proxy_lifecycle_guard_adapter_test'
        spec = importlib.util.spec_from_file_location(
            name, MODULE_PATH.with_name('__init__.py'),
            submodule_search_locations=[str(MODULE_PATH.parent)])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
            ctx = Mock()
            module.register(ctx)
            ctx.register_hook.assert_called_once()
            event, callback = ctx.register_hook.call_args.args
            self.assertEqual(event, 'pre_tool_call')
            # Source tree intentionally has no runtime config; reads still pass,
            # and executable calls fail closed through the genuine callback.
            self.assertIsNone(callback(tool_name='read_file', args={'path': 'fixture'}))
            self.assertEqual(callback(tool_name='terminal', args={'command': 'echo hi'}),
                             {'action': 'block', 'message': guard.BLOCK_MESSAGE})
        finally:
            sys.modules.pop(name + '.guard', None)
            sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)