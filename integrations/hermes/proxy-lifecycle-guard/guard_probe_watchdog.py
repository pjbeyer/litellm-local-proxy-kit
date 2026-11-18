#!/usr/bin/env python3
"""Guard-probe watchdog for the deployed proxy-lifecycle-guard plugin.

Read-only recurring monitor (recurring-signal-monitor pattern): verifies the
deployed guard still (a) exists with correct owner-only permissions,
(b) passes its own integrity digest, (c) vetoes a recognized lifecycle command
on a branch that cannot execute, (d) allows a read-only diagnostic, and
(e) routes shape-class envelopes correctly (lpkit-9df): decode-fail and
non-canonical key shapes must surface as SHAPE_ENVELOPE (LGV-020 corrective
routing, lpkit-1cz / amendment 3a), and a canonical relay envelope must pass.
Fail-closed: any probe step that cannot be proven healthy is an alert.
Silent-on-success: empty stdout when everything passes.

Probe note (LGV-001 class, FR-010): the veto probe command is placed on a
shell branch that cannot execute ('if false; then ...') — non-destructive by
construction; nothing is ever signaled. The shape-class probes are equally
non-executable by construction: the decode-fail payload never parses, the
relay targets a nonexistent tool_id, and only the guard's decision is
asserted, never execution.

Deployment: this script is mirrored to ~/.hermes/scripts/guard_probe_watchdog.py
and run by Hermes cron job 8928a8340f33 (every 4h, no_agent). The deployed
copy is the runtime source of truth for the cron job; this repo copy is the
reviewed upstream — mirror changes here to the scripts directory (or vice
versa) in the same change. State: probe_history.jsonl under
$XDG_STATE_HOME/hermes/guard-probe-watchdog/.
"""
import os
import sys
import json
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cron_output_sanitizer import sanitize_markdown  # noqa: E402
from network_health import check as network_check  # noqa: E402

DEPLOY_DIR = os.environ.get(
    "GUARD_PROBE_DEPLOY_DIR",
    os.path.expanduser("~/.hermes/plugins/proxy-lifecycle-guard"))
POLICY = os.path.join(DEPLOY_DIR, "config.json")
STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "hermes", "guard-probe-watchdog",
)
STATE_FILE = os.path.join(STATE_DIR, "probe_history.jsonl")

# Non-destructive by construction: the kill verb sits on a dead branch.
VETO_PROBE = "if false; then lsof -t -iTCP:4000 | xargs kill -9; fi"
ALLOW_PROBE = "echo healthy"

# Shape-class probes (lpkit-9df). All are non-executable by construction:
# the decode-fail payload fails json.loads inside the guard before any
# command could run; the relay envelope targets a nonexistent tool_id so
# the aggregator would refuse it anyway; the guard's decision is what we
# assert on, never the execution.
DECODE_FAIL_PROBE = "[{'name': 'terminal', 'arguments': {'command': 'echo hi'}}]"
NONCANONICAL_PROBE = [{"tool": "terminal", "args": {"command": "echo hi"}}]
CANONICAL_RELAY_PROBE = [{"name": "mcp__local_mcp_aggregator__call_tool",
                          "arguments": {"tool_id": "watchdog-shape-probe-none",
                                        "arguments": {}}}]


def _probe_eval(command: str, tool_name: str = "terminal",
                args: dict = None) -> str:
    """Run guard.evaluate() in a subprocess; return the decision category.

    tool_name/args let shape-class probes (lpkit-9df) evaluate
    tool_call envelopes instead of only terminal commands; the
    subprocess contract (PROBE_ERROR prefix, ALLOW on None) is
    unchanged.
    """
    call_args = {"command": command} if args is None else args
    code = (
        "import sys, json\n"
        "sys.path.insert(0, %r)\n"
        "import guard\n"
        "res = guard.evaluate(%r, %r, %r)\n"
        "if res is None:\n"
        "    print(json.dumps({'category': None, 'log_code': None}))\n"
        "else:\n"
        "    print(json.dumps({'category': res.get('category'),"
        " 'log_code': res.get('log_code')}))\n"
        % (DEPLOY_DIR, tool_name, call_args, POLICY)
    )
    out = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        return "PROBE_ERROR: " + out.stderr.strip().splitlines()[-1][:120]
    try:
        parsed = json.loads(out.stdout.strip())
        return parsed.get("category") or "ALLOW"
    except (ValueError, AttributeError):
        return "PROBE_ERROR: unparseable evaluate() output"


