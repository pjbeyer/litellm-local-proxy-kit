"""lpkit-ovj: nested same-char quoting in command substitution (terminal).

FP class (2026-11-13 Friday Flash publication): `--params "$(python3 -c
"...")"` — valid bash; bash reparses the substitution interior in a NEW
quote context, so the inner double-quotes neither close the outer arg
nor terminate the substitution. The chunk splitter previously treated
the interior `"`/`;` as top-level state, fragmenting the payload
(`gws ... "$(python3 -c "import json` | `print(1)")"`), and the
unterminated fragment fail-closed as an LGV-001 kill-workflow VETO for
a benign Gmail-draft write.

Fix direction (chosen): substitution-aware splitting — the splitter
tracks $( / ` regions with their own interior quote state and never
splits on separators inside them; the interior still receives its own
recursive shell analysis, so kill intent inside a substitution is
never lost (adversarial gates below).

Secondary fix (parity with lpkit-1cz on the tool_call surface):
unparseable shell shapes (shlex "No closing quotation", unterminated
$( ) refuse with SHAPE_ENVELOPE/LGV-020 corrective guidance, never the
kill-workflow VETO wording that misleads operators.
"""
import pytest

from test_lifecycle_guard import write_policy
from test_guard_quoted_control_flow import scope  # noqa: F401


def test_ovj_exact_shape_allows(scope):
    # byte-shape of the 2026-11-16 veto (msg 1020143): two nested
    # json.dumps substitutions as CLI arguments — benign, must allow
    evaluate, _, _ = scope
    cmd = ('cd ~/Projects/work/workspace && gws gmail users drafts create '
           '--params "$(python3 -c "import json; print(json.dumps({\'a\': 1}))")" '
           '--json "$(python3 -c "import json; print(json.dumps({\'b\': 2}))")"')
    assert evaluate(cmd) is None


def test_semicolon_inside_substitution_allows(scope):
    # the `;` in "import json; print(1)" is an INNER-program separator
    evaluate, _, _ = scope
    assert evaluate('gws x --params "$(python3 -c "import json; print(1)")"') is None


def test_subst_inside_double_quotes_inner_quote_not_closing(scope):
    # inner `"` must not close the outer quoted arg (fragmentation root)
    evaluate, _, _ = scope
    assert evaluate('gws x --params "$(python3 -c "print(1)")"') is None


def test_bd_git_variants_allow(scope):
    evaluate, _, _ = scope
    assert evaluate('bd sql "$(python3 -c "print(\'select 1\')")"') is None
    assert evaluate('git commit -m "$(python3 -c "print(\'msg\')")"') is None


def test_backtick_form_allows(scope):
    evaluate, _, _ = scope
    assert evaluate('gws x --params "`python3 -c "print(1)"`"') is None


def test_nested_subst_interior_allows(scope):
    evaluate, _, _ = scope
    assert evaluate('gws x --params "$(echo "$(echo hi)")"') is None


def test_absolute_path_redirect_variant_allows(scope):
    # ovj cmd 2 shape: absolute binary + stdout redirect
    evaluate, _, _ = scope
    assert evaluate('/opt/homebrew/bin/gws x --params '
                    '"$(python3 -c "print(1)")" > out.json') is None


# --- adversarial: kill intent inside a substitution still vetoes ---
def test_kill_inside_substitution_vetoes(scope):
    evaluate, _, _ = scope
    d = evaluate('gws x --params "$(pkill litellm)"')
    assert d is not None and d["category"] == "VETO"


def test_kill_body_substitution_vetoes(scope):
    evaluate, _, _ = scope
    from test_lifecycle_guard import FIXTURE_PID
    d = evaluate(f'echo "$(kill {FIXTURE_PID})"')
    assert d is not None and d["category"] == "VETO"


def test_os_kill_python_inside_substitution_vetoes(scope):
    evaluate, _, _ = scope
    d = evaluate('gws x --params "$(python3 -c "import os; os.kill(9, 15)")"')
    assert d is not None and d["category"] == "VETO"


def test_plugin_dir_write_inside_substitution_vetoes(scope):
    evaluate, _, _ = scope
    d = evaluate('echo "$(rm ~/.hermes/plugins/proxy-lifecycle-guard/guard.py)"')
    assert d is not None and d["category"] == "VETO"


def test_kill_after_substitution_outer_chain_vetoes(scope):
    evaluate, _, _ = scope
    d = evaluate('gws x --params "$(python3 -c "import json; print(1)")" '
                 '&& pkill litellm')
    assert d is not None and d["category"] == "VETO"


def test_pipeline_to_kill_inside_substitution_vetoes(scope):
    evaluate, _, _ = scope
    d = evaluate('gws x --params "$(echo hi | pkill litellm)"')
    assert d is not None and d["category"] == "VETO"


# --- shape class: unprovable refuses SHAPE_ENVELOPE, never the VETO ---
def test_unterminated_substitution_refuses_shape(scope):
    evaluate, _, _ = scope
    d = evaluate('gws x --params "$(python3 -c "print(1)"')
    assert d is not None
    assert d["category"] == "SHAPE_ENVELOPE"
    assert d["log_code"] == "LGV-020"


def test_unterminated_quote_refuses_shape(scope):
    evaluate, _, _ = scope
    d = evaluate('gws x --params "unterminated')
    assert d is not None
    assert d["category"] == "SHAPE_ENVELOPE"
    assert d["log_code"] == "LGV-020"


def test_shape_message_is_corrective_not_kill_workflow(scope):
    evaluate, _, _ = scope
    d = evaluate('gws x --params "unterminated')
    assert d is not None
    # the misleading kill-workflow wording must NOT appear on shape refusals
    assert "kill command" not in d["message"]
    assert "controlled restart" not in d["message"]


# --- lpkit-7kb: no benign parse failure surfaces kill-workflow wording ---
# The last remaining leak site: an unterminated heredoc raised a plain
# _Block() (LGV-001 kill-workflow message) for a benign incomplete
# command. It is a shape failure — refused SHAPE_ENVELOPE/LGV-020 now.
def test_unterminated_heredoc_refuses_shape(scope):
    evaluate, _, _ = scope
    d = evaluate('cat <<EOF')
    assert d is not None
    assert d["category"] == "SHAPE_ENVELOPE"
    assert d["log_code"] == "LGV-020"


def test_unterminated_heredoc_message_is_corrective(scope):
    evaluate, _, _ = scope
    d = evaluate('cat <<EOF')
    assert d is not None
    assert "kill command" not in d["message"]
    assert "controlled restart" not in d["message"]


def test_heredoc_with_terminator_still_vetoes_kill_body(scope):
    # the SHAPE routing must not weaken real heredoc composition gates:
    # a terminated heredoc feeding a kill pipeline still vetoes
    evaluate, _, _ = scope
    d = evaluate("cat <<'EOF' | sh\npkill -f litellm\nEOF")
    assert d is not None
    assert d["category"] == "VETO"
    assert d["log_code"] == "LGV-001"