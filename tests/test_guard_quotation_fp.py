"""Quotation-blind newline splitting and F-017 read/write separation.

Two FP classes from real session refusals:
- A: a real newline inside a quoted argument (git multi-paragraph -m
  convention). Valid Python with quoted newlines allows; the byte-exact
  refused payload (raw newline in a single-quoted string — invalid
  Python) refuses with the corrective bound category, never a lifecycle
  VETO; fail-closed on malformed input is preserved.
- B: F-017 narrowed to write primitives. Reads of the deployed
  directory pass on every surface; writes (open with writing mode,
  pathlib write family, redirect) still veto.
Offline: payloads are NEVER executed."""
import os
import shlex

import pytest

from test_guard_quoted_control_flow import scope  # noqa: F401
from test_lifecycle_guard import FIXTURE_PORT

MSG2 = "Small message.\n\nCloses litest-6c5."
# Publication hygiene: no absolute workstation paths in the tree. The
# deployed plugin path is composed from $HOME at runtime.
READ_PATH = os.path.expanduser(
    "~/.hermes/plugins/proxy-lifecycle-guard/guard.py")


@pytest.mark.parametrize("code", [
    'import subprocess\n'
    'subprocess.run(["git", "commit", "-m", ' + repr(MSG2) + '])',
    'import subprocess\n'
    'subprocess.run(["git", "commit", "-m", ' +
    repr("msg.\n\nsecond paragraph") + '])',
])
def test_quoted_newline_commit_messages_pass(scope, code):
    evaluate, _, _ = scope
    assert evaluate("python3 -c " + shlex.quote(code)) is None


def test_quoted_newline_signal_intent_as_data_passes(scope):
    # lpkit-0gk/90d posture change: a commit MESSAGE (or any inert
    # argv data) containing kill-family text is DEAD DATA, not an
    # execution path. It now passes (consistent with the terminal
    # `git commit -m` surface, which always allowed the same string).
    # Genuine execution of the signal (os.system/subprocess shell /
    # kill argv) still vetoes — pinned by the red battery.
    evaluate, _, _ = scope
    code = (
        'import subprocess\n'
        'subprocess.run(["git", "commit", "-m", ' +
        repr("msg.\n\n" + "pkill -f litellm" + " done.") + '])')
    assert evaluate("python3 -c " + shlex.quote(code)) is None


def test_terminal_quoted_newline_commit_passes(scope):
    evaluate, _, _ = scope
    command = 'git commit -m "Small message.\n\nCloses issue-1."'
    assert evaluate(command) is None


def test_byte_exact_invalid_python_newline_refuses_shape_not_veto(scope):
    # The byte-exact class from the real refusal: a raw newline inside
    # a single-quoted Python string is invalid Python. It cannot be
    # statically proven safe, so it stays REFUSED — with the corrective
    # shape category (IR-008: under-cap syntax failures are shape
    # defects, not size defects), never a lifecycle VETO signature.
    evaluate, _, _ = scope
    code = (
        "t('git commit -m ' + '\"Small message.\n"
        "\nCloses litest-6c5.\"', 'commit')")
    decision = evaluate("python3 -c " + shlex.quote(code))
    assert decision is not None
    assert decision["category"] == "SHAPE_ENVELOPE"
    assert decision["log_code"] == "LGV-020"


@pytest.mark.parametrize("code", [
    "data = open(" + repr(READ_PATH) + ").read()\nprint(len(data))",
    "data = open(" + repr(READ_PATH) + ", mode='r').read()\n"
    "print(len(data))",
    "from pathlib import Path\n"
    "print(len(Path(" + repr(READ_PATH) + ").read_text()))",
])
def test_deployed_directory_reads_pass(scope, code):
    evaluate, _, _ = scope
    assert evaluate("python3 -c " + shlex.quote(code)) is None


@pytest.mark.parametrize("code", [
    "f = open(" + repr(READ_PATH) + ", 'w')\nf.write('x')\nf.close()",
    "f = open(os.path.expanduser('~/.hermes/plugins/"
    "proxy-lifecycle-guard/x.log'), 'a')\nf.write('x')",
    "f = open(os.path.expanduser('~/.hermes/plugins/"
    "proxy-lifecycle-guard/x.new'), mode='x')",
    "from pathlib import Path\n"
    "Path(" + repr(READ_PATH) + ").write_text('x')",
    "from pathlib import Path\n"
    "Path(os.path.expanduser('~/.hermes/plugins/"
    "proxy-lifecycle-guard/x.b')).write_bytes(b'x')",
])
def test_deployed_directory_writes_still_veto(scope, code):
    evaluate, _, _ = scope
    decision = evaluate("python3 -c " + shlex.quote(code))
    assert decision is not None
    assert decision["category"] == "VETO"
    assert decision["log_code"] == "LGV-001"


@pytest.mark.parametrize("command", [
    "cat " + READ_PATH + " | head -1",
    "wc -c " + READ_PATH,
    "grep def " + READ_PATH,
])
def test_deployed_directory_terminal_reads_pass(scope, command):
    evaluate, _, _ = scope
    assert evaluate(command) is None


def test_deployed_directory_terminal_write_still_vetoes(scope):
    evaluate, _, _ = scope
    decision = evaluate("echo x > " + READ_PATH)
    assert decision is not None
    assert decision["category"] == "VETO"