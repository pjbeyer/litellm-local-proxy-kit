"""Composition-aware heredoc routing and FR-013 amendment-5a
counter-cases (lpkit-5tb; spec amendment 5a, review findings 1-3).

The heredoc body's destination is determined by the FULL composition
— head command, any pipeline consumer, any redirection into an
executing interpreter — never the head alone. Bodies reaching an
interpreter through any enumerated composition receive full signal-
intent inspection; only grammar-proven non-executing destinations are
prose data. Misclassification fails toward inspection, never
excision.

FR-013 counter-cases (amendment 5a), all remain blocked:
(i) cat << EOF | sh carrying a kill-family verb — composition case
(ii) heredoc directly feeding an interpreter with a kill verb
(iii) expanding (unquoted-delimiter) heredoc bodies — $() executes
per POSIX regardless of destination
(iv) quoted payload carrying signal-call text on a non-analyzing verb
(v) multi-apostrophe quote-pairing leaving a kill verb unquoted and
executing — canonical vectors pinned
(vi) unquoted word-internal apostrophe stays a conservative refusal
Offline: payloads are NEVER executed."""
import shlex

import pytest

from test_guard_quoted_control_flow import scope  # noqa: F401
from test_lifecycle_guard import FIXTURE_PID  # noqa: F401

A = chr(39)  # apostrophe
K = "pkill -f lit" + "ellm"


@pytest.mark.parametrize("command", [
    "cat << 'EOF' | sh\n" + K + "\nEOF",
    "cat << 'EOF' | bash\n" + K + "\nEOF",
    "cat << 'EOF' | sh -s\n" + K + "\nEOF",
    "cat << 'EOF' | sort | sh\n" + K + "\nEOF",
    "cat << 'EOF' | tee /tmp/x | sh\n" + K + "\nEOF",
])
def test_counter_case_i_piped_heredoc_body_vetoes(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["category"] == "VETO"
    assert decision["log_code"] == "LGV-001"


def test_counter_case_i_python_consumer_routes_to_code_analysis(scope):
    evaluate, _, _ = scope
    command = ("cat << 'EOF' | python3\n"
               "import os\nos.kill(1248, 9)\nEOF")
    decision = evaluate(command)
    assert decision is not None
    assert decision["category"] == "VETO"


def test_counter_case_ii_direct_interpreter_heredoc_vetoes(scope):
    evaluate, _, _ = scope
    decision = evaluate("sh <<'EOF'\n" + K + "\nEOF")
    assert decision is not None
    assert decision["category"] == "VETO"


@pytest.mark.parametrize("command", [
    # expanding heredocs: $() executes per POSIX regardless of the
    # write destination (amendment 5a: unquoted delimiter is NOT a
    # prose span)
    "cat << EOF\necho note $(pkill -f litellm) done\nEOF",
    "cat >> /tmp/x.md << EOF\nnote $(pkill -f litellm)\nEOF",
])
def test_counter_case_iii_expanding_heredoc_vetoes(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["category"] == "VETO"


def test_counter_case_iv_quoted_signal_operand_vetoes(scope):
    evaluate, _, _ = scope
    decision = evaluate("some_cmd " + A + "pkill -f litellm" + A)
    assert decision is not None
    assert decision["category"] == "VETO"


def test_inert_verbs_carrying_signal_text_still_pass(scope):
    # documented inert-text carve-out: echo/grep of signal words are
    # source data, not operands of an arbitrary executor
    evaluate, _, _ = scope
    assert evaluate("grep -rn 'kill' docs/README.md") is None
    assert evaluate("echo 'do not kill the proxy'") is None


@pytest.mark.parametrize("command", [
    # two prose apostrophes change real-shell quote pairing, leaving
    # the kill verb UNQUOTED and executing
    "echo " + A + "don" + A + "t worry; pkill -f litellm",
    "echo it" + A + "s " + A + "fine; kill -9 " + str(FIXTURE_PID),
])
def test_counter_case_v_multi_apostrophe_pairing_vetoes(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["category"] == "VETO"


def test_counter_case_vi_unquoted_apostrophe_still_refused(scope):
    evaluate, _, _ = scope
    decision = evaluate("echo it" + A + "s-fine")
    assert decision is not None
    assert decision["category"] == "VETO"


@pytest.mark.parametrize("command", [
    # prose pass-cases (amendment 5) must stay green
    "cat >> /tmp/d.md << 'EOF'\nthe reviewer" + A + "s note\nEOF\n"
    "echo done",
    # grammar-proven non-expanding quoted delimiter: $() is literal
    "cat >> /tmp/x.md << 'EOF'\nnote $(pkill -f litellm)\nEOF\n"
    "echo done",
    # benign body piped to sh: no signal intent
    "cat << 'EOF' | sh\necho hello\nEOF",
])
def test_prose_pass_cases_stay_allowed(scope, command):
    evaluate, _, _ = scope
    assert evaluate(command) is None