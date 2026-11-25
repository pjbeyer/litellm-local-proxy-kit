"""IR-007 synthetic, offline-only command resolution; never execute payloads."""
import hashlib
import inspect
import json
import os
from pathlib import Path

import pytest

from test_guard_quoted_control_flow import scope  # noqa: F401


@pytest.fixture(autouse=True)
def inert_unrelated_expansion_environment(monkeypatch):
    # Prevent ambient shell settings from changing synthetic vectors.
    for name in ('P', 'PY', 'HERMES_PYTHON', 'EDITOR'):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("command", [
    '$HOME/bin/python3 -c "print(1)"',
    '${HOME}/bin/python3 -c "print(1)"',
    '"$HOME/bin/python3" -c "print(1)"',
    '"${HOME}/bin/python3" -c "print(1)"',
    'printf x | $HOME/bin/python3 -c "print(1)"',
    '$HOME/bin/python3 -c "print(1)" | cat',
    'printf x | ${HOME}/bin/python3 -c "print(1)" | cat',
    'HOME=/fixture/other; $HOME/bin/python3 -c "print(1)"',
    'A=ok; HOME=/fixture/other; $HOME/bin/python3 -c "print(1)"',
    'A=ok\nHOME=/fixture/other\n"${PY:-$HOME/bin/python3}" -c "print(1)"',
    'HOME=/fixture/other; "${PY:-$HOME/bin/python3}" -c "print(1)"',
    'HOME=/fixture/other; PY="${P:-$HOME/bin/python3}"; "$PY" -c "print(1)"',
    'HOME=/fixture/other; $HOME/bin/python3 -c "print(1)" | "${P:-$HOME/bin/python3}" -c "print(1)"',
])
def test_home_commands_are_fully_analyzed(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', 'unsafe inherited home')
    if 'HOME=/fixture/other' not in command:
        monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    assert evaluate(command) is None
    assert seen == ['print(1)'] * (2 if ' | "${P:-' in command else 1)


@pytest.mark.parametrize('command', [
    '"${PY:-$HOME/bin/python3}" -c "print(1)"; HOME=/fixture/other',
    '$HOME/bin/python3 -c "print(1)"; HOME=/fixture/other',
    '$HOME/bin/python3 -c "print(1)" | "${PY:-$HOME/bin/python3}" -c "print(1)"; HOME=/fixture/other',
])
def test_home_assignment_after_use_does_not_override_inherited(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert seen == []


@pytest.mark.parametrize('binding', [
    'HOME=', 'HOME=""', 'HOME=$UNKNOWN', 'HOME="$HOME/recursive"',
    'HOME="unsafe value"', 'HOME=/fixture/a; HOME=/fixture/b',
    'HOME=/fixture/a; HOME=',
    'HOME=/fixture/a; env HOME=/fixture/b echo x',
    'HOME=/fixture/other echo x',
    'HOME=/fixture/a; HOME=/fixture/' + 'x' * 4096,
])
@pytest.mark.parametrize('command', [
    '$HOME/bin/python3 -c "print(1)"',
    '"${PY:-$HOME/bin/python3}" -c "print(1)"',
])
def test_shadowed_empty_unsafe_recursive_and_ambiguous_refuse(scope, monkeypatch, binding, command):
    monkeypatch.setenv('HOME', '/fixture/inherited')
    evaluate, _, _ = scope
    decision = evaluate(binding + '; ' + command)
    assert decision is not None and decision['category'] == 'VETO'


@pytest.mark.parametrize('home', [None, '', 'bad value', '/fixture/$OTHER', '/fixture/' + 'x' * 4096])
@pytest.mark.parametrize('command', ['$HOME/bin/python3', '"${PY:-$HOME/bin/python3}"'])
def test_inherited_home_must_be_exact_safe_nonempty_and_bounded(scope, monkeypatch, home, command):
    if home is None:
        monkeypatch.delenv('HOME', raising=False)
    else:
        monkeypatch.setenv('HOME', home)
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'


@pytest.mark.parametrize('command', [
    '$HOME/bin/python3',
    '"${PY:-$HOME/bin/python3}"',
])
def test_assignment_count_bound_refuses(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    assignments = '; '.join(f'V{i}=ok' for i in range(33))
    evaluate, _, _ = scope
    decision = evaluate(assignments + '; ' + command)
    assert decision is not None and decision['category'] == 'VETO'


@pytest.mark.parametrize('command', [
    "'$HOME/bin/python3' -c 'print(1)'",
    "'${HOME}/bin/python3' -c 'print(1)'",
    "'${PY:-$HOME/bin/python3}' -c 'print(1)'",
    '$HOMEOTHER/bin/python3',
    '"${PY:-$HOMEOTHER/bin/python3}"',
    '$HOME/bin/$OTHER',
    r'\$HOME/bin/python3',
    'HOME=/fixture/other $HOME/bin/python3 -c "print(1)"',
    'env HOME=/fixture/other $HOME/bin/python3 -c "print(1)"',
    'command $HOME/bin/python3 -c "print(1)"',
])
def test_literals_prefix_collision_and_unknown_quotes_do_not_expand(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert seen == []


@pytest.mark.parametrize('command', [
    '$HOME/bin/pkill -f litellm',
    'echo x | ${HOME}/bin/pkill -f litellm',
    'HOME=/fixture/other; "${PY:-$HOME/bin/pkill}" -f litellm',
])
def test_resolved_protected_operations_still_veto(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, _, discovered = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert discovered == [4000]


@pytest.mark.parametrize('command', [
    'HOME=/fixture/ok; PY="$UNKNOWN"; "${PY:-$HOME/bin/python3}" -c "print(1)"',
    'HOME=/fixture/ok; PY=; "${PY:-$HOME/bin/python3}" -c "print(1)"',
    'HOME=/fixture/ok; PY=python3; PY="$UNKNOWN"; "${PY:-$HOME/bin/python3}" -c "print(1)"',
])
def test_unresolved_default_variable_does_not_fall_back(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert seen == []


@pytest.mark.parametrize('command', [
    "HOME=/fixture/other; PY='${P:-$HOME/bin/python3}'; \"$PY\" -c \"print(1)\"",
    'HOME=/fixture/other; PY="${P:-$HOMEOTHER/bin/python3}"; "$PY" -c "print(1)"',
    'HOME=/fixture/other; PY="${P:-$HOME/bin/$OTHER}"; "$PY" -c "print(1)"',
])
def test_assignment_default_quotes_and_prefix_collisions_refuse(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert seen == []


@pytest.mark.parametrize('command', [
    'env HOME=/fixture/other echo x; "${PY:-$HOME/bin/python3}" -c "print(1)"',
    'export HOME=/fixture/other; $HOME/bin/python3 -c "print(1)"',
    'PY="$HOME/bin/python3"; "$PY" -c "print(1)"',
])
def test_unsupported_assignment_shapes_refuse(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert seen == []


@pytest.mark.parametrize('command', [
    'true && PY=python3; "${PY:-$HOME/bin/python3}" -c "print(1)"',
    "echo 'x;PY=python3;'; \"${PY:-$HOME/bin/python3}\" -c \"print(1)\"",
    '"${PY:-$HOME/bin/python3}" -c "print(1)"; PY=python3',
])
def test_default_variable_visibility_must_be_provable(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert seen == []


@pytest.mark.parametrize('command', [
    'PY=/usr/bin/printf; PY=/fixture/bin/python3 "$HOME/bin/pkill" -f litellm',
    'PY=/usr/bin/printf; PY=; "${PY:-$HOME/bin/python3}" -c "print(1)"',
])
def test_inline_shadowed_command_binding_refuses(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'


@pytest.mark.parametrize('command', [
    '$HOME/bin/python3 -c "print(1)"; HOME=/fixture/other',
    "echo 'x;HOME=/fixture/other;'; $HOME/bin/python3 -c \"print(1)\"",
    'true && HOME=/fixture/other; $HOME/bin/python3 -c "print(1)"',
])
def test_home_binding_visibility_must_be_provable(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', 'unsafe inherited home')
    evaluate, seen, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert seen == []


def test_inherited_default_variable_is_not_assumed_unset(scope, monkeypatch):
    monkeypatch.setenv('HOME', '/fixture/home')
    monkeypatch.setenv('PY', '/fixture/other')
    evaluate, seen, _ = scope
    decision = evaluate('"${PY:-$HOME/bin/python3}" -c "print(1)"')
    assert decision is not None and decision['category'] == 'VETO'
    assert seen == []


@pytest.mark.parametrize('command', [
    'echo ready; HOME=/fixture/other; $HOME/bin/python3 -c "print(1)"',
    'echo ready\nHOME=/fixture/other\n"${HOME}/bin/python3" -c "print(1)"',
    'echo ready; HOME=/fixture/other; "${PY:-$HOME/bin/python3}" -c "print(1)"',
    'echo ready; HOME=/fixture/other; printf x | $HOME/bin/python3 -c "print(1)"',
])
def test_later_unconditional_home_assignment_is_visible(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', 'unsafe inherited home')
    evaluate, seen, _ = scope
    assert evaluate(command) is None
    assert seen == ['print(1)']


@pytest.mark.parametrize('early_operand', ['ready', '"$HOME"', '"${HOME}"'])
def test_data_only_home_reference_before_literal_assignment(scope, monkeypatch, early_operand):
    monkeypatch.setenv('HOME', 'unsafe inherited home')
    evaluate, seen, _ = scope
    command = f'echo {early_operand}; HOME=/fixture/other; "$HOME/bin/python3" -c "print(1)"'
    decision = evaluate(command)
    assert decision is None, decision
    assert seen == ['print(1)']


@pytest.mark.parametrize('early_command', [
    'printf "%s" "$HOME"',
    'echo "$HOME" | cat',
    'echo "$HOME" > /fixture/output',
    'command echo "$HOME"',
    'echo "${HOME:-default}"',
])
def test_ambiguous_home_use_before_literal_assignment_stays_unresolved(scope, monkeypatch, early_command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    decision = evaluate(f'{early_command}; HOME=/fixture/other; "$HOME/bin/python3" -c "print(1)"')
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'
    assert seen == []


@pytest.mark.parametrize('command', [
    'echo ready; X=echo; $X hello',
    'echo ready\nX="echo"\n"$X" hello',
])
def test_later_unconditional_literal_command_assignment_is_visible(scope, command):
    evaluate, seen, _ = scope
    assert evaluate(command) is None
    assert seen == []


def test_later_home_assignment_still_analyzes_protected_code(scope, monkeypatch):
    from test_lifecycle_guard import FIXTURE_PID
    monkeypatch.setenv('HOME', 'unsafe inherited home')
    evaluate, seen, _ = scope
    program = f'import os; os.kill({FIXTURE_PID}, 15)'
    decision = evaluate(f'echo ready; HOME=/fixture/other; $HOME/bin/python3 -c "{program}"')
    assert seen == [program]
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'


@pytest.mark.parametrize('command', [
    'echo ready; $HOME/bin/python3 -c "print(1)"; HOME=/fixture/other',
    'echo ready; true && HOME=/fixture/other; $HOME/bin/python3 -c "print(1)"',
    'echo ready; HOME=/fixture/a; HOME=/fixture/b; $HOME/bin/python3 -c "print(1)"',
    'echo ready; HOME=; $HOME/bin/python3 -c "print(1)"',
    'echo ready; HOME=$UNKNOWN; $HOME/bin/python3 -c "print(1)"',
    'echo ready; HOME=/fixture/other echo x; $HOME/bin/python3 -c "print(1)"',
    'echo ready; HOME=/fixture/other | cat; $HOME/bin/python3 -c "print(1)"',
    '(echo ready; HOME=/fixture/other;); $HOME/bin/python3 -c "print(1)"',
    'f() { echo ready; HOME=/fixture/other; }; $HOME/bin/python3 -c "print(1)"',
])
def test_later_ambiguous_home_binding_never_launders(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/inherited')
    evaluate, seen, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert seen == []


@pytest.mark.parametrize('data', [
    'echo "HOME=/fake"',
    "echo 'HOME=/fake'",
    'echo "x;HOME=/fake;"',
    "echo 'x;HOME=/fake;'",
])
@pytest.mark.parametrize('executable', [
    '$HOME/bin/python3',
    '"${HOME}/bin/python3"',
    '"${PY:-$HOME/bin/python3}"',
])
@pytest.mark.parametrize('binding', ['', 'HOME=/fixture/other; '])
@pytest.mark.parametrize('data_first', [False, True])
def test_quoted_home_data_does_not_shadow_or_duplicate_binding(
        scope, monkeypatch, data, executable, binding, data_first):
    monkeypatch.setenv('HOME', 'unsafe inherited home' if binding else '/fixture/home')
    evaluate, seen, _ = scope
    prefix = f'{data}; {binding}' if data_first else f'{binding}{data}; '
    command = f'{prefix}{executable} -c "print(1)"'
    assert evaluate(command) is None
    assert seen == ['print(1)']


@pytest.mark.parametrize('data', [
    'echo "HOME=/fake"',
    "echo 'x;HOME=/fake;'",
])
def test_quoted_home_data_cannot_rescue_unsafe_inherited_home(scope, monkeypatch, data):
    monkeypatch.setenv('HOME', 'unsafe inherited home')
    evaluate, seen, _ = scope
    fresh_guard = inspect.getclosurevars(evaluate).nonlocals['fresh_guard']
    assert 'HOME' not in fresh_guard._collect_literal_vars(data)
    decision = evaluate(f'{data}; $HOME/bin/python3 -c "print(1)"')
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'
    assert seen == []


@pytest.mark.parametrize('executable', ['$HOME/bin/python3', '"${PY:-$HOME/bin/python3}"'])
@pytest.mark.parametrize('binding', ['', 'HOME=/fixture/other; '])
def test_quoted_home_data_still_analyzes_protected_code(scope, monkeypatch, executable, binding):
    from test_lifecycle_guard import FIXTURE_PID
    monkeypatch.setenv('HOME', 'unsafe inherited home' if binding else '/fixture/home')
    evaluate, seen, _ = scope
    program = f'import os; os.kill({FIXTURE_PID}, 15)'
    decision = evaluate(f'{binding}echo "HOME=/fake"; {executable} -c "{program}"')
    assert seen == [program]
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'


@pytest.mark.parametrize('binding', [
    'HOME="unsafe value"',
    "HOME='unsafe value'",
    'HOME=',
    'HOME=$UNKNOWN',
    'env HOME=/fixture/other echo x',
    'HOME=/fixture/other echo x',
    'true && HOME=/fixture/other',
    'HOME=/fixture/a; HOME=/fixture/b',
])
def test_real_home_shadow_with_quoted_data_never_inherits(scope, monkeypatch, binding):
    monkeypatch.setenv('HOME', '/fixture/inherited')
    evaluate, seen, _ = scope
    fresh_guard = inspect.getclosurevars(evaluate).nonlocals['fresh_guard']
    assert fresh_guard._collect_literal_vars(f'{binding}; echo "HOME=/fake"').get('HOME', 'missing') is None
    command = f'echo "HOME=/fake"; {binding}; $HOME/bin/python3 -c "print(1)"'
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'
    assert seen == []


@pytest.mark.parametrize('command', [
    'export "HOME=/fixture/other"',
    "export 'HOME=/fixture/other'",
    'env "HOME=/fixture/other" echo x',
    'readonly "HOME=/fixture/other"',
    'declare "HOME=/fixture/other"',
])
@pytest.mark.parametrize('preceding', ['', 'HOME=/fixture/safe; '])
def test_quoted_assignment_argument_to_binding_builtin_never_inherits(
        scope, monkeypatch, command, preceding):
    monkeypatch.setenv('HOME', '/fixture/inherited')
    evaluate, seen, _ = scope
    decision = evaluate(f'{preceding}{command}; $HOME/bin/python3 -c "print(1)"')
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'
    assert seen == []


@pytest.mark.parametrize('command', [
    'echo ready; $X hello; X=echo',
    'echo ready; true && X=echo; $X hello',
    'echo "x;X=echo;"; $X hello',
    'echo ready; X=echo; X=cat; $X hello',
    'echo ready; X=; $X hello',
    'echo ready; X=$UNKNOWN; $X hello',
    'echo ready; X=echo command; $X hello',
    'echo ready; X=echo | cat; $X hello',
])
def test_later_ambiguous_command_binding_never_launders(scope, command):
    evaluate, _, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'


SHADOW_HEAD_WORDS = ('export', 'readonly', 'declare', 'typeset', 'local', 'env')
# Verbs the guard proves print/match their operands. Only these demote a
# binding-head word to data; every other owner stays fail-closed.
DATA_VERBS = ('echo', 'printf', 'cat', 'grep')


@pytest.mark.parametrize('head', SHADOW_HEAD_WORDS)
@pytest.mark.parametrize('executable', [
    '$HOME/bin/python3',
    '"${HOME}/bin/python3"',
    '"${PY:-$HOME/bin/python3}"',
])
@pytest.mark.parametrize('binding', ['', 'HOME=/fixture/other; '])
@pytest.mark.parametrize('data_first', [True, False])
def test_binding_head_word_as_echo_data_never_shadows_home(
        scope, monkeypatch, head, executable, binding, data_first):
    """A head-shaped word inside echo's arguments is data, not a binding.

    ``echo export "HOME=/fake"`` prints both words; no HOME binding is
    created, so an inherited (or standalone-bound) safe HOME must resolve
    and the inner command must be analyzed in full. The quoted operand is
    unreachable as a binding because echo never executes its arguments.
    """
    monkeypatch.setenv('HOME', 'unsafe inherited home' if binding else '/fixture/home')
    evaluate, seen, _ = scope
    data = f'echo {head} "HOME=/fake"'
    command = (f'{data}; {binding}' if data_first
               else f'{binding}{data}; ')
    decision = evaluate(f'{command}{executable} -c "print(1)"')
    assert decision is None
    assert seen == ['print(1)']


@pytest.mark.parametrize('verb', DATA_VERBS)
@pytest.mark.parametrize('head', ['export', 'declare', 'env'])
@pytest.mark.parametrize('quoted', ['"HOME=/fake"', "'HOME=/fake'"])
def test_every_data_verb_demotes_binding_head_word_to_data(
        scope, monkeypatch, verb, head, quoted):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    decision = evaluate(f'{verb} {head} {quoted}; $HOME/bin/python3 -c "print(1)"')
    assert decision is None
    assert seen == ['print(1)']


@pytest.mark.parametrize('command', [
    'printf %s export "HOME=/fake"; $HOME/bin/python3 -c "print(1)"',
    'echo ready export "HOME=/fake"; $HOME/bin/python3 -c "print(1)"',
    'cat /dev/null export "HOME=/fake"; $HOME/bin/python3 -c "print(1)"',
    'echo "x export HOME=/fake"; $HOME/bin/python3 -c "print(1)"',
    'printf %s export "HOME=/fake"; "${PY:-$HOME/bin/python3}" -c "print(1)"',
    'grep export /dev/null "HOME=/fake"; $HOME/bin/python3 -c "print(1)"',
])
def test_head_word_in_non_command_data_position_is_not_a_shadow(
        scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    assert evaluate(command) is None
    assert seen == ['print(1)']


@pytest.mark.parametrize('command', [
    # Same data-shaped word, but behind owners this guard does NOT prove are
    # argument-only: arbitrary verbs, executors, grouping/function scope.
    'xargs export "HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'ls export "HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'git export "HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'head -1 export "HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'find . -name x export "HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'env export "HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'true export "HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'echo export "HOME=/fake"; ls export "HOME=/fixture/other"; '
    '$HOME/bin/python3 -c "print(1)"',
    'echo export "HOME=/fake"; sh -c \'export HOME=/fixture/other; '
    '$HOME/bin/python3 -c "print(1)"\'',
    'echo export "HOME=/fake"; { export "HOME=/fixture/other"; }; '
    '$HOME/bin/python3 -c "print(1)"',
    'echo export "HOME=/fake"; export "HOME=/fixture/other"; '
    '$HOME/bin/python3 -c "print(1)"',
    'echo export "HOME=/fake"; env HOME=/fixture/other '
    '$HOME/bin/python3 -c "print(1)"',
    'echo export "HOME=/fake"; true && export "HOME=/fixture/other"; '
    '$HOME/bin/python3 -c "print(1)"',
])
def test_unproven_owner_of_binding_head_word_stays_fail_closed(
        scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/inherited')
    evaluate, seen, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'
    assert seen == []


@pytest.mark.parametrize('head', SHADOW_HEAD_WORDS)
@pytest.mark.parametrize('quoted', ['"HOME=/fixture/other"', "'HOME=/fixture/other'"])
@pytest.mark.parametrize('preceding', ['', 'HOME=/fixture/safe; '])
def test_real_binding_head_home_operand_still_refuses(
        scope, monkeypatch, head, quoted, preceding):
    """A head in command position with a HOME operand stays fail-closed."""
    monkeypatch.setenv('HOME', '/fixture/inherited')
    evaluate, seen, _ = scope
    decision = evaluate(
        f'{preceding}{head} {quoted}; $HOME/bin/python3 -c "print(1)"')
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'
    assert seen == []


@pytest.mark.parametrize('command', [
    'command env "HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'HOME=/fixture/safe; env "HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'sudo export "HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'sudo -u root export "HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'true && local "HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    '{ export "HOME=/fixture/other"; }; $HOME/bin/python3 -c "print(1)"',
    'f() { declare "HOME=/fixture/other"; }; $HOME/bin/python3 -c "print(1)"',
    'nohup /bin/export "HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
])
def test_wrapped_or_grouped_binding_head_still_refuses(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/inherited')
    evaluate, seen, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'
    assert seen == []


@pytest.mark.parametrize('command', [
    'sudo -u root echo export "HOME=/fake"; $HOME/bin/python3 -c "print(1)"',
    'command echo export "HOME=/fake"; $HOME/bin/python3 -c "print(1)"',
    'nohup echo export "HOME=/fake"; $HOME/bin/python3 -c "print(1)"',
    'timeout 5 echo export "HOME=/fake"; $HOME/bin/python3 -c "print(1)"',
    'A=1 echo export "HOME=/fake"; $HOME/bin/python3 -c "print(1)"',
])
def test_wrapped_data_verb_still_resolves_home(scope, monkeypatch, command):
    """The shared wrapper logic peeled the prefix and found a data verb."""
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    assert evaluate(command) is None
    assert seen == ['print(1)']


@pytest.mark.parametrize('binding', ['', 'HOME=/fixture/other; '])
@pytest.mark.parametrize('executable', ['$HOME/bin/python3', '"${PY:-$HOME/bin/python3}"'])
def test_echo_data_head_word_still_analyzes_protected_code(
        scope, monkeypatch, binding, executable):
    from test_lifecycle_guard import FIXTURE_PID
    monkeypatch.setenv('HOME', 'unsafe inherited home' if binding else '/fixture/home')
    evaluate, seen, _ = scope
    program = f'import os; os.kill({FIXTURE_PID}, 15)'
    decision = evaluate(
        f'{binding}echo export "HOME=/fake"; {executable} -c "{program}"')
    assert seen == [program]
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'


@pytest.mark.parametrize('command', [
    # Not a HOME binding at all: the operand only contains the word HOME=.
    'export "XHOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'declare XHOME=/fixture/other; $HOME/bin/python3 -c "print(1)"',
    'echo export "XHOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
])
def test_home_lookalike_operand_is_not_a_shadow(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    assert evaluate(command) is None
    assert seen == ['print(1)']


@pytest.mark.parametrize('command', [
    # The HOME= text is not the start of the decoded operand, so it cannot
    # be a binding; only the prefix form HOME=… can shadow. A substring
    # test here would falsely refuse and lose inherited HOME resolution.
    # (A TRAILING space — "HOME=/x " — still prefixes the decoded word
    # and is a real declare/export binding; it refuses by design.)
    'export " HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'readonly " HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
    'declare "x=1 HOME=/fixture/other"; $HOME/bin/python3 -c "print(1)"',
])
def test_home_inside_but_not_prefix_of_operand_is_not_a_shadow(
        scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    assert evaluate(command) is None
    assert seen == ['print(1)']


@pytest.mark.parametrize('command', [
    # Unquoted HOME= data: a printed/matched operand is not a binding, so a
    # safe inherited HOME stays eligible. Mirrors the round-4 review P2 for
    # the unquoted branch of the quoted round-3 finding.
    'echo export HOME=/fake; $HOME/bin/python3 -c "print(1)"',
    'echo HOME=/fake; $HOME/bin/python3 -c "print(1)"',
    "printf '%s\\n' export HOME=/fake; $HOME/bin/python3 -c \"print(1)\"",
    'grep HOME= /fixture/notes; $HOME/bin/python3 -c "print(1)"',
])
def test_unquoted_home_data_still_resolves_home(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    assert evaluate(command) is None
    assert seen == ['print(1)']


@pytest.mark.parametrize('command', [
    'nice echo export HOME=/fake; $HOME/bin/python3 -c "print(1)"',
    'nice -n 5 echo export HOME=/fake; $HOME/bin/python3 -c "print(1)"',
    '/usr/bin/nice echo export HOME=/fake; $HOME/bin/python3 -c "print(1)"',
])
def test_wrapped_unquoted_home_data_still_resolves_home(
        scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    assert evaluate(command) is None
    assert seen == ['print(1)']


def test_unquoted_home_data_still_analyzes_protected_code(
        scope, monkeypatch):
    from test_lifecycle_guard import FIXTURE_PID
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    program = f'import os; os.kill({FIXTURE_PID}, 15)'
    decision = evaluate(
        f'echo export HOME=/fake; $HOME/bin/python3 -c "{program}"')
    assert seen == [program]
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'


@pytest.mark.parametrize('command', [
    # Real command-position bindings keep refusing even beside unquoted data.
    'export HOME=/fake; $HOME/bin/python3 -c "print(1)"',
    'env HOME=/fake echo x; $HOME/bin/python3 -c "print(1)"',
    'HOME=/fake echo x; $HOME/bin/python3 -c "print(1)"',
    # An unresolvable real binding plus data mentions must not inherit either.
    'HOME=$UNKNOWN; echo export HOME=/fake; $HOME/bin/python3 -c "print(1)"',
    'HOME=; echo export HOME=/fake; $HOME/bin/python3 -c "print(1)"',
])
def test_real_bindings_refuse_despite_unquoted_data(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/inherited')
    evaluate, seen, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'
    assert seen == []


def test_safe_standalone_binding_resolves_despite_unquoted_data(
        scope, monkeypatch):
    """The proven standalone binding wins; the echo argument is data."""
    monkeypatch.setenv('HOME', 'unsafe inherited home')
    evaluate, seen, _ = scope
    fresh_guard = inspect.getclosurevars(evaluate).nonlocals['fresh_guard']
    assert fresh_guard._collect_literal_vars(
        'HOME=/fixture/other; echo export HOME=/fake'
    )['HOME'] == '/fixture/other'
    command = ('HOME=/fixture/other; echo export HOME=/fake; '
               '$HOME/bin/python3 -c "print(1)"')
    assert evaluate(command) is None
    assert seen == ['print(1)']


@pytest.mark.parametrize('command', [
    'sudo nice -n 5 echo export HOME=/fake; $HOME/bin/python3 -c "print(1)"',
    'nice -n 5 sudo echo HOME=/fake; $HOME/bin/python3 -c "print(1)"',
])
def test_nested_wrapped_unquoted_home_data_still_resolves_home(
        scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    assert evaluate(command) is None
    assert seen == ['print(1)']


@pytest.mark.parametrize('command', [
    # A shell function can override a data verb and bind HOME through the
    # wrapper body, so a function definition anywhere in the payload defeats
    # data-verb trust: the plain HOME= mention stays ambiguous and refuses.
    'echo() { export "$1"; }; echo HOME=/fixture/other; $HOME/bin/python3 -c "print(1)"',
    'f() { export "$1"; }; echo HOME=/fixture/other; $HOME/bin/python3 -c "print(1)"',
    'function echo { export "$1"; }; echo HOME=/fixture/other; $HOME/bin/python3 -c "print(1)"',
    # Definitions nested inside control flow bind through the same body.
    'if true; then echo() { export "$1"; }; fi; echo HOME=; $HOME/bin/python3 -c "print(1)"',
    'if true; then f() { export "$1"; }; else :; fi; echo HOME=; $HOME/bin/python3 -c "print(1)"',
    'while false; do g() { export "$1"; }; break; done; echo HOME=; $HOME/bin/python3 -c "print(1)"',
    'if true; then function echo { export "$1"; }; fi; echo HOME=; $HOME/bin/python3 -c "print(1)"',
    # Definitions after grouping braces bind through the same body.
    '{ echo() { export "$1"; }; }; echo HOME=/fixture/other; $HOME/bin/python3 -c "print(1)"',
    'case x in a) h() { export "$1"; };; esac; echo HOME=; $HOME/bin/python3 -c "print(1)"',
])
def test_function_definition_defeats_data_verb_trust(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'
    assert seen == []


def test_command_wrapper_data_verb_still_resolves_home(scope, monkeypatch):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    command = 'command echo HOME=/fake; $HOME/bin/python3 -c "print(1)"'
    assert evaluate(command) is None
    assert seen == ['print(1)']


@pytest.mark.parametrize('command', [
    # A definition SHAPE inside quotes is printed data, not a definition:
    # the position-free function gate must be quote-aware.
    "echo 'function fake' HOME=/fake; $HOME/bin/python3 -c \"print(1)\"",
    "echo 'f() {}' HOME=/fake; $HOME/bin/python3 -c \"print(1)\"",
])
def test_quoted_function_shape_is_data_not_definition(scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    assert evaluate(command) is None
    assert seen == ['print(1)']


def test_escaped_quote_before_definition_still_refuses(scope, monkeypatch):
    """An escaped quote is a literal character, not a quote opener: the
    definition after it is unquoted and must defeat data-verb trust."""
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    command = ('echo \\"; echo() { export "$1"; }; echo HOME=/fake; '
               '$HOME/bin/python3 -c "print(1)"')
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'
    assert seen == []


def test_escaped_quote_without_definition_still_resolves_home(
        scope, monkeypatch):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    command = 'echo \\" HOME=/fake; $HOME/bin/python3 -c "print(1)"'
    assert evaluate(command) is None
    assert seen == ['print(1)']


def test_near_limit_quoted_shapes_stay_single_pass(scope, monkeypatch):
    """Thousands of definition shapes inside one quoted operand must not
    turn the definition scan quadratic: the quote state advances in one
    pass and the safe inherited HOME stays eligible."""
    import time
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    payload = ('echo "' + 'function x ' * 5000 +
               'HOME=/fake"; $HOME/bin/python3 -c "print(1)"')
    fresh_guard = inspect.getclosurevars(evaluate).nonlocals['fresh_guard']
    start = time.perf_counter()
    result = fresh_guard._collect_literal_vars(payload)
    elapsed = time.perf_counter() - start
    # The quoted HOME= data must not create a binding: no HOME key at all
    # means no suppression of the inherited value.
    assert 'HOME' not in result
    assert evaluate(payload) is None
    assert seen == ['print(1)']
    # Deterministic bound: the scan is linear; allow generous margin for
    # machine variance but catch any return to per-match rescanning.
    assert elapsed < 2.0, f'definition scan took {elapsed:.3f}s'


def test_trailing_space_binding_operand_still_refuses(scope, monkeypatch):
    """A trailing space still prefixes the decoded word: a real binding
    whose value is not a simple literal must refuse, not inherit."""
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    decision = evaluate(
        'declare "HOME=/fixture/other " ; $HOME/bin/python3 -c "print(1)"')
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'
    assert seen == []


@pytest.mark.parametrize('command', [
    r'hello \HOME=/fake; $HOME/bin/python3 -c "print(1)"',
    'echo $(echo "HOME=/fake"); $HOME/bin/python3 -c "print(1)"',
    'echo `echo HOME=/fake`; $HOME/bin/python3 -c "print(1)"',
])
def test_escaped_and_substitution_interior_home_mentions_still_refuse(
        scope, monkeypatch, command):
    monkeypatch.setenv('HOME', '/fixture/home')
    evaluate, seen, _ = scope
    fresh_guard = inspect.getclosurevars(evaluate).nonlocals['fresh_guard']
    assert fresh_guard._collect_literal_vars(command).get('HOME', 'missing') is None
    decision = evaluate(command)
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'
    assert seen == []


def test_ir010_synthetic_manifest_one_named_delta(scope, monkeypatch):
    manifest = json.loads((Path(__file__).parent / 'fixtures' / 'ir007-home-synthetic.json').read_text())
    assert manifest['provenance'] == 'synthetic; not a historical replay'
    assert manifest['delta']['element'] == 'environment.HOME'
    assert manifest['delta']['before'] == '/fixture/home'
    assert manifest['delta']['after'] == ''
    assert manifest['tool'] == 'terminal'
    # The pair contains no default-variable reference; HOME is the only
    # environment input that can affect command resolution.
    assert manifest['relevant_environment'] == {'HOME': '/fixture/home', 'PY': 'unset', 'P': 'unset'}
    from test_lifecycle_guard import MODULE_PATH, write_policy
    evaluate, seen, discovered = scope
    closure = inspect.getclosurevars(evaluate).nonlocals
    policy_bytes = closure['policy'].read_bytes()
    assert json.loads(policy_bytes) == manifest['policy']
    assert hashlib.sha256(policy_bytes).hexdigest() == manifest['policy_sha256']
    snapshot = inspect.getclosurevars(closure['discover']).nonlocals['snapshot']
    assert {'pids': sorted(snapshot.pids), 'pgids': sorted(snapshot.pgids),
            'comms': sorted(snapshot.comms), 'args': list(snapshot.args)} == manifest['discovery']
    assert hashlib.sha256(MODULE_PATH.with_name('__init__.py').read_bytes()).hexdigest() == manifest['adapter_sha256']
    assert hashlib.sha256(Path(inspect.getfile(evaluate)).read_bytes()).hexdigest() == manifest['scope_fixture_sha256']
    assert hashlib.sha256(Path(inspect.getfile(write_policy)).read_bytes()).hexdigest() == manifest['policy_fixture_sha256']
    raw = bytes.fromhex(manifest['payload_utf8_hex'])
    assert hashlib.sha256(raw).hexdigest() == manifest['payload_sha256']
    assert raw.decode('utf-8') == '$HOME/bin/python3 -c "print(1)"'
    evaluate, seen, _ = scope
    monkeypatch.setenv('HOME', manifest['delta']['before'])
    assert evaluate(raw.decode('utf-8')) is None
    assert seen == ['print(1)']
    seen.clear()
    monkeypatch.setenv('HOME', manifest['delta']['after'])
    decision = evaluate(raw.decode('utf-8'))
    assert decision is not None and decision['category'] == 'VETO'
    assert decision['log_code'] == 'LGV-001'
    assert seen == []
