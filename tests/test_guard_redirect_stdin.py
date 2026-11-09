"""Plugin-dir redirect FP and pipe-to-interpreter FN classes (lpkit-z4b).

Finding A: a redirect only vetoes when its destination resolves inside
the deployed plugin dir. Reads with stderr suppression
(2>/dev/null) allow; writes/redirects into the dir still veto.
Finding B: kill payloads fed to stdin-consuming interpreters
(pipe-to-sh, awk system(), find -exec, docker lifecycle, inline
-e interpreter programs, heredoc interpreter bodies) veto, mirroring
the F-007 xargs precedent; benign shapes stay allowed.
Offline: payloads are NEVER executed."""
import shlex

import pytest

from test_guard_quoted_control_flow import scope  # noqa: F401

PDIR = "/Users/pjbeyer/.hermes/plugins/proxy-lifecycle-guard/guard.py"
PDIRE = "~/.hermes/plugins/proxy-lifecycle-guard/__pycache__/"
K = "pkill -f lit" + "ellm"


@pytest.mark.parametrize("command", [
    "cat " + PDIR + " 2>/dev/null | head -3",
    "ls -la " + PDIRE + " 2>/dev/null",
    "grep def " + PDIR + " 2>/dev/null | wc -l",
    "ls -la /tmp/missing 2>/dev/null",  # no marker at all
])
def test_plugin_dir_reads_with_stderr_redirect_pass(scope, command):
    evaluate, _, _ = scope
    assert evaluate(command) is None


@pytest.mark.parametrize("command", [
    "echo x > " + PDIR,
    "echo x 2> " + PDIR,
    "echo x >> " + PDIR,
    "cat /tmp/y >" + " ~/.hermes/plugins/proxy-lifecycle-guard/x",
])
def test_plugin_dir_redirect_writes_still_veto(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["category"] == "VETO"
    assert decision["log_code"] == "LGV-001"


@pytest.mark.parametrize("command", [
    "echo '" + K + "' | sh",
    "echo '" + K + "' | bash",
    "echo '" + K + "' | sh -s",
    "printf '%s\\n' '" + K + "' | sh",
    "echo '" + K + "' | tee /tmp/x | sh",
    "echo '" + K + "' | zsh",
])
def test_pipe_to_interpreter_kill_payloads_veto(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["category"] == "VETO"
    assert decision["log_code"] == "LGV-001"


@pytest.mark.parametrize("command", [
    "echo 'echo hello' | sh",
    "printf 'ls /tmp\\n' | bash",
    "cat notes.txt | sort | uniq | head -3",
])
def test_benign_pipe_to_interpreter_passes(scope, command):
    evaluate, _, _ = scope
    assert evaluate(command) is None


def test_awk_system_kill_vetoes(scope):
    evaluate, _, _ = scope
    decision = evaluate(
        "echo 1248 | awk '{system(\"kill -9 \" $1)}'")
    assert decision is not None
    assert decision["log_code"] == "LGV-001"


def test_awk_benign_program_passes(scope):
    evaluate, _, _ = scope
    assert evaluate("echo 5 | awk '{print $1 * 2}'") is None


@pytest.mark.parametrize("command", [
    "find /tmp -name x -exec kill -9 {} \\;",
    "find /tmp -name x -execdir kill -9 {} \\;",
])
def test_find_exec_kill_vetoes(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["log_code"] == "LGV-001"


def test_find_exec_benign_passes(scope):
    evaluate, _, _ = scope
    assert evaluate("find /tmp -name x -exec wc -l {} \\;") is None


@pytest.mark.parametrize("command", [
    "docker kill litellm-local 2>/dev/null || echo n/a",
    "docker stop litellm-proxy 2>/dev/null || echo n/a",
])
def test_docker_lifecycle_litellm_named_vetoes(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["log_code"] == "LGV-001"


def test_docker_read_only_passes(scope):
    evaluate, _, _ = scope
    assert evaluate("docker ps 2>/dev/null | head -3") is None


def test_python_heredoc_kill_vetoes(scope):
    evaluate, _, _ = scope
    decision = evaluate("python3 <<'EOF'\nos.kill(1248, 9)\nEOF")
    assert decision is not None
    assert decision["log_code"] == "LGV-001"


def test_python_heredoc_benign_passes(scope):
    evaluate, _, _ = scope
    assert evaluate("python3 <<'EOF'\nprint('benign')\nEOF") is None


@pytest.mark.parametrize("command", [
    "node -e 'process.kill(1248, 9)' 2>/dev/null || echo n/a",
    "ruby -e 'system \"kill -9 1248\"' 2>/dev/null || echo n/a",
    "perl -e 'kill 9, 1248' 2>/dev/null || echo n/a",
])
def test_inline_interpreter_programs_kill_vetoes(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["log_code"] == "LGV-001"


def test_exec_built_kill_via_execute_code_vetoes(scope):
    evaluate, _, _ = scope
    code = 'exec("import os\\nos.kill(1248, 9)")'
    decision = evaluate("python3 -c " + shlex.quote(code))
    assert decision is not None
    assert decision["log_code"] == "LGV-001"


def test_getattr_os_kill_indirection_vetoes(scope):
    evaluate, _, _ = scope
    code = "import os\nf = getattr(os, 'kill')\nf(1248, 9)"
    decision = evaluate("python3 -c " + shlex.quote(code))
    assert decision is not None
    assert decision["log_code"] == "LGV-001"


def test_benign_exec_string_passes(scope):
    evaluate, _, _ = scope
    code = 'exec("print(1 + 1)")'
    assert evaluate("python3 -c " + shlex.quote(code)) is None


def test_benign_getattr_passes(scope):
    evaluate, _, _ = scope
    code = "import os\nf = getattr(os, 'getpid')\nprint(f())"
    assert evaluate("python3 -c " + shlex.quote(code)) is None