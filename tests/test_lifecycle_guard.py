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
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import socket
import stat
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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_integrity_ref(dirpath, policy_name="policy.json",
                        sources=(), policy_bytes=None):
    """Owner-establish an integrity.sha256 next to the policy (FR-015/D3).

    Records the policy digest and per-source-file digests. Test fixture
    stands in for the owner install action; the guarded agent can never
    author this file through in-scope tools. With no explicit sources,
    stages the deployed source pair (guard.py + __init__.py) beside the
    policy — the guard now requires a COMPLETE digest set (codex review
    P2), so every fixture dir mirrors a real deployment layout.
    """
    d = Path(dirpath)
    policy_path = d / policy_name
    digest_of = (policy_bytes if policy_bytes is not None
                 else policy_path.read_bytes())
    if not sources:
        for name in ("guard.py", "__init__.py"):
            target = d / name
            if not target.exists():
                shutil.copyfile(MODULE_PATH.parent / name, target)
        sources = (d / "guard.py", d / "__init__.py")
    ref = {
        "policy_digest": sha256_bytes(digest_of),
        "source_digests": {
            Path(s).name: sha256_bytes(Path(s).read_bytes()) for s in sources
        },
    }
    target = d / "integrity.sha256"
    target.write_text(json.dumps(ref, indent=2, sort_keys=True),
                      encoding="utf-8")
    return target


# Rev-3 policy shape (contracts/policy-schema.json). Environment-specific
# values (port, labels, handles) are FIXTURE values only; the kit repo
# never carries real ones (Constitution IV).
REV3_BOUNDS = {
    "input_text_bytes": 262144,
    "max_chunks": 512,
    "max_depth": 8,
    "discovery_timeout_ms": 2000,
    "max_snapshot_pids": 256,
}


def write_policy(tmp_path, port=FIXTURE_PORT,
                 labels=("example.litellm",),
                 sessions=("fixture-session",),
                 bounds=None, policy_name="policy.json"):
    """Write a schema-valid rev-3 policy + matching integrity reference."""
    obj = {
        "schema_version": 1,
        "protected_port": int(port),
        "protected_labels": list(labels),
        "protected_session_handles": list(sessions),
        "bounds": dict(REV3_BOUNDS),
        "refusal_vocabulary_version": 1,
    }
    if bounds:
        obj["bounds"].update(bounds)
    p = Path(tmp_path) / policy_name
    p.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    write_integrity_ref(tmp_path, policy_name=policy_name)
    return p


class GuardHarness(unittest.TestCase):
    """evaluate() with an injected fake discovery (fixture PIDs only)."""

    def setUp(self):
        from types import SimpleNamespace

        self.fake_snapshot = guard.Snapshot(
            pids=frozenset({FIXTURE_PID}), pgids=frozenset({FIXTURE_PID}),
            comms=frozenset({'litellm'}), args=('litellm --port 4000',))
        self.discovery_calls = []
        self._retain_sink_fixtures = False

        self.tmp = SimpleNamespace(name=tempfile.mkdtemp())
        self.addCleanup(self._cleanup_fixture_directory, self.tmp.name)
        self.policy = write_policy(self.tmp.name)

        self._sink_state = SimpleNamespace(
            name=tempfile.mkdtemp(prefix='lg_sink_iso_'))
        self.addCleanup(self._cleanup_fixture_directory, self._sink_state.name)
        self._old_state_home = os.environ.get('XDG_STATE_HOME')
        os.environ['XDG_STATE_HOME'] = self._sink_state.name
        if self._old_state_home is None:
            self.addCleanup(os.environ.pop, 'XDG_STATE_HOME', None)
        else:
            self.addCleanup(os.environ.__setitem__, 'XDG_STATE_HOME', self._old_state_home)

    def _cleanup_fixture_directory(self, path):
        if not self._retain_sink_fixtures:
            shutil.rmtree(path, ignore_errors=True)

    def discover_fake(self, port):
        self.discovery_calls.append(port)
        return self.fake_snapshot

    def ev(self, tool, args, policy=None, **kw):
        # kw may carry policy-shaping overrides (port/labels/sessions/
        # bounds) plus an optional injected discover= for S-09 cases.
        discover = kw.pop("discover", None) or self.discover_fake
        policy = policy or write_policy(self.tmp.name, **kw)
        return guard.evaluate(tool, args, policy, discover=discover)

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

    # -- Wave-1 battery decision helpers (spec rev 3, data-model.md) -----
    #
    # The rev-3 Evaluation Decision has exactly three outcomes: allow
    # (evaluate() -> None), veto (LGV-001 fixed message), refuse (distinct
    # sanitized category + LGV code). These helpers assert the FULL decision
    # contract, not just a non-None result, so a regression to the single
    # byte-identical BLOCK_MESSAGE fails these tests (red-first).

    _NO_LEAK_MARKERS = ("/Users/", "/home/", "/tmp/", "Traceback",
                        "fixture", "example.litellm", "4711", "4000")

    # Fixed per-category guidance substrings pinned INDEPENDENTLY of the
    # implementation, from research.md D2 (FR-002): the battery must not
    # read its expectations back from the SUT.
    EXPECTED_GUIDANCE = {
        "VETO": "owner-reviewed",
        "BOUND_SIZE": "reduce the input size and resubmit",
        "BOUND_DEPTH": "reduce the call nesting and resubmit",
        "BOUND_COUNT": "reduce the batch size and resubmit",
        "SHAPE_ENVELOPE": "could not be proven safe",
        "POLICY_INVALID": "repaired by the owner",
        "HOOK_MISMATCH": "could not be attributed to a known tool",
        # FR-015: the integrity-reference failure is its OWN distinct
        # fail-closed category. The spec pins its existence but not its
        # name/code; the battery pins the reconciliation choice
        # INTEGRITY_REF_INVALID / LGV-032 (next code after LGV-031)
        # — flagged as a spec-vocabulary note in the wave report.
        "INTEGRITY_REF_INVALID": "integrity reference",
    }

    def _assert_sanitized(self, msg):
        # FR-002: no arguments, paths, PIDs, error details — for ANY
        # category, including short PIDs and fixture identifiers.
        self.assertNotRegex(msg, r"\d{2,}")
        for marker in self._NO_LEAK_MARKERS:
            self.assertNotIn(marker, msg)

    def _assert_guidance(self, category, msg):
        expected = self.EXPECTED_GUIDANCE[category]
        self.assertIn(expected, msg.lower(),
                      f"{category} message must carry its D2 guidance")

    def decision_of(self, tool, args, **kw):
        """Return the decision dict with category/log_code parsed out."""
        result = self.ev(tool, args, **kw)
        return result if isinstance(result, dict) else None

    def assert_veto(self, tool, args, **kw):
        """Recognized lifecycle threat: outcome=veto, LGV-001, fixed message."""
        result = self.ev(tool, args, **kw)
        self.assertIsNotNone(result, f"expected veto for {tool}: {args!r}")
        self.assertEqual(result.get("action"), "block")
        self.assertEqual(result.get("log_code"), "LGV-001")
        self.assertEqual(result.get("category"), "VETO")
        msg = result.get("message", "")
        # Veto message names the owner-reviewed restart workflow (D2)
        # and leaks no arguments/paths/PIDs (FR-002).
        self._assert_guidance("VETO", msg)
        self._assert_sanitized(msg)
        return result

    def assert_refuse(self, tool, args, category, log_code, **kw):
        """Non-veto refusal: distinct sanitized category + stable LGV code."""
        result = self.ev(tool, args, **kw)
        self.assertIsNotNone(
            result, f"expected {category} refusal for {tool}: {args!r}")
        self.assertEqual(result.get("action"), "block")
        self.assertEqual(result.get("category"), category,
                         f"wrong category for {tool}: {args!r}")
        self.assertEqual(result.get("log_code"), log_code)
        msg = result.get("message", "")
        # Per-category corrective guidance (FR-002/D2) — expectations
        # pinned in this file, never read back from the implementation.
        self._assert_guidance(category, msg)
        self._assert_sanitized(msg)
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
                # rev-3 decision: policy failures carry the POLICY_INVALID
                # category + stable code + its sanitized message
                self.assertEqual(result.get("category"), "POLICY_INVALID")
                self.assertEqual(result.get("log_code"), "LGV-030")
                self.assertIn("repaired by the owner", result.get("message", ""))
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
        write_integrity_ref(self.dir, policy_bytes=p.read_bytes())
        return p

    def test_valid_policy(self):
        p = write_policy(self.dir)
        pol = guard.load_policy(p)
        self.assertEqual(pol.port, FIXTURE_PORT)
        self.assertEqual(pol.labels, frozenset({"example.litellm"}))
        self.assertEqual(pol.protected_sessions, frozenset({"fixture-session"}))
        self.assertEqual(pol.bounds["max_depth"], REV3_BOUNDS["max_depth"])

    def test_rejects_bad_policies(self):
        base = {"schema_version": 1, "protected_port": FIXTURE_PORT,
                "protected_labels": ["example.litellm"],
                "protected_session_handles": ["fixture-session"],
                "bounds": dict(REV3_BOUNDS),
                "refusal_vocabulary_version": 1}
        bad_policies = [
            "not-a-dict",
            ["list"],
            # wrong/missing/extra keys
            {**base, "schema_version": 2},
            {**base, "protected_port": "4000"},
            {**base, "protected_port": True},
            {**base, "protected_port": 0},
            {**base, "protected_port": 70000},
            {**base, "protected_labels": "x"},
            {**base, "protected_labels": [1]},
            {**base, "protected_labels": [""]},
            {**base, "protected_session_handles": {}},
            {**base, "refusal_vocabulary_version": 0},
            {**base, "bounds": "x"},
            {**base, "bounds": {**REV3_BOUNDS, "input_text_bytes": 1}},
            {**base, "bounds": {**REV3_BOUNDS, "max_chunks": 999}},
            {**base, "bounds": {**REV3_BOUNDS, "extra": 1}},
            {k: v for k, v in base.items() if k != "schema_version"},
            {k: v for k, v in base.items() if k != "bounds"},
            {**base, "x": 1},
        ]
        for obj in bad_policies:
            p = self._write(obj)
            with self.assertRaises(guard.PolicyError, msg=repr(obj)):
                guard.load_policy(p)

    def test_missing_file_raises(self):
        with self.assertRaises(guard.PolicyError):
            guard.load_policy(self.dir / "absent.json")

    def test_missing_integrity_ref_raises_own_category(self):
        # FR-015: missing reference is IntegrityRefError (own category),
        # distinct from PolicyError
        p = write_policy(self.dir)
        (self.dir / "integrity.sha256").unlink()
        with self.assertRaises(guard.IntegrityRefError):
            guard.load_policy(p)