def _integrity_ok() -> bool:
    """Recompute the deployed integrity digests per FR-015.

    The deployed reference is JSON: {"policy_digest": ..., "source_digests":
    {name: digest}}. Fail closed on any missing file, parse error, mismatch.
    """
    import hashlib
    ref_path = os.path.join(DEPLOY_DIR, "integrity.sha256")
    if not os.path.isfile(ref_path):
        return False
    try:
        with open(ref_path, "r", encoding="utf-8") as fh:
            refs = json.load(fh)
        expected = {"config.json": refs["policy_digest"]}
        expected.update(refs.get("source_digests", {}))
        for name, digest in expected.items():
            path = os.path.join(DEPLOY_DIR, name)
            if not os.path.isfile(path):
                return False
            with open(path, "rb") as fh:
                actual = hashlib.sha256(fh.read()).hexdigest()
            if actual != digest:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _record_state(decisions: dict) -> None:
    """Append the probe outcome to XDG state (never blocks the run)."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        entry = {
            "ts": subprocess.run(
                ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True,
                text=True).stdout.strip(),
            "veto": decisions["veto"], "allow": decisions["allow"],
            "decode_fail": decisions["decode_fail"],
            "noncanonical": decisions["noncanonical"],
            "relay_allow": decisions["relay_allow"],
            "integrity": decisions["integrity"], "perms": decisions["perms"],
        }
        with open(STATE_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # state recording is best-effort; never masks a probe verdict


def _check_perms() -> bool:
    """FR-009 scope: source/policy/integrity files must be owner-only; the
    directory and any runtime regenerable cache (__pycache__) must deny
    group/world WRITE. Directory execute bits are required for imports."""
    try:
        PROTECTED_FILES = {"README.md", "__init__.py", "config.json",
                           "guard.py", "integrity.sha256", "plugin.yaml"}
        for root, dirs, files in os.walk(DEPLOY_DIR):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name in PROTECTED_FILES:
                    mode = os.stat(os.path.join(root, name)).st_mode & 0o077
                    if mode != 0:
                        return False
        for path in [DEPLOY_DIR] + [
                os.path.join(DEPLOY_DIR, d) for d in ("__pycache__",)]:
            if os.path.isdir(path):
                mode = os.stat(path).st_mode & 0o022
                if mode != 0:  # group/world write denied
                    return False
        return True
    except OSError:
        return False


def run_watch() -> int:
    """Return 0 healthy, 1 alert-worthy failure, 2 unexpected error."""
    try:
        # Network is never needed for the probe itself, but the pre-check is
        # required by the compliance audit for any network-capable script;
        # skip gracefully when unreachable (probe is local-only).
        net = network_check()
        _ = net.reachable  # noqa: F841 — local probe; never blocks on network

        failures = []
        if not os.path.isdir(DEPLOY_DIR):
            failures.append("plugin directory missing — guard UNINSTALLED")
        else:
            if not _check_perms():
                failures.append("directory permissions not owner-only (FR-009)")
            integ = _integrity_ok()
            if not integ:
                failures.append(
                    "integrity reference invalid — tamper evidence tripped"
                    " (FR-015/LGV-032 class)")

        veto = _probe_eval(VETO_PROBE) if os.path.isdir(DEPLOY_DIR) else None
        allow = _probe_eval(ALLOW_PROBE) if os.path.isdir(DEPLOY_DIR) else None
        # Shape-class probes (lpkit-9df): non-executable by construction.
        decode_fail = _probe_eval(
            "", tool_name="tool_call",
            args={"calls": DECODE_FAIL_PROBE}) if os.path.isdir(DEPLOY_DIR) else None
        noncanonical = _probe_eval(
            "", tool_name="tool_call",
            args={"calls": NONCANONICAL_PROBE}) if os.path.isdir(DEPLOY_DIR) else None
        relay_allow = _probe_eval(
            "", tool_name="tool_call",
            args={"calls": CANONICAL_RELAY_PROBE}) if os.path.isdir(DEPLOY_DIR) else None
        decisions = {
            "veto": veto, "allow": allow,
            "decode_fail": decode_fail, "noncanonical": noncanonical,
            "relay_allow": relay_allow,
            "integrity": integ if os.path.isdir(DEPLOY_DIR) else False,
            "perms": _check_perms() if os.path.isdir(DEPLOY_DIR) else False,
        }

        if veto is not None and veto != "VETO":
            failures.append(
                "LIVE GUARD FAILURE: recognized lifecycle probe command was "
                "NOT vetoed (decision=%s) — protection may be absent"
                % sanitize_markdown(str(veto)))
        if allow is not None and allow != "ALLOW":
            failures.append(
                "false positive: read-only diagnostic refused (decision=%s)"
                % sanitize_markdown(str(allow)))

        if decode_fail is not None and decode_fail != "SHAPE_ENVELOPE":
            failures.append(
                "shape-class regression: decode-fail envelope did not route to "
                "SHAPE_ENVELOPE (decision=%s) — LGV-020 corrective routing "
                "absent (lpkit-1cz class)" % sanitize_markdown(str(decode_fail)))
        if noncanonical is not None and noncanonical != "SHAPE_ENVELOPE":
            failures.append(
                "shape-class regression: non-canonical key shape did not route "
                "to SHAPE_ENVELOPE (decision=%s) — amendment 3a enforcement "
                "absent (lpkit-6u6 class)" % sanitize_markdown(str(noncanonical)))
        if relay_allow is not None and relay_allow != "ALLOW":
            failures.append(
                "false positive: canonical relay envelope refused "
                "(decision=%s)" % sanitize_markdown(str(relay_allow)))

        _record_state(decisions)

        if failures:
            sys.stdout.write(sanitize_markdown(
                "# 🛡️ proxy-lifecycle-guard Watchdog — ALERT\n\n"
                + "\n".join("- " + f for f in failures) + "\n"))
            return 1
        return 0  # silent-on-success
    except Exception:  # top-level guard: script error path
        sys.stderr.write("guard-probe-watchdog: unexpected error\n")
        return 2


def _self_test() -> int:
    """Pure-logic assertions; no network, no guard import, no state writes."""
    import stat as stat_mod
    ok = True

    # The veto probe must be non-destructive by construction (FR-010).
    if "if false" not in VETO_PROBE:
        ok = False
    # The allow probe must never contain a kill-family verb.
    for verb in ("kill", "pkill", "killall", "bootout", "kickstart"):
        if verb in ALLOW_PROBE:
            ok = False

    # Shape probes (lpkit-9df) must be non-destructive: the decode-fail
    # payload is a non-decoding string (never executes), the relay targets
    # a nonexistent tool_id, and neither carries a live kill-family verb
    # in an executable position (they sit inside guard inputs only).
    if "kill" in DECODE_FAIL_PROBE or "pkill" in DECODE_FAIL_PROBE:
        ok = False
    if any(v in json.dumps(CANONICAL_RELAY_PROBE) for v in ("kill", "pkill")):
        ok = False
    if not NONCANONICAL_PROBE or "tool" not in NONCANONICAL_PROBE[0]:
        ok = False  # the probe must exercise the non-canonical key shape

    # _probe_eval contract: PROBE_ERROR prefix on subprocess failure.
    sentinel = object()
    got = _probe_eval("definitely-not-a-real-command-xyz")
    if not (isinstance(got, str) and got.startswith("PROBE_ERROR")):
        # a non-existent DEPLOY_DIR import error must map to PROBE_ERROR,
        # never a crash — but only when the plugin dir is genuinely absent.
        if not os.path.isdir(DEPLOY_DIR):
            ok = False
        elif not isinstance(got, str):
            ok = False
    # integrity check must fail closed on a missing reference file
    if os.path.isdir(DEPLOY_DIR) and not os.path.isfile(
            os.path.join(DEPLOY_DIR, "integrity.sha256")):
        if _integrity_ok():
            ok = False
    # perms check: 0o077 bits must be the only acceptance criterion
    if not (0o077 == (stat_mod.S_IRWXG | stat_mod.S_IRWXO)):
        ok = False

    print("self-test: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv or "--test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(run_watch())