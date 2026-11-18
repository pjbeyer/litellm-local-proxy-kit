"""Proof worker and observation channel: isolated fixtures only.

Real git tree, real worker process, real socketpair channel; no production
listener, credential store or service-control call is used.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'core'))
from restart_control.engine import Fault, atomic_json
from restart_control.runtime import Runtime
from restart_control.runtime_identity import KIT_INPUTS, PROXY_INPUTS, file_fact
from restart_lifecycle_adapter import FixtureLifecycleAdapter


@pytest.fixture
def private_root():
    with tempfile.TemporaryDirectory(prefix='pw-', dir=os.environ['TMPDIR']) as name:
        yield Path(name)


@pytest.fixture
def provenance_tree(private_root):
    """A real pinned git tree with a litellm package, like the runtime suite."""
    root = private_root
    source = root/'litellm'
    source.mkdir()
    (source/'__init__.py').write_text('VALUE = 1\n')
    info = root/'litellm-1.2.3.dist-info'
    info.mkdir()
    (info/'METADATA').write_text('Name: litellm\nVersion: 1.2.3\n')
    (info/'RECORD').write_text('litellm/__init__.py,,\n')
    env = {'HOME': str(root), 'PATH': os.defpath, 'TMPDIR': os.environ['TMPDIR'],
           'PYTHONPATH': str(root), 'PYTHONDONTWRITEBYTECODE': '1'}

    def git(*args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(root), *args],
                                       env=env, stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q')
    git('add', 'litellm')
    git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', 'fixture')
    tip = git('rev-parse', 'HEAD')
    (root/'pin.lock').write_text('base_version = "1.2.3"\npatch_tip = "'+tip+'"\n')
    return root, env, tip


@pytest.fixture
def proof_runtime(private_root, provenance_tree):
    """A Runtime whose kit/controller/proxy trees are staged for the worker.

    The pinned interpreter is a real throwaway venv with an editable-style
    litellm mapping (pth + dist-info with direct_url.json), mirroring the
    installed layout the -I -B worker resolves in production.
    """
    tree, tree_env, tip = provenance_tree
    venv = private_root/'venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)],
                   check=True, capture_output=True, timeout=60)
    venv_python = venv/'bin'/'python'
    site_dir = next((venv/'lib').glob('python*/site-packages'))
    dist = site_dir/'litellm-1.2.3.dist-info'
    dist.mkdir()
    (dist/'METADATA').write_text('Name: litellm\nVersion: 1.2.3\n')
    (dist/'RECORD').write_text('litellm/__init__.py,,\n')
    (dist/'direct_url.json').write_text(json.dumps(
        {'url': 'file://'+str(tree), 'dir_info': {'editable': True}}))
    (site_dir/'fixture-litellm.pth').write_text(str(tree)+'\n')
    env = {'HOME': str(tree), 'PATH': os.defpath, 'TMPDIR': os.environ['TMPDIR'],
           'PYTHONDONTWRITEBYTECODE': '1'}
    kit, config = private_root/'kit', private_root/'config'
    config.mkdir()
    for name in KIT_INPUTS:
        target = kit/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/name, target)
    label = 'fixture.local.proxy'
    for name in (*PROXY_INPUTS, label+'.plist', label+'.rotate.plist'):
        (config/name).write_text('fixture-owned input\n')
    (config/'credentials.toml').write_text(
        '[[credential]]\nenv_var = "FIXTURE_HTTP_KEY"\nop_ref = "op://fixture/item/field"\nrequired = true\n')
    (config/'pin.lock').write_text((tree/'pin.lock').read_text())
    job = {'Label': label, 'WorkingDirectory': str(config),
           'ProgramArguments': ['/bin/bash', str(config/'run-proxy-wrapper.sh'), '49131'],
           'EnvironmentVariables': {'LITELLM_PROXY_LABEL': label,
              'LITELLM_PROXY_CONFIG': str(config/'config.rendered.yaml'),
              'CREDENTIALS_MANIFEST': str(config/'credentials.toml'),
              'LITELLM_PROXY_PYTHON': str(venv_python), 'LITELLM_DEPLOYMENT': 'fixture'},
           'KeepAlive': True, 'RunAtLoad': True, 'ThrottleInterval': 30}
    import plistlib
    data = plistlib.dumps(job)
    (config/(label+'.plist')).write_bytes(data)
    auto = private_root/'autoload.plist'
    auto.write_bytes(data)
    deployment = private_root/'deployment'
    (deployment/'deployments/fixture').mkdir(parents=True)
    (deployment/'deploy.conf.sh').write_text('fixture deployment\n')
    for name in ('overlay.yaml', 'credentials.toml', 'pin.lock'):
        (deployment/'deployments/fixture'/name).write_text('fixture source\n')
    state = private_root/'state'
    policy = {'version': 1, 'kit_root': str(kit), 'deployment_root': str(deployment),
              'plist': str(config/(label+'.plist')), 'auto_plist': str(auto),
              'state_dir': str(state), 'scratch_dir': str(private_root/'scratch'),
              'candidate_port': 49132, 'models': ['fixture-direct', 'fixture-router'],
              'controller_root': str(kit/'core'), 'controller_label': 'fixture.controller'}
    policy_path = private_root/'policy.json'
    atomic_json(policy_path, policy)
    # The runtime imports litellm from the provenance tree via PYTHONPATH-free
    # site layout: use the tree's own env through the worker's interpreter.
    with mock.patch.dict(os.environ, {'OP_SERVICE_ACCOUNT_TOKEN': 'fixture-only',
                                      'PYTHONPATH': 'fixture-only', 'PYTHONHOME': 'fixture-only'}):
        obj = Runtime(policy_path, test_lifecycle=FixtureLifecycleAdapter(namespace='restart_control'))
    return obj, tree, env


def test_worker_proves_real_tree_end_to_end(proof_runtime):
    runtime, tree, env = proof_runtime
    runtime.env = {**runtime.env, **env}
    # test_lifecycle adapter is already bound by the constructor; the active
    # operation placeholder admits helper spawns through the same seam.
    result = runtime.run_proof_worker()
    proof = json.loads(result)
    assert len(proof['runtime_digest']) == 64
    # The full spawn lifecycle is journalled: closed, never uncertain.
    adapter = runtime.recovery
    assert all(row['state'] == 'closed' for row in adapter.spawns.values())
    assert not adapter.uncertain
    assert adapter.transitions.count('intent') == adapter.transitions.count('closed')
    assert not runtime._cleanup_blocked


def test_worker_failure_leaves_no_uncertain_or_leak(proof_runtime):
    runtime, tree, env = proof_runtime
    # Corrupt the pin so the worker's proof fails business-wise.
    (runtime.config/'pin.lock').write_text('base_version = "9.9.9"\npatch_tip = "'+'0'*40+'"\n')
    runtime.env = {**runtime.env, **env}
    adapter = runtime.recovery
    with pytest.raises(Fault):
        runtime.run_proof_worker()
    # Failure is conservative (uncertain) or closed; nothing leaks journalled.
    assert all(row['state'] in ('closed', 'uncertain') for row in adapter.spawns.values())
    assert not runtime._candidate
    assert runtime._cleanup_blocked or adapter.uncertain


def test_channel_protocol_refusals(proof_runtime):
    """Framing/schema refusals tear the channel down without executing."""
    runtime, tree, env = proof_runtime
    runtime.env = {**runtime.env, **env}
    # Direct protocol probes against the dispatch layer: forged bind.
    import socket as socket_module
    state_dict = {'next_seq': 2, 'nonce': 'x'*32, 'pid': 424242,
                  'pin_sha256': 'f'*64, 'root': None}
    a, b = socket_module.socketpair()
    forged = json.dumps({'version': 1, 'seq': 1, 'op': 'bind', 'nonce': 'wrong',
                         'pid': 424242, 'source_sha256': 'a'*64,
                         'pin_sha256': 'f'*64}).encode()
    with pytest.raises(Fault):
        runtime._dispatch_proof_observation(a, forged+b'\n', state_dict, time.monotonic()+10)
    a.close(); b.close()
    # Unknown op refuses.
    a, b = socket_module.socketpair()
    unknown = json.dumps({'version': 1, 'seq': 2, 'op': 'shell',
                          'argv': ['/bin/sh', '-c', 'id']}).encode()
    with pytest.raises(Fault):
        runtime._dispatch_proof_observation(a, unknown+b'\n', state_dict, time.monotonic()+10)
    a.close(); b.close()
    # Sequence skip refuses.
    a, b = socket_module.socketpair()
    skip = json.dumps({'version': 1, 'seq': 7, 'op': 'git_head'}).encode()
    with pytest.raises(Fault):
        runtime._dispatch_proof_observation(a, skip+b'\n', state_dict, time.monotonic()+10)
    a.close(); b.close()
