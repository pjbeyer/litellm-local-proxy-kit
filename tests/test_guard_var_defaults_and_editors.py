"""Command-position ${VAR:-default} resolution (lpkit-uy4), stream
editor read/write distinction (lpkit-1hh), pipeline-stage plugin-dir
mutation coverage (found repro'ing 1hh), and sibling in-place editors
(perl -i, ruby -i, awk -i inplace) — permanent suite gate.

uy4: ${NAME:-default}/${NAME-default} in command position resolves
against the WRITTEN default when it is a statically analyzable simple
literal, and the substituted text is RE-ANALYZED in full (resolution
is analysis, not a hole): benign defaults allow (FR-004 — the
family-standup canonical invocation), signal-intent defaults veto,
env-literal beats the written default, and unresolvable forms stay
refused (FR-006).

1hh: a stream editor is a write ONLY in its in-place form; sed -n /
cat / head / grep / wc / awk reads of the plugin dir allow. In-place
writes against the plugin dir veto from the head AND from any
pipeline stage (FR-011): echo x | rm <plugin> no longer escapes.
Non-plugin in-place edits are out of scope (allow).

Offline: payloads are NEVER executed."""
import pytest

from test_lifecycle_guard import (FIXTURE_PID, write_policy)  # noqa: F401
from test_guard_quoted_control_flow import scope  # noqa: F401

A = chr(39)


def test_uy4_canonical_invocation_allows(scope):
    # lpkit-hf5: BYTE-EXACT filed payload (session 41a5c291): the
    # default is the VARIABLE $HOME/..., not a static literal —
    # gate tests must never substitute literals for variable
    # defaults (they mask unresolved-expansion refusals). $HOME is
    # allowlisted for one-level env resolution; resolved text is
    # re-analyzed in full.
    evaluate, _, _ = scope
    cmd = ('"${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/'
           'python3}" \\\n'
           '  "$HOME/.hermes/skills/productivity/family-standup/'
           'scripts/family_calendar_adapter.py" merged \\\n'
           '  --start "2026-11-13T00:00:00-05:00" \\\n'
           '  --end "2026-11-13T23:59:59-05:00" \\\n'
           '  --timezone America/New_York \\\n'
           '  --write-artifact \\\n'
           '  --target-date 2026-11-13 \\\n'
           '  --phase morning')
    assert evaluate(cmd) is None


@pytest.mark.parametrize("command", [
    '"${PY:-$HOME/venv/bin/python3}" script.py',
    '"${HERMES_PYTHON:-${HOME}/venv/bin/python3}" s.py',
    'PY=/usr/bin/python3; "${PY:-$HOME/venv/python3}" script.py',
])
def test_hf5_home_default_forms_allow(scope, command):
    evaluate, _, _ = scope
    assert evaluate(command) is None


@pytest.mark.parametrize("command", [
    '"${PY:-$HOME/bin/pkill}" -f litellm',   # kill under $HOME default
    'PY=$HOME/x; "${PY:-$HOME/bin/pkill}" -f litellm',
    '"${PY:-$OTHER/x/python3}" script.py',   # non-allowlisted: refuse
    '"${PY:-${HOME}${X}}" script.py',        # chained nesting: refuse
    'PY=/usr/bin/pkill; "${PY:-$HOME/venv/python3}" -f litellm',
])
def test_hf5_home_default_adversarial(scope, command):
    evaluate, _, _ = scope
    assert evaluate(command) is not None


@pytest.mark.parametrize("command", [
    '"${PY:-/usr/bin/python3}" script.py',
    '"${EDITOR:-vi}" file.txt',
    '${PY:-/usr/bin/python3} script.py',   # unquoted
    '"${PY-/usr/bin/python3}" script.py',  # hyphen form
    'PY=vi; "${EDITOR:-vi}" file.txt',     # env literal wins
])
def test_uy4_default_forms_allow(scope, command):
    evaluate, _, _ = scope
    assert evaluate(command) is None


def test_uy4_signal_intent_default_vetoes(scope):
    evaluate, _, _ = scope
    decision = evaluate('"${PY:-/usr/bin/pkill -f litellm}" x')
    assert decision is not None
    assert decision["category"] == "VETO"


def test_uy4_env_literal_beats_default(scope):
    evaluate, _, _ = scope
    decision = evaluate('PY=/usr/bin/pkill; "${PY:-vi}" x')
    assert decision is not None
    assert decision["category"] == "VETO"


@pytest.mark.parametrize("command", [
    '"${PY:-}" script.py',        # empty default: unprovable
    '"$UNRESOLVED_XYZ" script.py',  # no default, no literal
])
def test_uy4_unresolvable_stays_refused(scope, command):
    evaluate, _, _ = scope
    assert evaluate(command) is not None


PLUGIN = "/Users/pjbeyer/.hermes/plugins/proxy-lifecycle-guard"


@pytest.mark.parametrize("command", [
    f"sed -n '2950,3010p' {PLUGIN}/guard.py",
    f"sed -n '30,45p' {PLUGIN}/README.md",
    f"cat {PLUGIN}/guard.py",
    f"head -5 {PLUGIN}/guard.py",
    f"grep -n litellm {PLUGIN}/guard.py",
    f"wc -l {PLUGIN}/guard.py",
    f"awk 'NR>=5&&NR<=10' {PLUGIN}/guard.py",
    f"perl -ne 'print if /litellm/;' {PLUGIN}/guard.py",
    "sed -n '5p' /etc/hosts",
])
def test_1hh_plugin_dir_reads_allow(scope, command):
    evaluate, _, _ = scope
    assert evaluate(command) is None


@pytest.mark.parametrize("command", [
    f"sed -i.bak 's/a/b/' {PLUGIN}/guard.py",
    f"sed --in-place 's/a/b/' {PLUGIN}/config.json",
    f"perl -i -pe 's/a/b/' {PLUGIN}/guard.py",
    f"perl -pi -e 's/a/b/' {PLUGIN}/README.md",
    f"ruby -i -pe 's/a/b/' {PLUGIN}/guard.py",
    f"awk -i inplace '{{$0=\"x\"}}' {PLUGIN}/core.py",
    f"echo x | rm {PLUGIN}/guard.py",
    f"echo x | tee {PLUGIN}/guard.py",
    f"echo x | sed -i 's/a/b/' {PLUGIN}/guard.py",
    f"echo x | mv /tmp/y {PLUGIN}/guard.py",
    f"echo x | cp /tmp/y {PLUGIN}/guard.py",
    f"echo x | truncate -s0 {PLUGIN}/config.json",
    f"echo x | dd of={PLUGIN}/guard.py",
    f"echo x | perl -i -pe 's/a/b/' {PLUGIN}/guard.py",
    f"rm {PLUGIN}/guard.py",
    f"tee {PLUGIN}/guard.py",
])
def test_1hh_plugin_dir_writes_block(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["category"] == "VETO"


def test_1hh_non_plugin_inplace_out_of_scope(scope):
    evaluate, _, _ = scope
    assert evaluate("sed -i 's/a/b/' /tmp/unrelated.py") is None