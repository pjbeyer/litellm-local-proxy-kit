"""Offline contract checks for the kit's Spec Kit approval gate."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


EXTENSION = Path(__file__).resolve().parents[1] / '.specify/extensions/spec-validate'
FEATURE = '001-gate-fixture'


@pytest.fixture(params=['bash', 'pwsh'])
def gate(request, tmp_path):
    shell = request.param
    if not shutil.which(shell):
        pytest.skip(f'{shell} is not installed')
    extension = tmp_path / '.specify/extensions/spec-validate'
    shutil.copytree(EXTENSION / 'scripts', extension / 'scripts')
    status = extension / 'status'
    status.mkdir()
    spec = tmp_path / 'specs' / FEATURE / 'spec.md'
    spec.parent.mkdir(parents=True)
    spec.write_text('# Spec\n', encoding='utf-8')
    digest = 'sha256:' + hashlib.sha256(spec.read_bytes()).hexdigest()
    tasks = spec.with_name('tasks.md')
    tasks.write_text('# Tasks\n', encoding='utf-8')
    tasks_digest = 'sha256:' + hashlib.sha256(tasks.read_bytes()).hexdigest()
    if shell == 'bash':
        argv = [shell, str(extension / 'scripts/bash/check-gate.sh'), FEATURE]
    else:
        argv = [shell, '-NoProfile', '-File', str(extension / 'scripts/powershell/check-gate.ps1'), FEATURE]
    env = {k: v for k, v in os.environ.items() if not k.startswith('SPEC_VALIDATE_')}
    env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env['PATH']

    def check(state=None, *, path=None, full=False):
        if state is not None:
            record = state if isinstance(state, str) else json.dumps(state)
            (status / (FEATURE + '.json')).write_text(record, encoding='utf-8')
        check_env = env if path is None else {**env, 'PATH': str(path)}
        result = subprocess.run(argv, cwd=tmp_path, env=check_env, capture_output=True, text=True, timeout=20)
        output = json.loads(result.stdout)
        assert isinstance(output['actions_required'], list)
        assert isinstance(output['warnings'], list)
        if output['outcome'] == 'allowed-with-warning':
            assert output['warnings'] and not output['actions_required']
        return result.returncode, output if full else output['outcome']

    return check, spec, {
        'feature': FEATURE,
        'spec_self_validation': 'passed',
        'spec_hash': digest,
        'tasks_self_validation': 'passed',
        'tasks_hash': tasks_digest,
        'review_status': 'approved',
        'reviewed_spec_hash': digest,
        'tasks_review_status': 'approved',
        'reviewed_tasks_hash': tasks_digest,
        'approval_status': 'allowed',
    }


def test_no_record_blocks(gate):
    check, _, _ = gate
    assert check() == (1, 'blocked')


def test_exact_revision_approval_allows(gate):
    check, _, state = gate
    assert check(state) == (0, 'allowed')


def test_changed_revision_requires_validation(gate):
    check, spec, state = gate
    spec.write_text('# Revised spec\n', encoding='utf-8')
    assert check(state) == (1, 'blocked')


def test_missing_spec_blocks(gate):
    check, spec, state = gate
    spec.unlink()
    assert check(state) == (1, 'blocked')


@pytest.mark.parametrize('field', ['spec_hash', 'reviewed_spec_hash', 'tasks_hash', 'tasks_self_validation'])
def test_incomplete_approval_blocks(gate, field):
    check, _, state = gate
    del state[field]
    assert check(state) == (1, 'blocked')


def test_validation_does_not_refresh_peer_approval(gate):
    check, _, state = gate
    state['reviewed_spec_hash'] = 'sha256:' + '0' * 64
    assert check(state) == (1, 'blocked')


def test_invalid_approval_record_blocks(gate):
    check, _, _ = gate
    assert check('{invalid json') == (1, 'blocked')


def test_changed_tasks_require_validation(gate):
    check, spec, state = gate
    spec.with_name('tasks.md').write_text('# Revised tasks\n', encoding='utf-8')
    assert check(state) == (1, 'blocked')


@pytest.mark.parametrize('status', ['pending', 'timed-out', 'changes-requested', 'unknown'])
def test_unresolved_review_stays_blocked(gate, status):
    check, _, state = gate
    state.update(review_status=status, override={'used': True, 'reason': 'Fixture'},
                 timeout_self_approval={'used': True, 'reason': 'Fixture'})
    assert check(state) == (1, 'blocked')


ENABLED = 'gate:\n  allow_maintainer_override: true\n'


def _write_config(tmp_path, text=ENABLED):
    config = tmp_path / '.specify/extensions/spec-validate/spec-validate-config.yml'
    config.write_text(text, encoding='utf-8')


def _record_override(state):
    state.update(review_status='pending', override={
        'used': True, 'reason': 'Schedule dependency', 'by': 'Maintainer',
        'spec_hash': state['spec_hash'], 'tasks_hash': state['tasks_hash'],
    })


@pytest.mark.parametrize('status, expected', [
    ('pending', (0, 'allowed-with-warning')),
    ('timed-out', (0, 'allowed-with-warning')),
    ('approved', (0, 'allowed')),
    ('changes-requested', (1, 'blocked')),
    ('unknown', (1, 'blocked')),
])
def test_override_only_applies_while_waiting_for_spec_review(gate, tmp_path, status, expected):
    check, _, state = gate
    _record_override(state)
    state['review_status'] = status
    _write_config(tmp_path)
    assert check(state) == expected


@pytest.mark.parametrize('review', ['spec', 'tasks'])
def test_powershell_override_can_use_python_when_python3_is_absent(request, gate, tmp_path, review):
    if request.node.callspec.params['gate'] != 'pwsh' or os.name == 'nt':
        pytest.skip('POSIX-hosted PowerShell fallback fixture')
    check, _, state = gate
    if review == 'spec':
        _record_override(state)
        _write_config(tmp_path)
    else:
        _record_task_override(state)
        _write_config(tmp_path, TASK_OVERRIDE_ENABLED)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'pwsh').symlink_to(shutil.which('pwsh'))
    # A symlink outside the venv can lose its pyvenv.cfg and thus PyYAML.
    python = bin_dir / 'python'
    python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n', encoding='utf-8')
    python.chmod(0o700)
    assert check(state, path=bin_dir) == (0, 'allowed-with-warning')


@pytest.mark.parametrize('key, value', [
    ('used', 'true'), ('used', False), ('reason', ' '), ('by', None),
    ('spec_hash', None), ('tasks_hash', 'sha256:' + '0' * 64),
])
def test_invalid_or_stale_override_blocks(gate, tmp_path, key, value):
    check, _, state = gate
    _record_override(state)
    state['override'][key] = value
    _write_config(tmp_path)
    assert check(state) == (1, 'blocked')


@pytest.mark.parametrize('text', [
    None,
    'gate:\n  allow_maintainer_override: false\n',
    'review:\n  allow_maintainer_override: true\n',
    'gate:\n  allow_maintainer_override: "true"\n',
    ENABLED + '  allow_maintainer_override: false\n',
    'gate: [\n',
])
def test_override_requires_config_permission(gate, tmp_path, text):
    check, _, state = gate
    _record_override(state)
    if text is not None:
        _write_config(tmp_path, text)
    assert check(state) == (1, 'blocked')


@pytest.mark.parametrize('key, value', [
    ('spec_hash', 'sha256:' + '0' * 64), ('tasks_hash', 'sha256:' + '0' * 64),
    ('spec_self_validation', 'failed'), ('tasks_self_validation', 'failed'),
    ('tasks_review_status', 'pending'), ('reviewed_tasks_hash', None),
])
def test_override_preserves_validation_and_task_review(gate, tmp_path, key, value):
    check, _, state = gate
    state[key] = value
    _record_override(state)
    _write_config(tmp_path)
    assert check(state) == (1, 'blocked')


TASK_OVERRIDE_ENABLED = 'gate:\n  allow_task_review_override: true\n'


def _record_task_override(state):
    state.update(tasks_review_status='pending', task_review_override={
        'used': True, 'reason': 'Schedule dependency', 'by': 'Maintainer',
        'feature': FEATURE, 'spec_hash': state['spec_hash'], 'tasks_hash': state['tasks_hash'],
    })
    state.pop('reviewed_tasks_hash', None)


@pytest.mark.parametrize('disposition, expected', [
    ('pending', (0, 'allowed-with-warning')),
    ('timed-out', (1, 'blocked')),
    ('changes-requested', (1, 'blocked')),
    ('unknown', (1, 'blocked')),
    (None, (1, 'blocked')),
    ('approved', (1, 'blocked')),  # No matching peer-review digest.
    ('not-required', (0, 'allowed')),
])
def test_task_override_only_applies_to_pending_review(gate, tmp_path, disposition, expected):
    check, _, state = gate
    _record_task_override(state)
    state['tasks_review_status'] = disposition
    _write_config(tmp_path, TASK_OVERRIDE_ENABLED)
    assert check(state) == expected


@pytest.mark.parametrize('config', [
    None,
    ENABLED,
    'gate:\n  allow_task_review_override: false\n',
    'gate:\n  allow_task_review_override: "true"\n',
    'review:\n  allow_task_review_override: true\n',
    TASK_OVERRIDE_ENABLED + '  allow_task_review_override: true\n',
    TASK_OVERRIDE_ENABLED + 'gate: {}\n',
    'gate: [\n',
])
def test_task_override_requires_separate_opt_in(gate, tmp_path, config):
    check, _, state = gate
    _record_task_override(state)
    if config is not None:
        _write_config(tmp_path, config)
    assert check(state) == (1, 'blocked')


@pytest.mark.parametrize('field, value', [
    ('used', 'true'), ('used', False), ('by', ''), ('reason', '   '),
    ('feature', '002-other-feature'), ('spec_hash', None), ('tasks_hash', 'sha256:' + '0' * 64),
])
def test_task_override_requires_complete_matching_receipt(gate, tmp_path, field, value):
    check, _, state = gate
    _record_task_override(state)
    state['task_review_override'][field] = value
    _write_config(tmp_path, TASK_OVERRIDE_ENABLED)
    assert check(state) == (1, 'blocked')


@pytest.mark.parametrize('field, value', [
    ('feature', '002-other-feature'), ('feature', None),
    ('spec_self_validation', 'failed'), ('tasks_self_validation', 'failed'),
    ('review_status', 'pending'), ('review_status', 'timed-out'),
    ('review_status', 'changes-requested'), ('review_status', 'unknown'),
    ('reviewed_spec_hash', 'sha256:' + '0' * 64),
])
def test_task_override_preserves_other_gate_requirements(gate, tmp_path, field, value):
    check, _, state = gate
    _record_task_override(state)
    state[field] = value
    _write_config(tmp_path, TASK_OVERRIDE_ENABLED)
    assert check(state) == (1, 'blocked')


@pytest.mark.parametrize('artifact', ['spec', 'tasks'])
@pytest.mark.parametrize('revalidated', [False, True])
def test_task_override_cannot_survive_artifact_change(gate, tmp_path, artifact, revalidated):
    check, spec, state = gate
    state['review_status'] = 'not-required'
    _record_task_override(state)
    path = spec.with_name(f'{artifact}.md')
    path.write_text('# Revised artifact\n', encoding='utf-8')
    if revalidated:
        state[f'{artifact}_hash'] = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
    _write_config(tmp_path, TASK_OVERRIDE_ENABLED)
    assert check(state) == (1, 'blocked')


def test_independent_overrides_report_each_waiver_without_writing_state(gate, tmp_path):
    check, _, state = gate
    _record_override(state)
    _record_task_override(state)
    _write_config(tmp_path, ENABLED + '  allow_task_review_override: true\n')
    code, result = check(state, full=True)
    assert code == 0 and result['outcome'] == 'allowed-with-warning'
    assert len(result['warnings']) == 2
    assert 'Unresolved spec review overridden by recorded maintainer authorization' in result['warnings']
    assert 'Pending task review overridden by recorded maintainer authorization' in result['warnings']
    status_path = tmp_path / f'.specify/extensions/spec-validate/status/{FEATURE}.json'
    assert status_path.read_text(encoding='utf-8') == json.dumps(state)


def test_task_opt_in_does_not_reuse_spec_receipt(gate, tmp_path):
    check, _, state = gate
    _record_override(state)
    state.update(review_status='approved', tasks_review_status='pending')
    _write_config(tmp_path, ENABLED + '  allow_task_review_override: true\n')
    assert check(state) == (1, 'blocked')


def test_task_override_does_not_downgrade_current_peer_approval(gate, tmp_path):
    check, _, state = gate
    _record_task_override(state)
    state.update(tasks_review_status='approved', reviewed_tasks_hash=state['tasks_hash'])
    _write_config(tmp_path, TASK_OVERRIDE_ENABLED)
    assert check(state) == (0, 'allowed')


def test_review_not_required_allows_current_validation(gate):
    check, _, state = gate
    state.pop('reviewed_spec_hash')
    state['review_status'] = 'not-required'
    assert check(state) == (0, 'allowed')


@pytest.mark.parametrize('review_status', [None, 'pending', 'timed-out', 'changes-requested', 'unknown', ['not-required']])
def test_tasks_need_independent_review_disposition(gate, review_status):
    check, _, state = gate
    state.update(review_status='not-required', tasks_review_status=review_status)
    assert check(state) == (1, 'blocked')


def test_stale_tasks_review_blocks_even_after_revalidation(gate):
    check, spec, state = gate
    tasks = spec.with_name('tasks.md')
    tasks.write_text('# Revised tasks\n', encoding='utf-8')
    state['tasks_hash'] = 'sha256:' + hashlib.sha256(tasks.read_bytes()).hexdigest()
    assert check(state) == (1, 'blocked')


def test_task_review_can_be_not_required(gate):
    check, _, state = gate
    state.pop('reviewed_tasks_hash')
    state['tasks_review_status'] = 'not-required'
    assert check(state) == (0, 'allowed')


def test_bash_state_writer_preserves_literal_path(tmp_path):
    state_dir = tmp_path / "operator's worktree"
    script = EXTENSION / 'scripts/bash/write-approval-state.sh'
    state = {'spec_self_validation': 'passed', 'approval_status': 'blocked'}
    state_dir.mkdir()
    existing = state_dir / f'{FEATURE}.json'
    existing.write_text('{}', encoding='utf-8')
    existing.chmod(0o644)
    env = dict(os.environ, SPEC_VALIDATE_STATE_DIR=str(state_dir))
    subprocess.run(['bash', str(script), FEATURE, json.dumps(state)], env=env,
                   capture_output=True, check=True, timeout=20)
    assert json.loads(existing.read_text()) == state
    assert existing.stat().st_mode & 0o777 == 0o600


SECURITY_SCRIPTS = EXTENSION.parent / 'security-review/scripts'


@pytest.mark.parametrize('shell', ['bash', 'pwsh'])
def test_review_scope_includes_deletions_and_new_files(tmp_path, shell):
    if not shutil.which(shell):
        pytest.skip(f'{shell} is not installed')
    file = tmp_path / 'retired-check.txt'
    file.write_text('Fixture\n', encoding='utf-8')
    changed_type = tmp_path / 'type-change.txt'
    changed_type.write_text('Fixture\n', encoding='utf-8')
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'add', file.name, changed_type.name], cwd=tmp_path, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
                    'commit', '-qm', 'Fixture'], cwd=tmp_path, check=True)
    file.unlink()
    changed_type.unlink()
    changed_type.symlink_to('new check.txt')
    (tmp_path / '.git/info/exclude').write_text('ignored.txt\n', encoding='utf-8')
    (tmp_path / 'ignored.txt').write_text('Local fixture\n', encoding='utf-8')
    added = tmp_path / 'new check.txt'
    added.write_text('New fixture\n', encoding='utf-8')
    if shell == 'bash':
        argv = [shell, str(SECURITY_SCRIPTS / 'bash/detect-changed-files.sh'), '--json']
    else:
        argv = [shell, '-NoProfile', '-File',
                str(SECURITY_SCRIPTS / 'powershell/detect-changed-files.ps1'), '-Json']
    result = subprocess.run(argv, cwd=tmp_path, check=True, capture_output=True, text=True, timeout=20)
    assert sorted(json.loads(result.stdout)['changed_files']) == sorted([added.name, file.name, changed_type.name])


@pytest.mark.parametrize(('body', 'count', 'category'), [
    ('# Review\n\nNo findings.\n', 0, '[]'),
    ('# Review\n\n### [MEDIUM] Fixture\n\nA05:2025\n', 1, '[A05:2025]'),
    ('# Review\n\n| Severity | Count | Percentage |\n| Low | 3 | 100% |\n', 3, '[]'),
])
def test_review_header_counts_findings(tmp_path, body, count, category):
    report = tmp_path / 'review.md'
    report.write_text(body, encoding='utf-8')
    subprocess.run(['bash', str(SECURITY_SCRIPTS / 'update-document-headers.sh'), str(report)],
                   env=dict(os.environ, TMPDIR=str(tmp_path)), check=True, capture_output=True, timeout=20)
    header = report.read_text(encoding='utf-8').split('---', 2)[1].splitlines()
    assert f'total_findings: {count}' in header
    assert f'owasp_categories: {category}' in header
