"""Prose-context apostrophes, command-position variables, and pinned
analysis-literal dispositions (lpkit-knp / lpkit-5ix / lpkit-3bj).

knp: heredoc bodies with non-interpreter heads are DATA and are
excised before parsing; shell comments are stripped as prose. An
apostrophe in reviewer's/don't prose no longer makes the chunk
unparseable. Genuinely unparseable shell (unquoted word-internal
apostrophe) stays refused — fail-closed preserved. Interpreter-headed
heredoc bodies still route to their analyzers (z4b pin).

5ix: a variable in command position resolves against simple literal
assignments visible in the same payload (adapter pattern). Resolution
is analysis: a resolved kill command still vetoes. Unresolvable
indirection stays refused (FR-006).

3bj: pinned dispositions — signal text carried as string data stays
BLOCK (documented source-data rule); unprovable analysis cells get
the corrective bound category; listing compounds allow.
Offline: payloads are NEVER executed."""
import shlex

import pytest

from test_guard_quoted_control_flow import scope  # noqa: F401

A = chr(39)  # apostrophe


@pytest.mark.parametrize("command", [
    "cat >> /tmp/d.md << 'EOF'\nthe reviewer" + A + "s note\nEOF\necho done",
    "cat >> /tmp/d.md << 'EOF'\nthe reviewer note\nEOF\necho done",
    "echo hi # don" + A + "t worry",
    "grep -rn x docs/ # reviewer" + A + "s note",
    # kill text in a cat-body is DATA: never executes
    "cat >> /tmp/d.md << 'EOF'\npkill -f litellm\nEOF\necho done",
])
def test_prose_apostrophe_contexts_pass(scope, command):
    evaluate, _, _ = scope
    assert evaluate(command) is None


def test_unparseable_unquoted_apostrophe_still_refused(scope):
    evaluate, _, _ = scope
    decision = evaluate("echo it" + A + "s-fine")
    assert decision is not None
    assert decision["category"] == "VETO"


def test_paired_quote_apostrophe_passes(scope):
    evaluate, _, _ = scope
    assert evaluate("echo " + A + "ok" + A) is None


@pytest.mark.parametrize("command", [
    "sh <<'EOF'\npkill -f litellm\nEOF",
    "python3 <<'EOF'\nimport os\nos.kill(1248, 9)\nEOF",
])
def test_interpreter_heredoc_bodies_still_veto(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["category"] == "VETO"
    assert decision["log_code"] == "LGV-001"


def test_python_comment_apostrophe_passes(scope):
    evaluate, _, _ = scope
    code = ("qs = '/tmp/tool-call-gate.json'\n"
            "# replicate engine" + A + "s fingerprint inputs\n"
            "print(qs)")
    assert evaluate("python3 -c " + shlex.quote(code)) is None


@pytest.mark.parametrize("command", [
    'PY="python3"\n"$PY" "$HOME/skills/a.py" merged --start "a"',
    'X="echo"\n"$X" hello',
    'X="echo"\n$X hello',
    'X="cat"\n"$X" "file.txt"',
])
def test_command_position_variable_resolves_passes(scope, command):
    evaluate, _, _ = scope
    assert evaluate(command) is None


@pytest.mark.parametrize("command", [
    '$X hi',                      # no visible assignment
    'X="$(cmd)"\n"$X" hi',        # substitution value: unresolvable
])
def test_unresolvable_command_position_refused(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["category"] == "VETO"


def test_resolved_kill_command_still_vetoes(scope):
    evaluate, _, _ = scope
    decision = evaluate('X="pkill -f litellm"\n"$X"')
    assert decision is not None
    assert decision["category"] == "VETO"
    assert decision["log_code"] == "LGV-001"


def test_signal_text_as_data_still_blocks(scope):
    # documented source-data rule (README): quoted program inspection
    # grants no exception for signal text carried as data
    evaluate, _, _ = scope
    code = 'rows = {"cleanup": "pkill -f litellm"}\nprint(len(rows))'
    decision = evaluate("python3 -c " + shlex.quote(code))
    assert decision is not None
    assert decision["category"] == "VETO"


def test_analysis_cell_unprovable_gets_bound_category(scope):
    evaluate, _, _ = scope
    code = ("t('git commit -m ' + '\"Small message.\n"
            "\nCloses litest-6c5.\"', 'commit')")
    decision = evaluate("python3 -c " + shlex.quote(code))
    assert decision is not None
    assert decision["category"] == "BOUND_SIZE"
    assert decision["log_code"] == "LGV-010"


def test_listing_compound_with_plugin_dir_legs_passes(scope):
    evaluate, _, _ = scope
    command = ("bd projects list; ls -la "
               "~/.hermes/plugins/proxy-lifecycle-guard/__pycache__/ "
               "2>/dev/null; grep -c def "
               "~/.hermes/plugins/proxy-lifecycle-guard/guard.py")
    assert evaluate(command) is None