"""Offline assertions for the public project operating baseline."""

import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _json(path):
    return json.loads((ROOT / path).read_text())


def _workflow(path):
    return yaml.safe_load(path.read_text())


def test_workflow_dependencies_are_commit_pinned():
    workflows = list((ROOT / '.github/workflows').glob('*.y*ml'))
    assert workflows
    refs = []
    for path in workflows:
        for job in _workflow(path)['jobs'].values():
            if 'uses' in job:
                refs.append(job['uses'])
            refs.extend(step['uses'] for step in job.get('steps', []) if 'uses' in step)
    assert refs
    for ref in refs:
        if not ref.startswith('./'):
            assert re.fullmatch(r'[^@]+@[0-9a-f]{40}', ref), ref


def test_release_is_owner_enabled_and_main_only():
    workflow = _workflow(ROOT / '.github/workflows/release-please.yml')
    # PyYAML uses YAML 1.1 rules for the unquoted word `on`.
    events = workflow.get('on', workflow.get(True))
    assert events == {'push': {'branches': ['main']}, 'workflow_dispatch': None}
    assert workflow['permissions'] == {}
    job = workflow['jobs']['release-please']
    assert job['if'] == "github.ref == 'refs/heads/main' && vars.ENABLE_RELEASE_PLEASE == 'true'"
    assert job['permissions'] == {'contents': 'write', 'pull-requests': 'write'}
    step, = job['steps']
    assert step['with'] == {
        'config-file': 'release-please-config.json',
        'manifest-file': '.release-please-manifest.json',
    }

    config = _json('release-please-config.json')
    manifest = _json('.release-please-manifest.json')
    assert re.fullmatch('[0-9a-f]{40}', config['bootstrap-sha'])
    assert set(manifest) == set(config['packages']) == {'.'}
    assert config['packages']['.'] == {
        'release-type': 'simple', 'version-file': 'VERSION', 'include-component-in-tag': False,
    }
    assert 'extra-files' not in config
    assert manifest['.'] == (ROOT / 'VERSION').read_text().strip()


def test_checked_in_spec_kit_is_the_hermes_shell_baseline():
    init = _json('.specify/init-options.json')
    assert (init['integration'], init['script']) == ('hermes', 'sh')
    registry = _json('.specify/extensions/.registry')['extensions']
    assert registry['agent-context']['enabled'] is True
    assert (ROOT / '.specify/scripts/bash/check-prerequisites.sh').is_file()
    assert '[PROJECT_NAME]' not in (ROOT / '.specify/memory/constitution.md').read_text()