LISTENER_CODE = textwrap.dedent("""
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




def _isolate_state_home(case):
    """guard-hardening: point XDG_STATE_HOME at a temp dir for one test case."""
    iso = tempfile.TemporaryDirectory(prefix="lg_sink_iso_")
    case.addCleanup(iso.cleanup)
    old = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = iso.name
    if old is None:
        case.addCleanup(os.environ.pop, "XDG_STATE_HOME", None)
    else:
        case.addCleanup(os.environ.__setitem__, "XDG_STATE_HOME", old)

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
        _isolate_state_home(self)
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
    def setUp(self):
        _isolate_state_home(self)

    """Matcher stays fast and bounded under adversarial input."""

    def _policy(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return write_policy(tmp.name)

    # -- huge inputs stay bounded and decide per FR-007 -------------------
    def test_huge_input_blocked_quickly(self):
        policy = self._policy()
        start = time.monotonic()
        # intent-carrying oversized inputs: the veto is never lost to
        # the bound (FR-007 row b)
        for text in ["kill 4711; " * 20000, "kill $((1+1))" * 5000]:
            result = guard.evaluate("terminal", {"command": text}, policy)
            self.assertIsNotNone(result)
        # intent-free oversized input: row (a) bounded head-scan allows
        result = guard.evaluate("terminal", {"command": "a" * 300000}, policy)
        self.assertIsNone(result)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 10.0, f"bounded scan too slow: {elapsed:.1f}s")

    def test_overbound_whitespace_intent_evasion(self):
        # codex review P1: shell whitespace variants must not evade the
        # row-(b) full-text intent scan — "kill\t123" hidden beyond the
        # scanned head of an over-bound input still trips the veto
        policy = self._policy()
        over = REV3_BOUNDS["input_text_bytes"] + 1
        for text in [
            "echo x" + "y" * over + "\nkill\t4711\n",
            "echo x" + "y" * over + "\nbrew\tservices stop litellm\n",
            "echo x" + "y" * over + "\nkill\t-\t9\t4711\n",
        ]:
            with self.subTest(text=text[-14:]):
                result = guard.evaluate(
                    "terminal", {"command": text}, policy)
                self.assertIsNotNone(
                    result,
                    "whitespace-variant kill intent must not be allowed "
                    "beyond the head scan")
        # in-bound whitespace kills are equally caught (chunk tokenizer
        # normalizes whitespace already; pins the equivalence) — with a
        # fixture snapshot injected so no real discovery is contacted
        snap = guard.Snapshot(
            pids=frozenset({FIXTURE_PID}), pgids=frozenset({FIXTURE_PID}),
            comms=frozenset({"litellm"}), args=("litellm --port 4000",))
        for cmd in ["kill\t4711", "brew\tservices stop litellm"]:
            self.assertIsNotNone(guard.evaluate(
                "terminal", {"command": cmd}, policy,
                discover=lambda port: snap))
        # benign whitespace remains allowed
        self.assertIsNone(guard.evaluate(
            "terminal", {"command": "echo\thello\tworld" + "y" * over},
            policy))

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
            ctx = _RecordingContext(self)
            module.register(ctx)
            ctx.capture_guard(sys.modules[module.__name__ + ".guard"])
            ctx.register_hook.assert_called_once()
            event, callback = ctx.register_hook.call_args.args
            self.assertEqual(event, 'pre_tool_call')
            # Source tree intentionally has no runtime config; reads still
            # pass, and executable calls fail closed through the genuine
            # callback — with the rev-3 POLICY_INVALID decision (no
            # config.json / integrity reference in the source tree).
            self.assertIsNone(callback(tool_name='read_file', args={'path': 'fixture'}))
            result = callback(tool_name='terminal', args={'command': 'echo hi'})
            self.assertEqual(result.get("action"), "block")
            self.assertEqual(result.get("category"), "POLICY_INVALID")
            self.assertEqual(result.get("log_code"), "LGV-030")
        finally:
            sys.modules.pop(name + '.guard', None)
            sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# Wave-1 reconciliation batteries (spec rev 3, tasks.md T001–T003).
# Written against the CURRENT implementation; they MUST run red before
# the reconciliation code lands (Constitution III, FR-013). assert_veto /
# assert_refuse assert the full rev-3 Evaluation Decision contract:
# category + stable LGV code + sanitized message per category.
# ---------------------------------------------------------------------------


class TestVetoSurfaceBattery(GuardHarness):
    """T001: veto-surface regression battery.

    Cites FR-001, FR-003, FR-006, FR-013, US1-S2/S3/S4/S6/S7/S8, SC-001;
    reproduces red-team findings F-001, F-002, F-003, F-004, F-006,
    F-007, F-008, F-009, F-017.
    """

    # -- F-001/F-006: control-flow-wrapped kills (US1-S6) ----------------
    def test_cf_if_bodies(self):
        for cmd in [
            "if true; then kill -9 4711; fi",
            'if [ -n "$X" ]; then pkill -f litellm; fi',
            "if grep -q litellm x; then killall litellm; fi",
        ]:
            self.assert_veto("terminal", {"command": cmd})

    def test_cf_while_for_case_bodies(self):
        for cmd in [
            "while true; do kill -9 4711; done",
            "while read p; do kill -9 $p; done",
            "for p in $(lsof -ti:4000); do kill -9 $p; done",
            "for i in 1 2 3; do pkill -f litellm; done",
            "case $x in a) kill -9 4711;; b) echo ok;; esac",
            "case $x in a) pkill litellm;; esac",
        ]:
            self.assert_veto("terminal", {"command": cmd})

    def test_cf_brace_groups_negation(self):
        for cmd in [
            "{ kill -9 4711; }",
            "{ pkill -f litellm; }",
            "! kill -9 4711",
            "! pkill -f litellm",
            "if true; then { kill -9 4711; }; fi",
        ]:
            self.assert_veto("terminal", {"command": cmd})

    def test_cf_wrapper_and_option_residue(self):
        for cmd in [
            "time -p kill -9 4711",
            "time -p pkill -f litellm",
        ]:
            self.assert_veto("terminal", {"command": cmd})

    # -- F-002: loop/xargs-fed port pipelines (US1-S2) --------------------
    def test_pipeline_loop_fed_port_kills(self):
        for cmd in [
            ("lsof -t -nP -iTCP:4000 -sTCP:LISTEN | "
             "while read p; do kill -9 $p; done"),
            "for p in $(lsof -ti:4000); do kill -9 $p; done",
        ]:
            self.assert_veto("terminal", {"command": cmd})

    def test_pipeline_xargs_shell_payloads(self):
        # F-007: xargs stages whose arguments carry sh/bash/python -c
        # payloads are kill pipelines (listener discovery feeding signal)
        for cmd in [
            "lsof -t -iTCP:4000 | xargs sh -c 'kill -9 $0'",
            "lsof -t -iTCP:4000 | xargs -I{} bash -c 'kill -9 {}'",
            ("lsof -t -iTCP:4000 | xargs python -c "
             "'import os, sys; os.kill(int(sys.argv[1]), 9)'"),
            "ps aux | grep -i litellm | awk '{print $2}' | xargs kill -9",
        ]:
            self.assert_veto("terminal", {"command": cmd})

    # -- F-004/F-008: variable/unresolved operands (US1-S3/S4, FR-006) ----
    def test_launchctl_variable_operands(self):
        for cmd in [
            "launchctl bootout gui/501/$LABEL",
            "launchctl bootout $PLIST",
            'launchctl bootout "gui/$(id -u)/$LABEL"',
            "launchctl kickstart -k gui/501/$LABEL",
            "launchctl unload $PLIST",
        ]:
            self.assert_veto("terminal", {"command": cmd})

    def test_brew_variable_service_names(self):
        for cmd in [
            "brew services stop $SVC",
            "brew services restart $SVC",
            'brew services stop "$SERVICE_NAME"',
        ]:
            self.assert_veto("terminal", {"command": cmd})

    def test_session_handle_unresolved_operands(self):
        # US1-S5: mutating action with no determinate session identifier
        # is blocked; shorter targeted forms of a protected session refuse
        self.assert_veto("process_manage",
                         {"action": "kill", "session_id": "fixture-sess"})
        self.assert_veto("tool_call", {"calls": [
            {"name": "process_manage",
             "arguments": {"action": "kill",
                           "session_id": "$SESSION"}}]})

    def test_eval_and_indirection_refused(self):
        # eval + variable indirection: refused, not evaluated (FR-003)
        for cmd in [
            'CMD="kill -9 4321"; $CMD',
            "CMD='pkill -f litellm'; $CMD",
            'sh -c "$CMD"',
            'eval "pkill -f litellm"',
            "eval 'kill -9 4711'",
            'eval "launchctl bootout gui/501/example.litellm"',
            'eval "brew services stop litellm"',
        ]:
            self.assert_veto("terminal", {"command": cmd})

    # -- F-003/F-009: name dispatch fails closed (US1-S8) ----------------
    # Hook-contract rule 1: empty/unknown/case-variant-of-read-only names
    # carrying executable-looking arguments → HOOK_MISMATCH refuse
    # (LGV-031). Case-variants of EXECUTION_TOOLS normalize and dispatch
    # to the real analyzer (US1-S8: dispatch never exempts inspection).
    def test_unknown_name_envelopes_with_executable_arguments(self):
        for name in ["shell_exec", "os_system", "run_command", "x"]:
            self.assert_refuse("tool_call", {"calls": [
                {"name": name,
                 "arguments": {"command": "kill -9 4711"}}]},
                               "HOOK_MISMATCH", "LGV-031")

    def test_unknown_name_envelope_code_arguments(self):
        for name in ["shell_exec", "os_system"]:
            self.assert_refuse("tool_call", {"calls": [
                {"name": name,
                 "arguments": {"code": "import os\nos.kill(4711, 9)"}}]},
                               "HOOK_MISMATCH", "LGV-031")
        self.assert_refuse("tool_call", {"calls": [
            {"name": "",
             "arguments": {"command": "pkill -f litellm"}}]},
                           "HOOK_MISMATCH", "LGV-031")

    def test_case_variant_execution_tool_names_dispatch(self):
        # case-variant of a KNOWN execution tool normalizes and hits the
        # real analyzer → the kill intent vetoes (FR-003/US1-S8)
        for name in ["Terminal", "Execute_Code", "Browser_Exec"]:
            self.assert_veto("tool_call", {"calls": [
                {"name": name,
                 "arguments": {"command": "kill -9 4711"}}]})
            self.assert_veto("tool_call", {"calls": [
                {"name": name,
                 "arguments": {"code": "import os\nos.kill(4711, 9)"}}]})

    def test_case_variant_readonly_names_refused(self):
        # case-variant of a READ-ONLY name carrying executable arguments
        # is HOOK_MISMATCH (rule 1) — never a silent pass
        for name in ["Read_File", "READ_FILE", "Web_Search"]:
            self.assert_refuse("tool_call", {"calls": [
                {"name": name,
                 "arguments": {"command": "kill -9 4711"}}]},
                               "HOOK_MISMATCH", "LGV-031")

    def test_json_string_encoded_unknown_name_envelope(self):
        # F-009: JSON-string-encoded unknown-name envelope
        envelope = json.dumps({"calls": [
            {"name": "shell_exec",
             "arguments": {"command": "kill -9 4711"}}]})
        self.assert_refuse("tool_call", {"calls": envelope},
                           "HOOK_MISMATCH", "LGV-031")

    def test_nested_unknown_name_envelope(self):
        # F-009: unknown-name call nested in a recognized tool_call
        nested = {"name": "tool_call", "arguments": {"calls": [
            {"name": "os_system",
             "arguments": {"command": "kill -9 4711"}}]}}
        self.assert_refuse("tool_call", {"calls": [nested]},
                           "HOOK_MISMATCH", "LGV-031")

    def test_top_level_empty_or_unknown_name_fails_closed(self):
        # Edge Cases + F-005: a hook invocation delivering an empty or
        # unrecognized TOP-LEVEL tool name with executable-looking
        # arguments is a contract mismatch → fail closed (LGV-031)
        self.assert_refuse("", {"command": "kill -9 4711"},
                           "HOOK_MISMATCH", "LGV-031")
        self.assert_refuse("not_a_tool", {"command": "kill -9 4711"},
                           "HOOK_MISMATCH", "LGV-031")

    def test_readonly_name_case_variant_passes_without_args(self):
        # a case-variant read-only name with NO executable-looking
        # arguments is not a refusal (no hazard to attribute)
        self.assertIsNone(self.ev("Read_File", {"path": "x"}))

    # -- F-017: self-directed disable/uninstall + plugin-dir mutation ----
    def test_self_disable_uninstall_commands(self):
        for cmd in [
            "hermes plugins disable proxy-lifecycle-guard",
            "hermes plugins uninstall proxy-lifecycle-guard",
            "hermes plugin disable proxy-lifecycle-guard",
            "hermes --profile x plugins disable proxy-lifecycle-guard",
        ]:
            self.assert_veto("terminal", {"command": cmd})

    def test_plugin_directory_mutation_commands(self):
        for cmd in [
            "rm -rf ~/.hermes/plugins/proxy-lifecycle-guard",
            "mv ~/.hermes/plugins/proxy-lifecycle-guard /tmp",
            ("cp /tmp/evil.py "
             "~/.hermes/plugins/proxy-lifecycle-guard/guard.py"),
            ("echo x > "
             "~/.hermes/plugins/proxy-lifecycle-guard/config.json"),
            ("echo x > "
             "~/.hermes/plugins/proxy-lifecycle-guard/integrity.sha256"),
            ("tee ~/.hermes/plugins/proxy-lifecycle-guard/policy.json "
             "<<< x"),
        ]:
            self.assert_veto("terminal", {"command": cmd})

    def test_plugin_directory_mutation_via_python(self):
        self.assert_veto("execute_code", {"code": textwrap.dedent("""\
            from pathlib import Path
            Path('~/.hermes/plugins/proxy-lifecycle-guard/guard.py').write_text('x')""")})

    # -- US1-S7: double-wrapped envelopes (sibling baseline) --------------
    def test_double_wrapped_string_envelope_unwraps(self):
        inner = {"name": "terminal", "arguments": {"command": "echo ok"}}
        self.assert_allowed("tool_call",
                            {"calls": json.dumps({"calls": [inner]})})
        bad = {"name": "terminal",
               "arguments": {"command": "kill -9 4711"}}
        self.assert_veto("tool_call", {"calls": json.dumps({"calls": [bad]})})
        nested = json.dumps({"calls": json.dumps({"calls": [inner]})})
        self.assert_allowed("tool_call", {"calls": nested})

    def test_nameless_object_string_still_blocks(self):
        # Rule 1: a nameless call (empty name) with executable-looking
        # arguments is HOOK_MISMATCH — never a silent pass
        nameless = json.dumps({"arguments": {"command": "kill -9 4711"}})
        self.assert_refuse("tool_call", {"calls": nameless},
                           "HOOK_MISMATCH", "LGV-031")


class TestAllowPathBattery(GuardHarness):
    """T002: allow-path and refusal-category battery.

    Cites FR-002, FR-004, FR-007, FR-013, US2-S1/S2/S4, US3-S3, SC-002,
    SC-004, SC-008; reproduces F-011, F-012, F-013, F-014.
    """

    # -- US2-S1: read-only diagnostics matrix ----------------------------
    def test_readonly_diagnostics_pass(self):
        for cmd in [
            "ps aux | grep -i litellm",
            "launchctl list | grep litellm",
            "launchctl print gui/501/example.litellm",
            "brew services list",
            "curl -s http://localhost:4000/health/liveness",
            "lsof -nP -iTCP:4000 -sTCP:LISTEN",
            "kill -0 4711",
            "ps -p 4711 -o pid=,pgid=,comm=,args=",
            "pgrep -f litellm",
            "pgrep -l litellm",
        ]:
            self.assert_allowed("terminal", {"command": cmd})

    # -- F-014: signal-0 carve-out on the Python surface ------------------
    def test_signal0_python_surface_passes(self):
        for code in [
            "import os\nos.kill(4711, 0)",
            "import signal, os\nos.kill(4711, signal.SIG_ZERO)",
            "import os\nos.killpg(4711, 0)",
        ]:
            self.assert_allowed("execute_code", {"code": code})

    def test_signal0_python_nonzero_still_vetoes(self):
        self.assert_veto("execute_code",
                         {"code": "import os\nos.kill(4711, 9)"})
        self.assert_veto("execute_code",
                         {"code": "import os\nos.kill(4711, signal.SIGKILL)"})

    # -- US2-S4 / F-013: own-job pattern kills cleared by snapshot proof --
    def test_own_job_pattern_kill_cleared_by_snapshot(self):
        for cmd in [
            "pkill -f my-own-audit-job",
            "pkill -f audit-not-litellm",
            "killall my-own-tool",
            "pkill -f own-cron-cleanup",
        ]:
            self.assert_allowed("terminal", {"command": cmd})

    def test_own_job_bash_prefix_name_not_blanket_blocked(self):
        # F-013: names containing "bash" were blanket-blocked as broad
        # selectors; snapshot proof (pattern absent from proxy identity)
        # must run BEFORE blanket rules
        self.assert_allowed("terminal", {"command": "pkill -f my-bash-helper"})
        self.assert_allowed("terminal", {"command": "killall my-bash-helper"})
        self.assert_allowed("terminal", {"command": "pkill -f job.bash.scripts"})

    def test_own_job_dotted_module_path_not_regex_blocked(self):
        # F-013: a lone dot is a literal, not a regex metacharacter
        self.assert_allowed("terminal", {"command": "pkill -f tools.myjob.worker"})
        self.assert_allowed("terminal", {"command": "pkill -f my.tool.job"})

    def test_pattern_kill_still_vetoes_when_pattern_matches_proxy(self):
        for cmd in [
            "pkill -f litellm",
            "pkill -f 'litellm.*proxy'",
            "killall litellm",
            "pkill -f example.litellm",
        ]:
            self.assert_veto("terminal", {"command": cmd})

    # -- US2-S2 / F-012: large-but-innocent, FR-007 decision table --------
    # Bound values from REV3_BOUNDS (policy-schema.json caps):
    # input_text_bytes=262144, max_chunks=512, max_depth=8.
    def test_large_innocent_inputs_pass(self):
        # FR-007 row (a): OVER the bounds but provably intent-free by a
        # bounded head-scan → ALLOW (F-012).
        # over input_text_bytes (280,000 chars)
        text = "x" * 280000
        self.assert_allowed("terminal",
                            {"command": f"printf '%s' {text} > /dev/null"})
        # over max_chunks (600-file bulk git mv ≈ 1,800 segments)
        moves = " ".join(f"f{i}.txt f{i}.final" for i in range(600))
        self.assert_allowed("terminal", {"command": f"git mv {moves}"})
        # legacy-token-count regression (70-package pip install)
        pkgs = " ".join(f"package-{i}" for i in range(70))
        self.assert_allowed("terminal", {"command": f"pip install {pkgs}"})

    def test_large_innocent_deferred_batch_passes(self):
        # F-012: 600-call innocent batch OVER max_chunks=512 → row (a)
        # bounded analysis allows
        call = {"name": "process_manage",
                "arguments": {"action": "poll", "session_id": "s"}}
        self.assert_allowed("tool_call", {"calls": [call] * 600})

    def test_large_intent_carrying_input_refuses_at_bound(self):
        # FR-007 row (b): over input_text_bytes WITH kill-family intent
        # anywhere inspectable → the veto is never lost to the bound
        intent = "pkill -f litellm; " * 22000
        assert len(intent) > REV3_BOUNDS["input_text_bytes"]
        self.assert_veto("terminal", {"command": intent})

    def _deep_payload(self, depth):
        payload = {"command": "echo ok"}
        for _ in range(depth):
            payload = {"calls": json.dumps(
                [{"name": "tool_call", "arguments": payload}])}
        return payload

    def test_unprovable_overbound_shape_refuses_with_category(self):
        # FR-007 row (c): over-bound, unprovable → refuse with THAT
        # BOUND's category + corrective guidance (US3-S3)
        # size bound: over input_text_bytes (262144) with unresolved
        # shell constructs → BOUND_SIZE
        big_text = ("unclassifiable ${a[$((i++))]} construct; " * 7000)
        self.assertGreater(len(big_text.encode("utf-8")),
                           REV3_BOUNDS["input_text_bytes"])
        self.assert_refuse("terminal", {"command": big_text},
                           "BOUND_SIZE", "LGV-010")
        # depth bound: nested envelopes beyond max_depth=8
        self.assert_refuse("tool_call", self._deep_payload(12),
                           "BOUND_DEPTH", "LGV-011")
        # count bound: batch over max_chunks whose tail is NOT provably
        # intent-free (an execution-tool call in the uninspectable
        # remainder) → BOUND_COUNT
        benign = {"name": "process_manage",
                  "arguments": {"action": "poll", "session_id": "s"}}
        unprovable = {"name": "terminal",
                      "arguments": {"command": "echo tail"}}
        self.assert_refuse("tool_call",
                           {"calls": [benign] * 600 + [unprovable]},
                           "BOUND_COUNT", "LGV-012")

    def test_self_correctable_resubmission(self):
        # SC-008: refusal → corrected resubmission succeeds; the refusal
        # carries per-category corrective guidance (BOUND_COUNT → reduce
        # the batch size; BOUND_DEPTH → reduce the nesting)
        benign = {"name": "process_manage",
                  "arguments": {"action": "poll", "session_id": "s"}}
        unprovable = {"name": "terminal",
                      "arguments": {"command": "echo tail"}}
        result = self.ev("tool_call",
                         {"calls": [benign] * 600 + [unprovable]})
        self.assertIsNotNone(result)
        self.assertEqual(result.get("category"), "BOUND_COUNT")
        self.assertIn("batch", result.get("message", "").lower())
        # corrected resubmission: benign batch, in-bound → allow
        self.assert_allowed("tool_call", {"calls": [benign] * 100})

    def test_malformed_sibling_blast_radius_documented(self):
        # F-015: one malformed sibling refuses the whole envelope (stated
        # trade-off); the refusal identifies the reason so an unattended
        # job can repair and resubmit. A nameless sibling is rule-1
        # HOOK_MISMATCH; the blast radius is the documented cost.
        good = {"name": "process_manage",
                "arguments": {"action": "poll", "session_id": "s"}}
        bad = {"arguments": {"command": "kill -9 4711"}}  # nameless
        result = self.ev("tool_call", {"calls": [good, bad, good]})
        self.assertIsNotNone(result)
        self.assertEqual(result.get("category"), "HOOK_MISMATCH")
        self.assertEqual(result.get("log_code"), "LGV-031")

    # -- malformed/failed discovery refusal matrix (S-09) ------------------
    def test_malformed_discovery_refuses_both_directions(self):
        # Edge Cases: failed discovery (tool missing/timeout) refuses
        # conservatively; a determinate EMPTY discovery is distinct —
        # unrelated literal kill passes, pattern kill against proxy
        # identity still vetoes (restart-transition boundary).
        def failed_discover(port):
            return None  # discovery failure
        self.assert_veto("terminal", {"command": "kill 4711"},
                         discover=failed_discover)
        self.assert_veto("terminal", {"command": "pkill -f narrow-pattern"},
                         discover=failed_discover)
        # determinate empty
        def empty_discover(port):
            return guard.Snapshot()  # determinate empty
        self.assertIsNone(guard.evaluate(
            "terminal", {"command": "kill 99999"},
            self.policy, discover=empty_discover))
        self.assert_veto("terminal", {"command": "pkill -f litellm"},
                         discover=empty_discover)

    # -- hook-contract rule 6: plugin-emitted log lines --------------------
    def test_plugin_emits_own_log_line_per_veto_refuse(self):
        with self.assertLogs("proxy_lifecycle_guard", level="INFO") as logged:
            self.assert_veto("terminal", {"command": "kill -9 4711"})
        self.assertTrue(any("LGV-001" in line for line in logged.output),
                        f"no LGV-001 in logged output: {logged.output}")
        # a BOUND refusal emits its category's code too (rule 6)
        benign = {"name": "process_manage",
                  "arguments": {"action": "poll", "session_id": "s"}}
        unprovable = {"name": "terminal",
                      "arguments": {"command": "echo tail"}}
        with self.assertLogs("proxy_lifecycle_guard", level="INFO") as logged:
            self.assert_refuse("tool_call",
                               {"calls": [benign] * 600 + [unprovable]},
                               "BOUND_COUNT", "LGV-012")
        self.assertTrue(any("LGV-012" in line for line in logged.output),
                        f"no LGV-012 in logged output: {logged.output}")


class TestFailClosedTamperBattery(GuardHarness):
    """T003: fail-closed and tamper battery.

    Cites FR-005, FR-008, FR-015, FR-013, US1-S8, US3-S1/S2, SC-003;
    reproduces F-005, F-016, F-019, F-021, F-025.
    """

    def _unreadable(self, d):
        p = write_policy(d)
        p.chmod(0o000)
        self.addCleanup(lambda: p.chmod(0o644))
        return p

    def _malformed(self, d):
        # malformed policy with a MATCHING integrity reference over the
        # malformed bytes — isolates schema validation from ref failure
        p = Path(d) / "policy.json"
        p.write_text("not json at all {{{", encoding="utf-8")
        write_integrity_ref(d, policy_bytes=p.read_bytes())
        return p

    def _schema_invalid(self, d):
        # schema-invalid policy with an integrity reference recorded for
        # the INVALID content (digest matches what's on disk; the schema
        # itself is broken) — isolates schema validation from tamper
        p = write_policy(d)
        obj = json.loads(p.read_text())
        obj["protected_labels"] = "not-a-list"
        p.write_text(json.dumps(obj), encoding="utf-8")
        write_integrity_ref(d)  # re-record digest of the broken policy
        return p

    def _wrong_version(self, d):
        p = write_policy(d)
        obj = json.loads(p.read_text())
        obj["schema_version"] = 2
        p.write_text(json.dumps(obj), encoding="utf-8")
        write_integrity_ref(d)
        return p

    def _tampered(self, d):
        # schema-valid policy whose digest no longer matches the
        # owner-recorded integrity reference (F-016/F-025)
        p = write_policy(d)
        obj = json.loads(p.read_text())
        obj["protected_port"] = 1  # schema-valid but disarming
        p.write_text(json.dumps(obj), encoding="utf-8")
        return p

    def test_fail_closed_split(self):
        # FR-005/SC-003: executable calls blocked with POLICY_INVALID,
        # file reads and process-status diagnostics pass
        variants = [
            ("missing", lambda d: Path(d) / "nope.json"),
            ("unreadable", self._unreadable),
            ("malformed", self._malformed),
            ("schema_invalid", self._schema_invalid),
            ("wrong_version", self._wrong_version),
            ("tampered", self._tampered),
        ]
        for label, factory in variants:
            with self.subTest(failure=label):
                # fresh dir per variant: the unreadable variant chmods the
                # policy to 000, which must not poison later variants
                fresh = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, fresh, ignore_errors=True)
                policy = factory(fresh)
                for tool, args in [
                    ("terminal", {"command": "echo hi"}),
                    ("execute_code", {"code": "print(1)"}),
                    ("browser_exec", {"code": "print(1)"}),
                    ("tool_call", {"calls": [{"name": "terminal",
                                              "arguments": {"command": "echo"}}]}),
                    ("process_manage", {"action": "kill",
                                        "session_id": "fixture-session"}),
                ]:
                    result = guard.evaluate(tool, args, policy,
                                            discover=self.discover_fake)
                    self.assertIsNotNone(result,
                                         f"{label}/{tool} must fail closed")
                    self.assertEqual(result.get("category"), "POLICY_INVALID",
                                     f"{label}/{tool} category")
                    self.assertEqual(result.get("log_code"), "LGV-030")
                # the split: reads and read-only process status pass
                self.assertIsNone(guard.evaluate(
                    "read_file", {"path": "x"}, policy))
                self.assertIsNone(guard.evaluate(
                    "process_manage", {"action": "status",
                                       "session_id": "s"},
                    policy, discover=self.discover_fake))

    # FR-015/D3: the integrity-reference failure category, pinned IN this
    # battery (spec pins existence, not the name; T005 implements it).
    # INTEGRITY_REF_INVALID / LGV-032 = next free code after LGV-031.
    INTEGRITY_CATEGORY = "INTEGRITY_REF_INVALID"
    INTEGRITY_LOG_CODE = "LGV-032"

    def test_integrity_reference_failure_classes(self):
        # FR-015: a missing, unreadable, or unparseable integrity
        # reference is ITS OWN fail-closed category with a documented
        # owner repair action — distinct from the digest-mismatch
        # POLICY_INVALID class (T005 wires the category).
        p = write_policy(self.tmp.name)
        ref = Path(self.tmp.name) / "integrity.sha256"
        # missing
        ref.unlink()
        self.assert_refuse("terminal", {"command": "echo hi"},
                           self.INTEGRITY_CATEGORY,
                           self.INTEGRITY_LOG_CODE, policy=p)
        # unparseable
        ref.write_text("garbage not json", encoding="utf-8")
        self.assert_refuse("terminal", {"command": "echo hi"},
                           self.INTEGRITY_CATEGORY,
                           self.INTEGRITY_LOG_CODE, policy=p)
        # unreadable
        ref.write_text(json.dumps({"policy_digest": "0" * 64,
                                   "source_digests": {}}),
                       encoding="utf-8")
        ref.chmod(0o000)
        self.addCleanup(lambda: ref.chmod(0o644))
        self.assert_refuse("terminal", {"command": "echo hi"},
                           self.INTEGRITY_CATEGORY,
                           self.INTEGRITY_LOG_CODE, policy=p)

    def test_incomplete_source_digest_set_fails_closed(self):
        # codex review P2: an empty or partial source_digests map must
        # NOT skip deployed-source verification — a ref that omits a
        # required source digest is rejected (POLICY_INVALID), so a
        # divergent deployed copy can never pass as untampered.
        p = write_policy(self.tmp.name)
        ref = json.loads(
            (Path(self.tmp.name) / "integrity.sha256").read_text())
        for label, digests in [
            ("empty", {}),
            ("partial_guard_only", {"guard.py": ref["source_digests"]
                                             ["guard.py"]}),
            ("partial_adapter_only", {"__init__.py": ref["source_digests"]
                                               ["__init__.py"]}),
        ]:
            with self.subTest(shape=label):
                bad = dict(ref)
                bad["source_digests"] = digests
                ref_path = Path(self.tmp.name) / "integrity.sha256"
                ref_path.write_text(json.dumps(bad, indent=2),
                                    encoding="utf-8")
                # pin policy=p: self.ev would regenerate the policy AND
                # a complete ref, silently discarding the crafted bad one
                result = self.ev("terminal", {"command": "echo hi"},
                                 policy=p)
                self.assertIsNotNone(result)
                self.assertEqual(result.get("category"), "POLICY_INVALID")
                self.assertEqual(result.get("log_code"), "LGV-030")
        # restore the complete ref
        ref_path = Path(self.tmp.name) / "integrity.sha256"
        ref_path.write_text(json.dumps(ref, indent=2), encoding="utf-8")
        self.assert_allowed("terminal", {"command": "echo hi"}, policy=p)

    def test_registration_self_check_against_owner_recorded_digests(self):
        # F-019: the self-check compares the DEPLOYED guard source against
        # the owner-recorded source digests in integrity.sha256 — a
        # tampered deployed guard.py must fail closed at load, with no
        # kit-checkout runtime dependency. This test LOADS the deployed
        # copy as its own module (not the workspace guard) so a tampered
        # deployed source is actually exercised.
        plugin_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, plugin_dir, ignore_errors=True)
        deployed = plugin_dir / "guard.py"
        deployed.write_bytes(MODULE_PATH.read_bytes())
        adapter = plugin_dir / "__init__.py"
        adapter.write_bytes((MODULE_PATH.parent / "__init__.py").read_bytes())
        policy = write_policy(plugin_dir, policy_name="config.json")
        write_integrity_ref(plugin_dir, policy_name="config.json",
                            sources=(deployed, adapter))

        def load_deployed(tag):
            spec = importlib.util.spec_from_file_location(
                f"deployed_guard_{tag}", deployed)
            if spec is None or spec.loader is None:
                raise RuntimeError("unable to load deployed guard copy")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            return mod

        # untampered deployed source: loads and guards normally
        ok_mod = load_deployed("ok")
        self.assertIsNone(ok_mod.evaluate(
            "terminal", {"command": "echo ok"}, policy,
            discover=self.discover_fake))
        # tamper the deployed source; the owner's reference still records
        # the ORIGINAL digest → the next evaluation fails closed
        # (POLICY_INVALID class: deployed source vs owner-recorded digests)
        deployed.write_bytes(MODULE_PATH.read_bytes() + b"\n# tamper\n")
        result = ok_mod.evaluate(
            "terminal", {"command": "echo ok"}, policy,
            discover=self.discover_fake)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("category"), "POLICY_INVALID")
        # and a FRESH registration of the tampered source fails closed
        # on its first guarded call too
        bad_mod = load_deployed("tampered")
        result = bad_mod.evaluate(
            "terminal", {"command": "echo ok"}, policy,
            discover=self.discover_fake)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("category"), "POLICY_INVALID")

    def test_per_call_reload_semantics(self):
        # FR-008/F-021: an owner's policy edit is visible on the NEXT
        # guarded call; no caching across calls or plugin lifetime.
        # Probe: session-handle protection flip via process_manage.
        p = write_policy(self.tmp.name, sessions=("fixture-session",))
        self.assert_veto("process_manage",
                         {"action": "kill", "session_id": "fixture-session"},
                         policy=p)
        obj = json.loads(p.read_text())
        obj["protected_session_handles"] = ["other-session"]
        p.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        write_integrity_ref(self.tmp.name)  # owner re-establishes integrity
        # next call must see the new policy: fixture-session no longer
        # protected → allow; other-session now protected → veto (LGV-001)
        self.assertIsNone(guard.evaluate(
            "process_manage", {"action": "kill", "session_id": "fixture-session"},
            p, discover=self.discover_fake),
            "policy edit must take effect on the next call")
        self.assert_veto("process_manage",
                         {"action": "kill", "session_id": "other-session"},
                         policy=p)

    def test_tamper_evidence_runs_every_load(self):
        # FR-015/F-025: tampering caught on the very next evaluation
        p = write_policy(self.tmp.name)
        self.assertIsNone(self.ev("terminal", {"command": "echo ok"},
                                  policy=p))
        obj = json.loads(p.read_text())
        obj["protected_labels"] = []
        p.write_text(json.dumps(obj), encoding="utf-8")
        # integrity reference still records the OLD digest → tampered
        result = self.ev("terminal", {"command": "echo ok"}, policy=p)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("category"), "POLICY_INVALID")
        self.assertEqual(result.get("log_code"), "LGV-030")

    def test_owner_only_permissions_checked_at_load(self):
        # FR-009/S-04: owner-only permissions verified on the policy file
        # AND the integrity reference (group/world-writable fails closed)
        p = write_policy(self.tmp.name)
        ref = Path(self.tmp.name) / "integrity.sha256"
        ref.chmod(0o666)  # group-writable → must fail closed
        self.addCleanup(lambda: ref.chmod(0o600))
        result = guard.evaluate("terminal", {"command": "echo hi"}, p,
                                discover=self.discover_fake)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("category"), "POLICY_INVALID")
        # policy file world-writable → must also fail closed
        write_integrity_ref(self.tmp.name)  # restore a valid ref first
        ref.chmod(0o600)
        p.chmod(0o666)
        result = guard.evaluate("terminal", {"command": "echo hi"}, p,
                                discover=self.discover_fake)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("category"), "POLICY_INVALID")


class TestMutationChecksBattery(GuardHarness):
    """T013: Constitution-III mutation checks (FR-013, SC-006).

    Each check DISABLES a matcher branch or the registration path in an
    ISOLATED copy (repo bytes never touched) and proves the batteries
    measure the REAL guard by asserting outcomes flip:
      - control-flow branch disabled → the cf-wrapped veto fixtures
        flip veto→allow, while direct kills STAY blocked (the mutation
        is precise: only the wrapped-verb scan is gone, not the guard);
      - registration disabled → no hook is registered and a protected
        lifecycle probe is NOT blocked (guard absent, SC-006).
    The isolated copy carries its own owner-established integrity
    reference over the MUTATED bytes (the fixture acts as the owner);
    the deployed-source digest self-check therefore passes for the copy
    and the mutation's effect is measured, not a tamper trip.
    """

    # the two halves of the control-flow branch (F-001/F-006), as
    # unique single-occurrence anchors in guard.py
    CF_MUTATIONS = (
        ('    chunk = _normalize_control_flow(chunk)',
         '    chunk = chunk  # MUTATION: cf segmentation disabled'),
        ("def _strip_control_flow(tokens):",
         "def _strip_control_flow(tokens):\n    return tokens  # MUTATION: cf strip disabled"),
    )
    REG_MUTATION = (
        ('    ctx.register_hook("pre_tool_call", before_tool)',
         '    pass  # MUTATION: registration disabled'),
    )
    # cf-wrapped fixtures from the T001 veto battery that must ALL flip
    CF_FLIP_FIXTURES = (
        "if true; then kill -9 4711; fi",
        'if [ -n "$X" ]; then pkill -f litellm; fi',
        "if grep -q litellm x; then killall litellm; fi",
        "while true; do kill -9 4711; done",
        "while read p; do kill -9 $p; done",
        "for p in $(lsof -ti:4000); do kill -9 $p; done",
        "for i in 1 2 3; do pkill -f litellm; done",
        "case $x in a) kill -9 4711;; b) echo ok;; esac",
        "case $x in a) pkill litellm;; esac",
        "{ kill -9 4711; }",
        "{ pkill -f litellm; }",
        "! kill -9 4711",
        "if true; then { kill -9 4711; }; fi",
        "time -p kill -9 4711",
        "time -p pkill -f litellm",
    )
    # direct kills that must stay BLOCKED under the cf mutation —
    # proving the mutation flipped only the wrapped-verb scan branch,
    # not the whole guard (mutation precision)
    DIRECT_KEEP_FIXTURES = (
        "kill -9 4711",
        "pkill -f litellm",
        "killall litellm",
    )

    def _stage_isolated(self, tag, guard_text=None, adapter_text=None):
        """Isolated copy of the plugin under an owner-established ref.

        A fresh temp dir receives guard.py (verbatim or mutated) and
        optionally __init__.py; the fixture acts as the owner and
        establishes integrity.sha256 over the staged bytes. Repo and
        deployed bytes are never modified.
        """
        plugin_dir = Path(tempfile.mkdtemp(prefix=f"guard-mut-{tag}-"))
        self.addCleanup(self._cleanup_fixture_directory, plugin_dir)
        (plugin_dir / "guard.py").write_text(
            guard_text if guard_text is not None
            else MODULE_PATH.read_text(encoding="utf-8"))
        # the integrity reference must cover BOTH deployed sources (the
        # guard requires a complete digest set); stage the adapter
        # verbatim when the test isn't mutating it
        (plugin_dir / "__init__.py").write_text(
            adapter_text if adapter_text is not None
            else (MODULE_PATH.parent / "__init__.py").read_text(
                encoding="utf-8"))
        policy = write_policy(plugin_dir, policy_name="config.json")
        write_integrity_ref(plugin_dir, policy_name="config.json",
                            sources=(plugin_dir / "guard.py",
                                     plugin_dir / "__init__.py"))
        return plugin_dir

    def _load_module(self, path, name):
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"unable to load isolated module {name}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    def _apply_mutations(self, text, mutations):
        for old, new in mutations:
            count = text.count(old)
            self.assertEqual(count, 1,
                             f"mutation anchor not unique ({count}): {old!r}")
            text = text.replace(old, new)
        return text

    def _fake_discover(self, mod):
        snap = mod.Snapshot(
            pids=frozenset({FIXTURE_PID}), pgids=frozenset({FIXTURE_PID}),
            comms=frozenset({"litellm"}), args=("litellm --port 4000",))
        return lambda port: snap

    def test_quoted_payload_regression_is_caught_in_isolated_copy(self):
        # Restore the old blanket substitution only in an isolated plugin.
        # Both outright refusal and silent analysis-byte corruption must fail
        # the same preservation invariant as the quotation regression tests.
        original = MODULE_PATH.read_text(encoding="utf-8")
        mutated = self._apply_mutations(original, (
            ('    chunk = _normalize_control_flow(chunk)',
             '    chunk = _CF_SUB_RE.sub(";", chunk)'),
        ))
        pristine_dir = self._stage_isolated("quote-pristine")
        mutant_dir = self._stage_isolated("quote-regressed", guard_text=mutated)
        pristine = self._load_module(pristine_dir / "guard.py", "quote_pristine")
        mutant = self._load_module(mutant_dir / "guard.py", "quote_regressed")

        def assert_preserved(mod, directory, program):
            import shlex
            seen = []
            analyze = mod._analyze_code_text

            def observe(code, policy, snapshot):
                seen.append(code)
                return analyze(code, policy, snapshot)

            mod._analyze_code_text = observe
            try:
                decision = mod.evaluate(
                    "terminal", {"command": "python3 -c " + shlex.quote(program)},
                    directory / "config.json", discover=self._fake_discover(mod))
                self.assertIsNone(decision)
                self.assertEqual(seen, [program])
            finally:
                mod._analyze_code_text = analyze

        for program in ("print(1 if True else 0)", "print('else')"):
            assert_preserved(pristine, pristine_dir, program)
            with self.assertRaises(AssertionError):
                assert_preserved(mutant, mutant_dir, program)
        word_regressed = self._apply_mutations(original, (
            ('normalize = (not in_word', 'normalize = (True'),
        ))
        word_dir = self._stage_isolated("word-regressed", guard_text=word_regressed)
        word_mutant = self._load_module(word_dir / "guard.py", "word_regressed")
        data = "echo ''else"
        self.assertEqual(pristine._normalize_control_flow(data), data)
        with self.assertRaises(AssertionError):
            self.assertEqual(word_mutant._normalize_control_flow(data), data)
        self.assertEqual(MODULE_PATH.read_text(encoding="utf-8"), original)

    def test_cf_branch_disabled_flips_wrapped_vetoes_to_allow(self):
        # T013/FR-013/SC-006: with the control-flow branch disabled in an
        # isolated copy, every cf-wrapped fixture that the T001 battery
        # proves vetoed MUST flip to allow — the battery measures the
        # real control-flow scan, not a constant-false matcher.
        mutated = self._apply_mutations(
            MODULE_PATH.read_text(encoding="utf-8"), self.CF_MUTATIONS)
        d_pristine = self._stage_isolated("cf-pristine")
        d_mutated = self._stage_isolated("cf-mutated", guard_text=mutated)
        m_pristine = self._load_module(d_pristine / "guard.py",
                                       "guard_mut_cf_pristine")
        m_mutated = self._load_module(d_mutated / "guard.py",
                                      "guard_mut_cf_mutated")
        disc_p = self._fake_discover(m_pristine)
        disc_m = self._fake_discover(m_mutated)
        for cmd in self.CF_FLIP_FIXTURES:
            with self.subTest(command=cmd):
                vetoed = m_pristine.evaluate(
                    "terminal", {"command": cmd},
                    d_pristine / "config.json", discover=disc_p)
                allowed = m_mutated.evaluate(
                    "terminal", {"command": cmd},
                    d_mutated / "config.json", discover=disc_m)
                self.assertIsNotNone(
                    vetoed, f"pristine must veto {cmd!r} (battery premise)")
                self.assertIsNone(
                    allowed,
                    f"cf-disabled guard must allow {cmd!r} — battery "
                    f"measures the real control-flow branch")

    def test_cf_branch_disabled_keeps_direct_kills_blocked(self):
        # mutation precision: the cf mutation must not disable the whole
        # guard — direct kills stay vetoed, so the flip set is causal,
        # not a blanket "guard off" artifact.
        mutated = self._apply_mutations(
            MODULE_PATH.read_text(encoding="utf-8"), self.CF_MUTATIONS)
        d_mutated = self._stage_isolated("cf-mutated-direct", guard_text=mutated)
        m_mutated = self._load_module(d_mutated / "guard.py",
                                      "guard_mut_cf_direct")
        disc_m = self._fake_discover(m_mutated)
        for cmd in self.DIRECT_KEEP_FIXTURES:
            with self.subTest(command=cmd):
                result = m_mutated.evaluate(
                    "terminal", {"command": cmd},
                    d_mutated / "config.json", discover=disc_m)
                self.assertIsNotNone(
                    result, f"cf-disabled guard must STILL block {cmd!r}")

    def test_registration_disabled_leaves_guard_absent(self):
        # T013/FR-013/SC-006: with ctx.register_hook disabled in an
        # isolated copy, NOTHING is registered and a protected lifecycle
        # probe runs unvetoed — a disabled/unregistered plugin
        # demonstrably stops blocking (guard absent). The pristine copy
        # registers once and vetoes the identical probe.
        adapter = (MODULE_PATH.parent / "__init__.py").read_text(
            encoding="utf-8")
        mutated_adapter = self._apply_mutations(adapter, self.REG_MUTATION)
        d_pristine = self._stage_isolated("reg-pristine",
                                          adapter_text=adapter)
        d_mutated = self._stage_isolated(
            "reg-mutated", guard_text=MODULE_PATH.read_text(encoding="utf-8"),
            adapter_text=mutated_adapter)
        from unittest.mock import Mock
        results = {}
        for label, d in (("pristine", d_pristine), ("mutated", d_mutated)):
            name = f"guard_adapter_mut_{label}"
            spec = importlib.util.spec_from_file_location(
                name, d / "__init__.py",
                submodule_search_locations=[str(d)])
            if spec is None or spec.loader is None:
                raise RuntimeError(f"unable to load isolated adapter {name}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            ctx = _RecordingContext(self)
            mod.register(ctx)
            ctx.capture_guard(sys.modules[mod.__name__ + ".guard"])
            # identity-match probe: vetoes without live discovery; the
            # "false &&" branch keeps it non-destructive if the hook is
            # missing (SC-005 probe discipline)
            probe = "echo guard-probe-marker && false && pkill -f litellm"
            blocked = None
            if ctx.register_hook.call_count:
                cb = ctx.register_hook.call_args.args[1]
                blocked = cb(tool_name="terminal", args={"command": probe})
            results[label] = (ctx.register_hook.call_count, blocked)
            del sys.modules[name]
        calls_p, blocked_p = results["pristine"]
        calls_m, blocked_m = results["mutated"]
        self.assertEqual(calls_p, 1, "pristine registers exactly one hook")
        self.assertIsNotNone(blocked_p,
                             "pristine must veto the probe (matcher + hook)")
        self.assertEqual(calls_m, 0,
                         "registration-disabled adapter registers nothing")
        self.assertIsNone(blocked_m,
                          "guard absent: the probe is not blocked (SC-006)")

    def test_mutated_copies_leave_source_bytes_unchanged(self):
        # the isolated-copy discipline itself is guarded: the mutations
        # never touch the repo checkout (FR-014 parity evidence stays
        # meaningful), and the staged dirs carry an owner-established
        # integrity ref over the mutated bytes so the guard loads.
        before = MODULE_PATH.read_bytes()
        self.test_cf_branch_disabled_flips_wrapped_vetoes_to_allow()
        self.test_cf_branch_disabled_keeps_direct_kills_blocked()
        self.test_registration_disabled_leaves_guard_absent()
        self.assertEqual(
            MODULE_PATH.read_bytes(), before,
            "mutation checks must never modify the repo guard source")
        adapter = MODULE_PATH.parent / "__init__.py"
        self.assertEqual(
            adapter.read_bytes(),
            adapter.read_bytes(),
            "mutation checks must never modify the repo adapter source")


class TestShapeEnvelopeBattery(GuardHarness):
    """Red-first battery for guard-hardening (amendment 3a) + guard-hardening.

    Pins the spec contract, never the implementation:
    - Amendment 3a (spec.md Clarification 2026-11-04): an inner deferred
      call lacking the canonical name/arguments key pair is UNPROVABLE —
      refused SHAPE_ENVELOPE (LGV-020) when it carries executable-looking
      content; never eligible for FR-007 row (a) benign-tail analysis.
    - guard-hardening: a calls/arguments STRING that fails json.loads is an
      unprovable envelope shape — refused SHAPE_ENVELOPE (LGV-020) with
      corrective guidance, NOT VETO/LGV-001's kill-workflow message.
    Allow-controls pin the canonical shapes that must keep passing.
    """

    # ---- amendment 3a: non-canonical inner-call key shapes ---------------

    def test_noncanonical_tool_args_keys_executable_refused(self):
        # the guard-hardening finding: 'tool'/'args' keys carrying a port-kill
        # pipeline were silently skipped. Must be SHAPE_ENVELOPE, never
        # a skip and never LGV-001.
        self.assert_refuse(
            "tool_call",
            {"calls": [{"tool": "terminal",
                        "args": {"command": "lsof -t -iTCP:4000 | xargs kill -9"}}]},
            "SHAPE_ENVELOPE", "LGV-020")

    def test_noncanonical_keys_extra_executable_content_refused(self):
        # extra keys carrying executable-looking content are also
        # unprovable (amendment 3a: "including unrecognized keys")
        self.assert_refuse(
            "tool_call",
            {"calls": [{"name": "terminal",
                        "arguments": {"command": "echo hi"},
                        "extra": {"command": "pkill -f litellm"}}]},
            "SHAPE_ENVELOPE", "LGV-020")

    def test_noncanonical_inert_extra_passes(self):
        # amendment 3a Edge Cases: a plain object with no executable-looking
        # content anywhere passes as an unrecognized-but-inert extra.
        self.assert_allowed(
            "tool_call",
            {"calls": [{"tool": "notion_create",
                        "args": {"page_id": "abc", "title": "Standup notes"}}]})

    def test_canonical_nameless_envelope_still_recurses(self):
        # F-009: nameless envelope-shaped body still recurses into calls
        self.assert_veto(
            "tool_call",
            {"calls": [{"calls": [{"name": "terminal",
                                   "arguments": {"command": "pkill -f litellm"}}]}]})

    # ---- guard-hardening second exposure: benign-tail hole ----------------------

    def test_noncanonical_call_not_benign_in_oversized_batch(self):
        # FR-007 row (c): a non-canonical shape in the uninspectable tail
        # of an oversized batch must refuse BOUND_COUNT, never ride the
        # row (a) benign-tail allow.
        benign_tail_call = {"tool": "notion_fetch", "args": {"id": "x"}}
        calls = [{"name": "read_file", "arguments": {"path": "a"}}] * 8
        calls.append(benign_tail_call)
        # bounds.max_chunks=8 via the bounds override — 9 calls put the
        # non-canonical shape in the tail beyond the head scan.
        self.assert_refuse("tool_call", {"calls": calls},
                           "BOUND_COUNT", "LGV-012",
                           bounds={"max_chunks": 8})

    def test_nameless_arguments_key_not_benign_in_oversized_batch(self):
        # M4 sweep escape (fixed 2026-11-04): a call with the canonical
        # `arguments` key but NO name scored benign under the pre-fix
        # logic. Absence of a attributable name is unprovable — row (c).
        calls = [{"name": "read_file", "arguments": {"path": "a"}}] * 8
        calls.append({"arguments": {"command": "pkill -f litellm"}})
        self.assert_refuse("tool_call", {"calls": calls},
                           "BOUND_COUNT", "LGV-012",
                           bounds={"max_chunks": 8})

    def test_extra_keys_tail_not_benign_in_oversized_batch(self):
        # M4 sweep escape (fixed 2026-11-04): canonical benign keys plus an
        # unprovable extra key must NOT score benign for tail analysis.
        calls = [{"name": "read_file", "arguments": {"path": "a"}}] * 8
        calls.append({"name": "read_file", "arguments": {"path": "b"},
                      "extra": {"command": "pkill -f litellm"}})
        self.assert_refuse("tool_call", {"calls": calls},
                           "BOUND_COUNT", "LGV-012",
                           bounds={"max_chunks": 8})

    def test_canonical_tail_still_allows_oversized_batch(self):
        # row (a) allow-control: same oversized batch with a CANONICAL
        # inert tail element still passes (amendment must not over-block).
        calls = [{"name": "read_file", "arguments": {"path": "a"}}] * 9
        self.assert_allowed("tool_call", {"calls": calls},
                            bounds={"max_chunks": 8})

    # ---- guard-hardening: non-decodable JSON strings ----------------------------

    def test_calls_string_decode_failure_is_shape_refusal(self):
        # hand-typed single-quoted JSON: json.loads fails. Must surface as
        # SHAPE_ENVELOPE with corrective guidance — the exact failure that
        # misled the 02:00 cron run into reading a kill-workflow message.
        bad = "[{'name': 'terminal', 'arguments': {'command': 'echo hi'}}]"
        self.assert_refuse("tool_call", {"calls": bad},
                           "SHAPE_ENVELOPE", "LGV-020")

    def test_inner_arguments_string_decode_failure_is_shape_refusal(self):
        bad_inner = "{'tool_id': 'x', 'arguments': {'tool_name': 'y'}}"
        self.assert_refuse(
            "tool_call",
            {"calls": [{"name": "mcp__local_mcp_aggregator__call_tool",
                        "arguments": bad_inner}]},
            "SHAPE_ENVELOPE", "LGV-020")

    # ---- allow-controls: canonical shapes must keep passing --------------

    def test_canonical_json_dumps_calls_string_allows(self):
        # the documented invocation pattern (json.dumps-built envelope)
        # must keep passing — the fix must not over-block canonical use.
        payload = json.dumps([{"name": "notion_fetch",
                               "arguments": {"id": "abc"}}])
        self.assert_allowed("tool_call", {"calls": payload})

    def test_canonical_dict_envelope_allows(self):
        self.assert_allowed(
            "tool_call",
            {"calls": [{"name": "notion_fetch", "arguments": {"id": "abc"}}]})

    def test_canonical_relay_triple_nesting_allows(self):
        # the shared-gateway relay shape from the cron reference: nested
        # tool_id + arguments dicts with no executable content.
        self.assert_allowed(
            "tool_call",
            {"calls": [{"name": "mcp__local_mcp_aggregator__call_tool",
                        "arguments": {
                            "tool_id": "relay-abc",
                            "arguments": {"tool_name": "aigateway_notion-x",
                                          "arguments": {"pages": [{
                                              "properties": {
                                                  "Title": {
                                                      "title": "Standup"
                                                  }}}]}}}}]})

    # ---- guard-hardening: inert plain lists under unknown keys ----------------

    def test_inert_plain_list_under_unknown_key_passes(self):
        # amended Edge Case: a list whose every element is plain
        # (string/number/inert dict, arbitrarily nested) is
        # unrecognized-but-inert and passes.
        for extra in (
            {"tags": ["a", "b"]},
            {"ids": [1, 2, 3]},
            {"meta": [{"k": "v"}, {"n": 1}]},
            {"matrix": [[1, 2], [3, 4]]},
            {"tags": []},
        ):
            call = {"name": "read_file", "arguments": {"path": "/tmp/x"}}
            call.update(extra)
            self.assert_allowed("tool_call", {"calls": [call]})

    def test_list_with_executable_dict_under_unknown_key_refused(self):
        # a list element carrying command/code (executable-looking keys)
        # makes the whole call unprovable — SHAPE_ENVELOPE, never allow.
        self.assert_refuse(
            "tool_call",
            {"calls": [{"name": "read_file",
                        "arguments": {"path": "/tmp/x"},
                        "history": [{"command": "lsof -t -iTCP:4000"
                                              " | xargs pk" + "ill -9"}]}]},
            "SHAPE_ENVELOPE", "LGV-020")

    def test_list_with_command_key_string_nested_refused(self):
        # same rule one level deeper: inert strings in the list do not
        # launder a nested executable dict.
        self.assert_refuse(
            "tool_call",
            {"calls": [{"name": "read_file",
                        "arguments": {"path": "/tmp/x"},
                        "history": ["echo a",
                                    {"command": "ki" + "ll -9 123"}]}]},
            "SHAPE_ENVELOPE", "LGV-020")

    def test_list_inert_regression_pins_still_hold(self):
        # pre-vf0 pins must be unchanged after the list relaxation:
        # inert string/dict values pass, command-key strings refuse.
        self.assert_allowed(
            "tool_call",
            {"calls": [{"name": "read_file", "arguments": {"path": "/tmp/x"},
                        "note": "hello", "meta": {"k": "v"}}]})
        self.assert_refuse(
            "tool_call",
            {"calls": [{"name": "read_file", "arguments": {"path": "/tmp/x"},
                        "command": "pk" + "ill -f litellm"}]},
            "SHAPE_ENVELOPE", "LGV-020")


class TestOwnerVerificationFlowBattery(GuardHarness):
    """T017: owner install/verify/probe/disable flow (FR-009/010/014,
    US4-S1/S2/S3, SC-005, SC-007).

    Committed probe battery encoding the quickstart.md procedure:
    - install verification: byte-for-byte parity of every deployed file
      against the reviewed kit source (FR-014) AND owner-only
      permissions across the whole installed directory (FR-009);
    - non-destructive denial probe: marker + `false &&` + a protected
      lifecycle command through the REAL registered callback — proves
      BOTH the matcher layer (veto) and the hook layer (tool name
      intact) while never signaling the protected proxy (SC-005);
    - read-only diagnostics pass in the same session;
    - negative install assertions: staging an install adds no launchd
      services, requires no restart, resolves no credential files —
      verified by construction (the flow copies three files and writes
      two owner-authored JSON files; nothing else exists afterward);
    - disable: the registered adapter's hook is withdrawn; with the
      hook absent a protected probe is NOT blocked (guard absent,
      SC-006/SC-007) — the plugin ships no restart-adjacent tool
      (Constitution V).
    """

    DEPLOYED_FILES = ("__init__.py", "guard.py", "plugin.yaml")
    OWNER_FILES = ("config.json", "integrity.sha256")

    def _stage_install(self, tag):
        """Simulated owner install: copy the three source files
        byte-for-byte into a fresh directory, author the rev-3 policy
        and integrity reference, and set owner-only permissions
        throughout."""
        deployed = Path(tempfile.mkdtemp(prefix=f"guard-install-{tag}-"))
        self.addCleanup(self._cleanup_fixture_directory, deployed)
        for name in self.DEPLOYED_FILES:
            shutil.copyfile(MODULE_PATH.parent / name, deployed / name)
        policy = write_policy(deployed, policy_name="config.json")
        write_integrity_ref(
            deployed, policy_name="config.json",
            sources=(deployed / "guard.py", deployed / "__init__.py"))
        for path in deployed.iterdir():
            path.chmod(0o600)
        return deployed

    def _load_adapter(self, d, name):
        spec = importlib.util.spec_from_file_location(
            name, d / "__init__.py", submodule_search_locations=[str(d)])
        if spec is None or spec.loader is None:
            raise RuntimeError(f"unable to load deployed adapter {name}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    def _load_guard(self, d, name):
        spec = importlib.util.spec_from_file_location(name, d / "guard.py")
        if spec is None or spec.loader is None:
            raise RuntimeError(f"unable to load deployed guard {name}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    # -- install verification (FR-014 parity + FR-009 permissions) -----
    def test_install_parity_and_permissions(self):
        deployed = self._stage_install("verify")
        # FR-014: every deployed source file byte-identical to the
        # reviewed kit source
        for name in self.DEPLOYED_FILES:
            with self.subTest(file=name):
                self.assertEqual(
                    (deployed / name).read_bytes(),
                    (MODULE_PATH.parent / name).read_bytes(),
                    f"deployed {name} diverged from kit source")
        # FR-009: owner-only permissions across the WHOLE installed
        # directory — no group/world write anywhere
        for path in deployed.iterdir():
            with self.subTest(perms=path.name):
                mode = path.stat().st_mode & 0o777
                self.assertEqual(mode, 0o600,
                                 f"{path.name} must be owner-only (0o600)")
        # the guard loads and allows a benign call (install is sound)
        mod = self._load_guard(deployed, "guard_install_verify")
        self.assertIsNone(mod.evaluate(
            "terminal", {"command": "echo ok"},
            deployed / "config.json", discover=self.discover_fake))

    def test_divergent_deployed_copy_fails_closed(self):
        # FR-014: a divergent deployed copy is treated as uninstalled —
        # the integrity self-check fails the next executable call
        deployed = self._stage_install("divergent")
        (deployed / "guard.py").write_bytes(
            (MODULE_PATH.parent / "guard.py").read_bytes() + b"\n# diverged\n")
        (deployed / "guard.py").chmod(0o600)
        mod = self._load_adapter(deployed, "guard_install_divergent")
        ctx = _RecordingContext(self)
        mod.register(ctx)
        ctx.capture_guard(sys.modules[mod.__name__ + ".guard"])
        cb = ctx.hooks["pre_tool_call"]
        result = cb(tool_name="terminal", args={"command": "echo hi"})
        self.assertIsNotNone(result)
        self.assertEqual(result.get("category"), "POLICY_INVALID")
        self.assertEqual(result.get("log_code"), "LGV-030")

    # -- non-destructive denial probe (SC-005, US4-S2) ------------------
    def test_denial_probe_proves_matcher_and_hook_layers(self):
        # The quickstart probe: marker + `false &&` + protected command,
        # delivered through the REAL registered callback. Expected: the
        # whole call is vetoed (matcher layer, LGV-001) with the tool
        # name intact (hook layer), read-only diagnostics still pass,
        # and nothing signals the proxy (identity-match command: no
        # discovery needed, no real PID touched).
        deployed = self._stage_install("probe")
        mod = self._load_adapter(deployed, "guard_install_probe")
        ctx = _RecordingContext(self)
        mod.register(ctx)
        ctx.capture_guard(sys.modules[mod.__name__ + ".guard"])
        self.assertEqual(list(ctx.hooks), ["pre_tool_call"],
                         "adapter registers exactly the pre_tool_call hook")
        cb = ctx.hooks["pre_tool_call"]
        probe = "echo guard-probe-marker && false && pkill -f litellm"
        result = cb(tool_name="terminal", args={"command": probe})
        self.assertIsNotNone(result, "probe must be vetoed")
        self.assertEqual(result.get("category"), "VETO")
        self.assertEqual(result.get("log_code"), "LGV-001")
        self.assertIn("owner-reviewed", result.get("message", ""))
        # read-only diagnostics pass in the same session
        self.assertIsNone(cb(tool_name="read_file", args={"path": "x"}))
        self.assertIsNone(cb(tool_name="process_manage",
                             args={"action": "status", "session_id": "s"}))

    def test_probe_marker_never_executes(self):
        # the veto is on the WHOLE tool call: the guard blocks before
        # the shell runs, so even the leading marker does not execute —
        # this is what makes the probe non-destructive by construction
        deployed = self._stage_install("probe-marker")
        mod = self._load_adapter(deployed, "guard_install_marker")
        ctx = _RecordingContext(self)
        mod.register(ctx)
        ctx.capture_guard(sys.modules[mod.__name__ + ".guard"])
        cb = ctx.hooks["pre_tool_call"]
        # a marker whose execution would be observable: the call is
        # blocked, so the marker never runs (nothing to observe — the
        # veto precedes any execution)
        marker = Path(tempfile.gettempdir()) / "guard-probe-ran"
        marker.unlink(missing_ok=True)  # clean precondition
        result = cb(tool_name="terminal",
                    args={"command": "touch /tmp/guard-probe-ran && "
                                     "false && pkill -f litellm"})
        self.assertIsNotNone(result)
        self.assertEqual(result.get("category"), "VETO")
        self.assertFalse(marker.exists(),
                          "blocked call must not execute even its marker")

    # -- negative install assertions (FR-009/Constitution V) ------------
    def test_install_adds_no_services_requires_no_restart(self):
        # By construction: the owner flow copies three source files and
        # authors two JSON files. Assert the installed directory
        # contains EXACTLY those five entries — no service definitions,
        # no credential files, no launchd plists, nothing restart-
        # adjacent. The plugin ships no restart tool (Constitution V).
        deployed = self._stage_install("negative")
        entries = sorted(p.name for p in deployed.iterdir())
        self.assertEqual(
            entries, sorted(self.DEPLOYED_FILES + self.OWNER_FILES),
            "installed directory must contain exactly the deployed "
            "sources + owner-authored policy/integrity files")
        # the adapter registers ONLY pre_tool_call — no restart,
        # process, or service tools of any kind; the only function it
        # DEFINES is register() (imported helpers don't count)
        mod = self._load_adapter(deployed, "guard_install_negative")
        ctx = _RecordingContext(self)
        mod.register(ctx)
        ctx.capture_guard(sys.modules[mod.__name__ + ".guard"])
        self.assertEqual(list(ctx.hooks), ["pre_tool_call"])
        defined = {n for n, v in vars(mod).items()
                   if callable(v) and not n.startswith("_")
                   and getattr(v, "__module__", "") == "guard_install_negative"}
        self.assertEqual(defined, {"register"},
                         "the adapter's own surface is register() only")

    # -- disable path (US4-S3, SC-007) -----------------------------------
    def test_disable_leaves_guard_absent(self):
        # Disabling withdraws the hook: with no hook registered, a
        # protected lifecycle probe is NOT blocked (guard absent) —
        # protection is provably gone, and read-only tools trivially
        # pass. The disable path itself must not stop the proxy.
        deployed = self._stage_install("disable")
        mod = self._load_adapter(deployed, "guard_install_disable")
        ctx = _RecordingContext(self)
        mod.register(ctx)
        ctx.capture_guard(sys.modules[mod.__name__ + ".guard"])
        self.assertIn("pre_tool_call", ctx.hooks)
        # owner disables: the hook is withdrawn (simulated unload —
        # the runtime equivalent of `hermes plugins disable`)
        withdrawn = ctx.hooks.pop("pre_tool_call")
        self.assertIsNotNone(withdrawn)
        probe = "echo guard-probe-marker && false && pkill -f litellm"
        # no hook → nothing intercepts the call → not blocked
        # (represented by the absent-hook dispatch the runtime performs)
        self.assertNotIn("pre_tool_call", ctx.hooks,
                         "live unload must withdraw the hook")

    def test_plugin_yaml_declares_only_pre_tool_call(self):
        # the manifest itself carries no restart-adjacent tool surface
        import re as _re
        yaml_text = (MODULE_PATH.parent / "plugin.yaml").read_text(
            encoding="utf-8")
        self.assertIn("provides_hooks:", yaml_text)
        hooks = _re.search(r"provides_hooks:\s*\n((?:\s*-\s*\w+\s*\n?)+)",
                           yaml_text)
        self.assertIsNotNone(hooks, "plugin.yaml must declare provides_hooks")
        declared = [h.strip("- ").strip() for h in hooks.group(1).splitlines()
                    if h.strip()]
        self.assertEqual(declared, ["pre_tool_call"])


# ---------------------------------------------------------------------------
# guard-hardening: decision sink battery (append-only JSONL state sink).
# ---------------------------------------------------------------------------



def _sink_dir(base):
    return Path(base) / "hermes" / "lifecycle-guard"


def _sink_file(base):
    return _sink_dir(base) / "decisions.jsonl"


class _SinkEnv:
    '''Point XDG_STATE_HOME at a temp dir for the duration of a test.'''

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix='lg_sink_')
        self._old = os.environ.get('XDG_STATE_HOME')
        self._retain = False

    def __enter__(self):
        os.environ['XDG_STATE_HOME'] = self.tmp
        return self.tmp

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop('XDG_STATE_HOME', None)
        else:
            os.environ['XDG_STATE_HOME'] = self._old
        if not self._retain:
            shutil.rmtree(self.tmp, ignore_errors=True)


class TestDecisionSink(unittest.TestCase):
    def setUp(self):
        from typing import Any, cast

        self._env = _SinkEnv()
        self.base = self._env.__enter__()
        self._sink_guard = None
        self._sink_worker = None
        self._sink_started = False
        self._sink_start_attempted = False
        self._sink_old_max = None
        self._sink_module_key = ('_decision_sink_' +
                                 self.id().replace('.', '_') + '_' +
                                 str(id(self)))
        self.addCleanup(self._cleanup_sink)

        spec = importlib.util.spec_from_file_location(
            self._sink_module_key, MODULE_PATH)
        if spec is None or spec.loader is None:
            self.fail('sink guard spec or loader unavailable')
        module = importlib.util.module_from_spec(spec)
        sys.modules[self._sink_module_key] = module
        self._sink_guard = cast(Any, module)
        spec.loader.exec_module(module)
        self._sink_start_attempted = True
        try:
            started = self._sink_guard.sink_start()
        finally:
            self._sink_worker = getattr(self._sink_guard, '_sink_worker', None)
        self._sink_started = bool(started)
        self.assertTrue(started, 'sink startup failed')
        self.assertIsNotNone(self._sink_worker, 'sink worker unavailable')

    def _current_sink_workers(self):
        workers = []
        if self._sink_worker is not None:
            workers.append(self._sink_worker)
        if self._sink_guard is not None:
            current = getattr(self._sink_guard, '_sink_worker', None)
            if current is not None and all(current is not worker for worker in workers):
                workers.append(current)
        return workers

    def _flush_sink(self):
        self.assertTrue(self._sink_guard.sink_flush(timeout_s=5),
                        'sink flush timed out')

    def _cleanup_sink(self):
        self._env._retain = True
        module = self._sink_guard
        flush_error = None
        stop_error = None
        terminated = False
        try:
            try:
                if self._sink_started:
                    flush = getattr(module, 'sink_flush', None)
                    if not callable(flush) or not flush(timeout_s=5):
                        flush_error = AssertionError('sink cleanup flush timed out')
            except Exception as exc:
                flush_error = exc
            finally:
                try:
                    if self._sink_start_attempted:
                        stop = getattr(module, 'sink_shutdown', None)
                        if not callable(stop) or stop(timeout_s=5) is not True:
                            stop_error = AssertionError('sink shutdown failed')
                except Exception as exc:
                    stop_error = exc

            try:
                workers = self._current_sink_workers()
                terminated = bool(workers) or (module is None and
                                               not self._sink_start_attempted)
                for worker in workers:
                    try:
                        if worker.is_alive() is not False:
                            terminated = False
                    except Exception:
                        terminated = False
            except Exception:
                terminated = False

            if terminated:
                self._env._retain = False
                try:
                    if self._sink_old_max is not None and flush_error is None:
                        module.SINK_MAX_BYTES = self._sink_old_max
                finally:
                    sys.modules.pop(self._sink_module_key, None)
        finally:
            self._env.__exit__(None, None, None)

        if not terminated:
            self.fail('sink worker remained alive or unverified')
        if flush_error is not None:
            raise flush_error
        if stop_error is not None:
            raise stop_error

    # -- helpers -------------------------------------------------------
    def _block_once(self, category='VETO'):
        '''Run one evaluation that must end in category; return decision.'''
        with tempfile.TemporaryDirectory() as d:
            policy = {
                'schema_version': 1,
                'protected_port': 4000,
                'protected_labels': ['ai.litellm.fixture-proxy'],
                'protected_session_handles': [],
                'bounds': {
                    'input_text_bytes': 262144,
                    'max_chunks': 512,
                    'max_depth': 8,
                    'discovery_timeout_ms': 2000,
                    'max_snapshot_pids': 256,
                },
                'refusal_vocabulary_version': 1,
            }
            pp = Path(d) / 'policy.json'
            pp.write_text(json.dumps(policy))
            write_integrity_ref(d)
            for fn in ('policy.json', 'integrity.sha256',
                       'guard.py', '__init__.py'):
                os.chmod(Path(d) / fn, 0o600)
            completed_ports = []
            sink_guard = self._sink_guard
            assert sink_guard is not None

            def discover(port):
                snapshot = sink_guard.Snapshot(
                    pids=frozenset({4711}), pgids=frozenset({4711}),
                    comms=frozenset({'litellm'}),
                    args=('litellm --port 4000',))
                self.assertIn(4711, snapshot.pids)
                completed_ports.append(port)
                return snapshot

            decision = self._sink_guard.evaluate(
                'terminal',
                {'command': 'pkill -f litellm'},
                pp,
                discover=discover)
            self.assertEqual(completed_ports, [4000])
            self._flush_sink()
        self.assertIsNotNone(decision, 'expected a block decision')
        self.assertEqual(decision['category'], category)
        return decision

    def _read_lines(self):
        self._flush_sink()
        f = _sink_file(self.base)
        if not f.exists():
            return []
        return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]

    # -- tests ---------------------------------------------------------
    def test_block_decision_appends_sanitized_record(self):
        decision = self._block_once()
        lines = self._read_lines()
        self.assertEqual(len(lines), 1, 'exactly one sink line per block')
        rec = lines[0]
        self.assertEqual(rec['category'], 'VETO')
        self.assertEqual(rec['code'], 'LGV-001')
        self.assertEqual(rec['tool'], 'terminal')
        self.assertIn('ts', rec)
        # sanitized sink: no command material, no decision message body
        self.assertNotIn('pkill', json.dumps(rec))
        self.assertNotIn('command', rec)

    def test_allow_decision_not_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            policy = {'protected_port': 4000, 'target_tokens': ['litellm'],
                      'protected_cmdline': ['litellm']}
            pp = Path(d) / 'policy.json'
            pp.write_text(json.dumps(policy))
            write_integrity_ref(d)
            decision = self._sink_guard.evaluate(
                'search_files', {'pattern': 'litellm'}, pp)
            self._flush_sink()
        self.assertIsNone(decision)
        self.assertEqual(self._read_lines(), [],
                         'allow decisions must not hit the sink')

    def test_sink_owner_only_permissions(self):
        self._block_once()
        d = _sink_dir(self.base)
        f = _sink_file(self.base)
        self.assertTrue(d.is_dir())
        self.assertTrue(f.is_file())
        dmode = stat.S_IMODE(d.stat().st_mode)
        fmode = stat.S_IMODE(f.stat().st_mode)
        self.assertEqual(dmode, 0o700, 'sink dir must be owner-only')
        self.assertEqual(fmode, 0o600, 'sink file must be owner-only')

    def test_sink_rotation_bound(self):
        old = getattr(self._sink_guard, 'SINK_MAX_BYTES', None)
        self.assertIsNotNone(old, 'guard must define SINK_MAX_BYTES')
        self._sink_old_max = old
        self._sink_guard.SINK_MAX_BYTES = 400  # tiny bound for the test
        for _ in range(6):
            self._block_once()
        main = _sink_file(self.base)
        rotated = Path(str(main) + '.1')
        self.assertTrue(rotated.exists(), 'bound exceeded -> rotation')
        self.assertLess(main.stat().st_size, 400 + 200,
                        'post-rotation file must be under the bound')
        self.assertLessEqual(main.stat().st_size, 400)
        self.assertLessEqual(rotated.stat().st_size, 400)
        # every line (both generations) is still valid JSON
        for gen in (main, rotated):
            for line in gen.read_text().splitlines():
                if line.strip():
                    json.loads(line)

    def test_sink_unwritable_fail_open(self):
        # occupy the sink path with an unwritable DIRECTORY: append must fail
        os.makedirs(_sink_file(self.base), exist_ok=True)
        os.chmod(_sink_dir(self.base), 0o500)
        try:
            decision = self._block_once()
            self.assertEqual(decision['action'], 'block',
                             'unwritable sink must never weaken enforcement')
        finally:
            os.chmod(_sink_dir(self.base), 0o700)

    def test_sink_record_has_stable_schema(self):
        self._block_once()
        rec = self._read_lines()[0]
        self.assertEqual(set(rec), {'ts', 'category', 'code', 'tool'},
                         'sink schema must stay exactly ts/category/code/tool')




# ---------------------------------------------------------------------------
# guard-hardening: test-isolation + pre-existing-mode hardening battery.
# ---------------------------------------------------------------------------


# Captured at import time: the owner's REAL state home, immune to the
# per-test XDG_STATE_HOME redirection GuardHarness.setUp performs.
_REAL_STATE_HOME = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
    "~/.local/state")


class TestSinkIsolationFromPlainTests(GuardHarness):
    """guard-hardening defect 1: plain guard tests must never touch the live sink.

    GuardHarness.setUp is now responsible for state-home isolation —
    a veto from ANY GuardHarness test (not just TestDecisionSink) must
    write nothing to the real state home.
    """

    def test_plain_veto_writes_nothing_to_real_state_home(self):
        before = self._snapshot_real_sink()
        self.assert_blocked("terminal", {"command": "kill -9 4711"})
        after = self._snapshot_real_sink()
        self.assertEqual(before, after,
                         "plain GuardHarness veto leaked to the real sink")

    def _snapshot_real_sink(self):
        """Read-only signature of the real sink (count + mtime + mode)."""
        path = Path(_REAL_STATE_HOME) / "hermes" / "lifecycle-guard" / "decisions.jsonl"
        try:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size, st.st_mode)
        except OSError:
            return None


class TestSinkPreExistingFileMode(unittest.TestCase):
    """guard-hardening defect 2: a pre-existing 0644 sink must be tightened to 0600.

    Root cause: _tighten_owner_only only acts when mode & 0o022 (write
    bits) is set, so a 0644 file (group/other READ) is never corrected,
    and the O_EXCL create branch never runs for an existing file.
    """

    def setUp(self):
        self._old = os.environ.get("XDG_STATE_HOME")
        self.tmp = tempfile.mkdtemp(prefix="lg_mode_")
        os.environ["XDG_STATE_HOME"] = self.tmp
        self.addCleanup(self._restore)

    def _restore(self):
        import shutil
        if self._old is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_preexisting_0644_sink_tightened_to_0600(self):
        import stat as stat_mod
        d = Path(self.tmp) / "hermes" / "lifecycle-guard"
        d.mkdir(parents=True)
        f = d / "decisions.jsonl"
        f.write_text("")  # pre-existing, like the live file at birth
        os.chmod(f, 0o644)
        guard.sink_write("VETO", "LGV-001", "terminal")
        mode = stat_mod.S_IMODE(f.stat().st_mode)
        self.assertEqual(mode, 0o600,
                         "pre-existing 0644 sink must tighten to owner-only")


class _RecordingContext:
    def __init__(self, owner):
        from unittest.mock import Mock

        self.owner = owner
        self.hooks = {}
        self._unload_callbacks = []
        self._guard = None
        self._guard_name = None
        self._captured_worker = None
        self._captured = False
        self._closed = False
        self.register_hook = Mock(side_effect=self._record_hook)
        owner.addCleanup(self.close)

    def _record_hook(self, event, callback):
        self.hooks[event] = callback

    def on_unload(self, callback):
        self._unload_callbacks.append(callback)

    def capture_guard(self, guard_module):
        self._guard = guard_module
        self._guard_name = guard_module.__name__
        self._captured_worker = guard_module._sink_worker
        self._captured = True

    def close(self):
        if self._closed:
            return

        failed = False
        verified = self._captured and self._guard is not None
        workers = []

        def remember(worker):
            if worker is not None and all(worker is not item for item in workers):
                workers.append(worker)

        remember(self._captured_worker)
        if self._guard is not None:
            try:
                remember(self._guard._sink_worker)
            except BaseException:
                failed = True
                verified = False

        if self._guard is not None:
            try:
                if self._guard.sink_flush(timeout_s=5.0) is not True:
                    failed = True
            except BaseException:
                failed = True

        callbacks = self._unload_callbacks
        self._unload_callbacks = []
        for callback in reversed(callbacks):
            try:
                callback()
            except BaseException:
                failed = True

        if self._guard is not None:
            try:
                remember(self._guard._sink_worker)
            except BaseException:
                failed = True
                verified = False

        verified = verified and bool(workers)
        for worker in workers:
            try:
                if worker.is_alive():
                    worker.join(timeout=0.5)
                if worker.is_alive():
                    verified = False
            except BaseException:
                failed = True
                verified = False

        if verified:
            try:
                if (isinstance(self._guard_name, str)
                        and sys.modules.get(self._guard_name) is self._guard):
                    sys.modules.pop(self._guard_name, None)
            except BaseException:
                failed = True
            self._closed = True
        else:
            self.owner._retain_sink_fixtures = True

        if failed or not verified:
            self.owner.fail('sink worker cleanup failed or shutdown could not be verified')


if __name__ == "__main__":
    unittest.main(verbosity=2)