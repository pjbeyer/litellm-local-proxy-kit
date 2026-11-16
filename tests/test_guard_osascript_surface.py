"""osascript indirection battery (macOS AppleScript/JXA surface).

A recognized FN class: signal intent routed through AppleScript shell
indirection must veto on BOTH in-scope surfaces; benign AppleScripts
must pass. Offline: the guard's matcher is driven directly and the
command strings are NEVER executed (the fixture forbids native ops).
"""
import shlex

import pytest

# Reuse the quoted-control-flow scope fixture (worker lifecycle, forbidden
# native ops, discovery capture) by importing it under its fixture name.
from test_guard_quoted_control_flow import scope  # noqa: F401
from test_lifecycle_guard import FIXTURE_PID, FIXTURE_PORT

KILL = "pkill -f litellm"


def _decision(evaluate, command):
    decision = evaluate(command)
    assert decision is not None
    assert decision["action"] == "block"
    assert decision["category"] == "VETO"
    assert decision["log_code"] == "LGV-001"
    return decision


# expect_discovery: True when the veto flows through protected-target
# (PID) analysis; False when it flows through label analysis (launchctl)
# or the FR-006 unprovable-operand refusal (unextractable operand or a
# non-shell-execution operand shape).
@pytest.mark.parametrize("command,expect_discovery", [
    # nested-quote variants of the do-shell-script wrapper
    ("osascript -e 'do shell script \"" + KILL + "\"'", True),
    ('osascript -e "do shell script \'' + KILL + '\'"', False),
    # bare -e payload: kill text as the osascript program itself is not
    # a recognizable shell-execution form — unprovable, refused (FR-006)
    ('osascript -e "' + KILL + '"', False),
    # JXA shell execution form (literal argument extracts cleanly)
    ('osascript -l JavaScript -e '
     + shlex.quote('ObjC.import("stdlib"); $.system("' + KILL + '")'),
     True),
    # launchctl mutation through do shell script (label-based analysis)
    ("osascript -e 'do shell script \"launchctl bootout gui/501/"
     "ai.litellm.local-proxy\"'", False),
    # kill wrapped in shell control flow INSIDE the AppleScript payload
    ("osascript -e 'do shell script \"if true; then " + KILL
     + "; fi\"'", True),
    # with-administrator-privileges form
    ("osascript -e 'do shell script \"" + KILL + "\" "
     "with administrator privileges'", True),
])
def test_osascript_shell_indirection_is_vetoed(scope, command,
                                               expect_discovery):
    evaluate, _, discovered = scope
    _decision(evaluate, command)
    if expect_discovery:
        assert discovered == [FIXTURE_PORT], \
            "veto must reach protected-target analysis"


@pytest.mark.parametrize("command", [
    'osascript -e ' + shlex.quote('display dialog "Hello"'),
    'osascript -e ' + shlex.quote('tell app "Finder" to activate'),
    "osascript -e 'do shell script \"ls -la /tmp\"'",
    "osascript -e 'do shell script \"mkdir -p /tmp/guard-x && echo done\"'",
])
def test_benign_applescripts_pass(scope, command):
    evaluate, _, _ = scope
    assert evaluate(command) is None


# --- lpkit-paw: quote-style consistency gates (both operand forms) ---
# Adjudication (2026-11-16): the FN claim is falsified on HEAD — through
# the dq form the guard vetoes every genuine threat (protected numeric
# PID via discovery, pkill/pattern verbs, $(pgrep) substitutions,
# port-sentinel xargs pipelines, launchctl bootout, killall). The
# original battery's "numeric PID" row used a PID that was never in
# the protected set (live discovery), so its dq-form ALLOW was the
# documented by-target scope, not a leak. These rows pin the two
# styles AGREEING, by operand class:
#   - protected numeric operand: both styles VETO (target analysis)
#   - non-protected numeric operand: both styles ALLOW (by-target scope
#     parity with the direct surface)
#   - real pattern verbs: both styles VETO regardless of operand
#   - by-name no-op kill (<kill -9 litellm>, a POSIX no-op): both
#     styles ALLOW, matching the direct surface
@pytest.mark.parametrize("command", [
    # protected numeric PID, dq and sq inner operand
    "osascript -e 'do shell script \"kill -9 " + str(FIXTURE_PID) + "\"'",
    'osascript -e "do shell script \'kill -9 ' + str(FIXTURE_PID) + '\'"',
])
def test_osascript_protected_numeric_pid_vetoes_both_styles(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["category"] == "VETO"
    assert decision["log_code"] == "LGV-001"


@pytest.mark.parametrize("command", [
    "osascript -e 'do shell script \"kill -9 1234\"'",
    "kill -9 1234",
])
def test_osascript_nonprotected_numeric_allows_valid_forms(scope, command):
    # 1234 is not in the fixture's protected set; the direct surface
    # allows it, so the valid dq osascript form must too (consistency)
    evaluate, _, _ = scope
    assert evaluate(command) is None


def test_osascript_sq_form_invalid_applescript_refuses(scope):
    # `do shell script '...'` is INVALID AppleScript (strings are
    # double-quoted only), so the operand is unextractable — FR-006
    # fail-closed refusal regardless of operand content. This is the
    # original battery's sq-vs-dq "asymmetry": not a leak — one form
    # is valid (analyzable, target-scoped) and one is malformed
    # (unprovable, refused). Pinned so the asymmetry question stays
    # settled: malformed always refuses, valid follows target scope.
    evaluate, _, _ = scope
    decision = evaluate('osascript -e "do shell script \'kill -9 1234\'"')
    assert decision is not None
    assert decision["category"] == "VETO"
    assert decision["log_code"] == "LGV-001"


@pytest.mark.parametrize("command", [
    # real pattern verbs through the dq form (the original FN concern)
    "osascript -e 'do shell script \"pkill -f litellm\"'",
    "osascript -e 'do shell script \"killall litellm\"'",
    "osascript -e 'do shell script \"kill -9 $(pgrep litellm)\"'",
    "osascript -e 'do shell script \"lsof -t -iTCP:4000 | xargs kill -9\"'",
])
def test_osascript_real_verbs_dq_vetoes(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["category"] == "VETO"
    assert decision["log_code"] == "LGV-001"


@pytest.mark.parametrize("code", [
    'import subprocess\n'
    'subprocess.run(["osascript", "-e", '
    + repr('do shell script "' + KILL + '"') + '])',
    'import subprocess\n'
    'subprocess.run(["osascript", "-e", '
    + repr("do shell script '" + KILL + "'") + '])',
    'import subprocess\n'
    'subprocess.run(["osascript", "-e", ' + repr(KILL) + '])',
    'import subprocess\n'
    'subprocess.run(["osascript", "-l", "JavaScript", "-e", '
    + repr('ObjC.import("stdlib"); $.system("' + KILL + '")') + '])',
])
def test_osascript_argv_indirection_is_vetoed(scope, code):
    # execute_code surface: the argv-literal path must route the nested
    # AppleScript payload into shell analysis
    evaluate, _, discovered = scope
    decision = evaluate("python3 -c " + shlex.quote(code))
    assert decision is not None
    assert decision["action"] == "block"
    assert decision["category"] == "VETO"
    assert decision["log_code"] == "LGV-001"


@pytest.mark.parametrize("code", [
    'import subprocess\n'
    'subprocess.run(["osascript", "-e", '
    + repr('display dialog "Hello"') + '])',
])
def test_benign_argv_applescript_passes(scope, code):
    evaluate, _, _ = scope
    assert evaluate("python3 -c " + shlex.quote(code)) is None
