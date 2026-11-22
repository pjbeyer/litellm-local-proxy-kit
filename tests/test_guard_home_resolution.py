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


def test_ir010_synthetic_manifest_one_named_delta(scope, monkeypatch):
    manifest = json.loads((Path(__file__).parent / 'fixtures' / 'ir007-home-synthetic.json').read_text())
    assert manifest['provenance'] == 'synthetic; not a historical replay'
    assert manifest['delta']['element'] == 'environment.HOME'
    assert manifest['delta']['before'] == '/fixture/home'
    assert manifest['delta']['after'] == ''
    assert manifest['tool'] == 'terminal'
    # The pair contains no default-variable reference; HOME is the only
    # environment input that can affect command resolution.
    assert manifest['relevant_environment'] == {'HOME': 'delta', 'PY': 'unset', 'P': 'unset'}
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
    assert seen == []
