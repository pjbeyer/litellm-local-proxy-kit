"""lpkit-0gk / lpkit-90d: execute_code data-vs-execution posture (terminal).

FP class (2026-11-09, V&V): execute_code string literals carrying kill
vocabulary were routed to shell analysis even when they were DEAD DATA —
dict values, json.dumps args, bytestrings, census/comprehension lists —
vetoing analysis/inspection cells that never executed the text. The
terminal surface allowed the identical literal, so execute_code was an
over-match. FIXED (2026-11-18): only string operands that reach an
EXECUTION primitive are analyzed (os.system/os.popen/subprocess shell
and argv with a lifecycle executor verb/os.exec*/spawn*/exec/eval —
including bare-name and module-alias forms and wrapper-wrapped kill
argv); dead-data literals now pass. This reverses the prior documented
'source-data' rule — the issues directed it.

Security core is preserved and pinned here: every genuine execution
form still vetoes (the red battery). The data-ALLOW is scoped narrowly:
a literal that is actually passed to an execution sink is still blocked.
"""
import shlex

from test_lifecycle_guard import FIXTURE_PID
from test_guard_quoted_control_flow import scope  # noqa: F401


# --- data literals pass (was the FP) ---
def test_dead_data_strings_pass(scope):
    evaluate, _, _ = scope
    cases = [
        'rows = {"cleanup": "pkill -f litellm"}\nprint(len(rows))',
        'table = [{"cmd": "pkill -f litellm"}]\nprint(table)',
        'import json; data = json.dumps({"a": "kill -9 1234"}); print(data)',
        'print("subprocess.run([\'pkill\', \'-f\', \'litellm\'])")',
        'docs = b"pkill -f litellm"; print(len(docs))',
        'cmds = ["pkill -f litellm"]\nprint(len(cmds))',
        'n = sum(1 for c in ["pkill -f litellm"] if "litellm" in c); print(n)',
        # non-lifecycle executor with DATA argv (incl. a dynamic element)
        'import subprocess\nsubprocess.run(["grep", "a;b;kill", "f.txt"])',
        'import subprocess\nsubprocess.run(["ls", "-la", v])',
    ]
    for code in cases:
        assert evaluate("python3 -c " + shlex.quote(code)) is None, code


def test_git_commit_message_signal_data_passes_terminal(scope):
    # terminal surface has always allowed this; execute_code argv now agrees
    evaluate, _, _ = scope
    assert evaluate('git commit -m "msg.\n\npkill -f litellm done."') is None


def test_git_commit_message_signal_data_passes_execute_code(scope):
    evaluate, _, _ = scope
    msg = "msg.\n\npkill -f litellm done."
    code = ('import subprocess\n'
            'subprocess.run(["git", "commit", "-m", ' +
            repr(msg) + '])')
    assert evaluate("python3 -c " + shlex.quote(code)) is None


# --- genuine execution STILL vetoes (security core, red battery) ---
def test_shell_string_sinks_veto(scope):
    evaluate, _, _ = scope
    cases = [
        'import os\nos.system("pkill -f litellm")',
        'import os\nos.system(f"kill -9 4711")',
        'import os\nos.popen("pkill -f litellm")',
        'import subprocess\nsubprocess.run("pkill -f litellm", shell=True)',
        'import subprocess\nsubprocess.call("kill -9 4711", shell=True)',
        "subprocess.run('kill $(lsof -ti:4000)', shell=True)",
    ]
    for code in cases:
        d = evaluate("python3 -c " + shlex.quote(code))
        assert d is not None and d["category"] == "VETO", code


def test_exec_eval_sinks_veto(scope):
    evaluate, _, _ = scope
    for code in (
        'exec("import os; os.kill(9, 4711)")',
        'eval("import os; os.kill(9, 4711)")',
        'exec("os.system(\'pkill -f litellm\')")',
    ):
        d = evaluate("python3 -c " + shlex.quote(code))
        assert d is not None and d["category"] == "VETO", code


def test_kill_argv_veto(scope):
    evaluate, _, _ = scope
    for code in (
        'import subprocess\nsubprocess.run(["pkill", "-f", "litellm"])',
        'import subprocess\nsubprocess.run(["kill", "-9", "4711"])',
        'import subprocess\nsubprocess.run(["sudo", "kill", "4711"])',
        'import subprocess\nsubprocess.run(["sh", "-c", "pkill -f litellm"])',
    ):
        d = evaluate("python3 -c " + shlex.quote(code))
        assert d is not None and d["category"] == "VETO", code


def test_aliased_executors_veto(scope):
    # from subprocess import run / import sp / from os import system/popen
    evaluate, _, _ = scope
    cases = [
        'from subprocess import run\nrun("pkill -f litellm", shell=True)',
        'import subprocess as sp\nsp.run("pkill -f litellm", shell=True)',
        'from os import system\nsystem("pkill -f litellm")',
        'from os import popen\npopen("pkill -f litellm")',
        "code = 'kill -9 " + str(FIXTURE_PID) + "'\n"
        'import subprocess\nsubprocess.run(code, shell=True)',
    ]
    for code in cases:
        d = evaluate("python3 -c " + shlex.quote(code))
        assert d is not None and d["category"] == "VETO", code


def test_unprovable_dynamic_shell_sink_refuses(scope):
    # a dynamic (non-literal) command with shell=True is unprovable
    evaluate, _, _ = scope
    d = evaluate("python3 -c " + shlex.quote(
        'import subprocess\nv = "litellm"\n'
        'subprocess.run("pkill -f " + v, shell=True)'))
    assert d is not None
    assert d["category"] == "VETO"