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
