"""Optional native receipt closure for controller freeze/LKG — fixtures only.

Covers the native runtime artifact contract at the controller layer: explicit
presence/absence and frozen bytes for the optional ``native-build.json``
receipt on both the installed and deployment pins, pin pairing through the
identity module's shared metadata-only routine (documented API:
``native_receipt_fact(pin_path) -> file fact dict or None``), LKG
copy-only-when-present, receipt mutation/presence-drift refusal, and
fail-closed restore that never silently removes stale evidence during the
legacy transition.

Every runtime here is a disposable fixture runtime whose helper capture is a
mock: no pinned-interpreter subprocess, installer, drift check, preflight or
cold-start helper actually runs in the negative cases, and the shared
pairing routine is a metadata-only stand-in. No native extension is imported
or executed, no credentials/proxy/process-control/launchctl are touched, and
no service state is written.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'core'))
from restart_control import runtime as runtime_module
from restart_control.engine import Controller, Fault, atomic_json
from restart_control.runtime import Runtime
from restart_control.shutdown import Shutdown
from restart_control.runtime_identity import KIT_INPUTS, PROXY_INPUTS, file_fact

RECEIPT = 'native-build.json'
LEGACY_PIN = 'base_version = "1.2.3"\npatch_tip = "' + '0'*40 + '"\n'
RECEIPT_BYTES = b'{"version":1,"fixture":"synthetic native receipt evidence"}\n'


def native_pin(receipt_sha):
    return ('base_version = "1.2.3"\npatch_tip = "' + '0'*40 + '"\n[native_artifact]\n'
            'version = 1\npath = "litellm/rust_bridge/_native.abi3.so"\n'
            'sha256 = "' + '1'*64 + '"\nsource_tree = "' + '2'*40 + '"\n'
            'receipt_sha256 = "' + receipt_sha + '"\n'
            'baseline_pin_digest = "' + '3'*64 + '"\n')


def pairing_stub(record=None):
    """Metadata-only stand-in for the identity module's shared routine.

    Documents the planned API surface only: reads pin/receipt bytes, applies
    strict presence/pairing checks and returns a file fact or None. It never
    imports the package, never executes native code and never repairs files.
    """
    def native_receipt_fact(pin_path):
        if record is not None:
            record.append(Path(pin_path))
        pin = Path(pin_path)
        text = pin.read_text()
        if '[native_artifact]' not in text:
            return None
        receipt = pin.parent/RECEIPT
        if not receipt.exists() or receipt.is_symlink():
            return None
        match = re.search(r'receipt_sha256\s*=\s*"([0-9a-f]{64})"', text)
        if match is None or hashlib.sha256(receipt.read_bytes()).hexdigest() != match.group(1):
            return None
        return file_fact(receipt)
    return native_receipt_fact


@pytest.fixture
def private_root():
    with tempfile.TemporaryDirectory(prefix='ncc-', dir=os.environ['TMPDIR']) as name:
        yield Path(name)


@pytest.fixture
def native_runtime(private_root):
    kit, config = private_root/'kit', private_root/'config'
    config.mkdir()
    for name in KIT_INPUTS:
        target = kit/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/name, target)
    label = 'fixture.local.proxy'
    for name in (*PROXY_INPUTS, label+'.plist', label+'.rotate.plist'):
        (config/name).write_text('fixture-owned input\n')
    (config/'pin.lock').write_text(LEGACY_PIN)
    manifest = ('[[credential]]\nenv_var = "FIXTURE_HTTP_KEY"\n'
                'op_ref = "op://fixture/item/field"\nrequired = true\n')
    (config/'credentials.toml').write_text(manifest)
    job = {'Label': label, 'WorkingDirectory': str(config),
           'ProgramArguments': ['/bin/bash', str(config/'run-proxy-wrapper.sh'), '49131'],
           'EnvironmentVariables': {'LITELLM_PROXY_LABEL': label,
              'LITELLM_PROXY_CONFIG': str(config/'config.rendered.yaml'),
              'CREDENTIALS_MANIFEST': str(config/'credentials.toml'),
              'LITELLM_PROXY_PYTHON': sys.executable, 'LITELLM_DEPLOYMENT': 'fixture'},
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
        (deployment/'deployments/fixture'/name).write_text(LEGACY_PIN)
    state = private_root/'state'
    policy = {'version': 1, 'kit_root': str(kit), 'deployment_root': str(deployment),
              'plist': str(config/(label+'.plist')), 'auto_plist': str(auto),
              'state_dir': str(state), 'scratch_dir': str(private_root/'scratch'),
              'candidate_port': 49132, 'models': ['fixture-direct', 'fixture-router'],
              'controller_root': str(kit/'core'), 'controller_label': 'fixture.controller'}
    policy_path = private_root/'policy.json'
    atomic_json(policy_path, policy)
    with mock.patch.dict(os.environ, {'OP_SERVICE_ACCOUNT_TOKEN': 'fixture-only',
                                      'PYTHONPATH': 'fixture-only', 'PYTHONHOME': 'fixture-only'}):
        obj = Runtime(policy_path, shutdown=Shutdown())
    obj.run_proof_worker = mock.Mock(return_value=json.dumps({'runtime_digest': 'a'*64}).encode())
    return obj


def install_receipt(runtime, receipt_bytes=RECEIPT_BYTES, installed=True, deployment=True):
    """Place a paired pin+receipt on the requested sides and return the bytes."""
    sha = hashlib.sha256(receipt_bytes).hexdigest()
    if installed:
        (runtime.config/RECEIPT).write_bytes(receipt_bytes)
        (runtime.config/'pin.lock').write_text(native_pin(sha))
    if deployment:
        root = runtime.deployment/'deployments'/runtime.name
        (root/RECEIPT).write_bytes(receipt_bytes)
        (root/'pin.lock').write_text(native_pin(sha))
    return receipt_bytes


def lkg_dir(runtime):
    path = runtime.state/'fixture-lkg'
    path.mkdir(mode=0o700)
    return path


# -- explicit absence: the default legacy state -------------------------------

def test_valid_absence_freezes_explicitly_snapshots_and_restores_absence(native_runtime):
    runtime = native_runtime
    with mock.patch.object(runtime_module, 'native_receipt_fact', pairing_stub()):
        frozen = runtime.freeze()
    assert 'native-build.json' in runtime_module.PROXY_OPTIONAL_INPUTS
    assert runtime._last_proxy['optional/'+RECEIPT] is None
    path = lkg_dir(runtime)
    binding = runtime.snapshot_lkg(path)
    manifest = json.loads((path/'manifest.json').read_text())
    assert manifest[RECEIPT] is None and not (path/RECEIPT).exists()
    runtime.validate_lkg(path, binding)
    runtime.restore_lkg(path, binding)
    assert not (runtime.config/RECEIPT).exists()
    assert set(frozen) == {'asset_digest', 'runtime_digest', 'policy_digest', 'controller_digest'}


# -- paired presence: bytes frozen on both sides, LKG copies when present ----

def test_paired_receipt_frozen_into_installed_and_deployment_digests(native_runtime):
    runtime = native_runtime
    stub = pairing_stub()
    with mock.patch.object(runtime_module, 'native_receipt_fact', stub):
        absent = runtime.freeze()
        install_receipt(runtime)
        present = runtime.freeze()
        # The standalone proof runs inside the isolated worker now: the
        # controller never imports LiteLLM for proof, and every observation
        # executes in the parent through the journal-attached action gate.
        assert runtime.run_proof_worker.call_count == 2
        runtime.run_proof_worker.assert_called_with()
        assert present['asset_digest'] != absent['asset_digest']
        other = install_receipt(runtime, receipt_bytes=RECEIPT_BYTES+b"{\"other\":true}\n")
        rotated = runtime.freeze()
        assert rotated['asset_digest'] != present['asset_digest']
        # Deployment-side receipt bytes are frozen too: rotate the deployment
        # receipt together with its own pin pairing and the digest changes.
        install_receipt(runtime)
        (runtime.deployment/'deployments'/runtime.name/RECEIPT).write_bytes(other)
        (runtime.deployment/'deployments'/runtime.name/'pin.lock').write_text(
            native_pin(hashlib.sha256(other).hexdigest()))
        assert runtime.freeze()['asset_digest'] != rotated['asset_digest']
        # A valid snapshot/restore round-trip preserves exact receipt bytes.
        path = lkg_dir(runtime)
        install_receipt(runtime)
        proof = runtime.freeze()
        binding = runtime.snapshot_lkg(path)
        assert (path/RECEIPT).read_bytes() == RECEIPT_BYTES
        assert binding == {'asset_digest': proof['asset_digest'],
                           'runtime_digest': proof['runtime_digest']}
        runtime.validate_lkg(path, binding)
        runtime.restore_lkg(path, binding)
        assert (runtime.config/RECEIPT).read_bytes() == RECEIPT_BYTES


# -- wrong / missing / unpaired receipts block freeze before any helper ------

@pytest.mark.parametrize('state', ['wrong_sha', 'missing_receipt', 'unpaired_receipt',
                                   'symlink_receipt', 'routine_raises'])
def test_receipt_defects_block_freeze_before_proof_and_gates(native_runtime, state):
    runtime = native_runtime
    if state == 'wrong_sha':
        install_receipt(runtime)
        (runtime.config/RECEIPT).write_bytes(b'{"version":1,"fixture":"mutated"}\n')
    elif state == 'missing_receipt':
        (runtime.config/'pin.lock').write_text(native_pin('4'*64))
    elif state == 'unpaired_receipt':
        (runtime.config/RECEIPT).write_bytes(RECEIPT_BYTES)  # legacy pin, stale receipt
    elif state == 'symlink_receipt':
        install_receipt(runtime)
        target = runtime.config/RECEIPT
        target.rename(target.with_suffix('.real'))
        target.symlink_to(target.with_suffix('.real').name)

    def raising(pin_path):
        raise ValueError('strict pairing refused')
    stub = pairing_stub() if state != 'routine_raises' else raising
    if state == 'routine_raises':
        install_receipt(runtime)
    before = (runtime.config/RECEIPT).read_bytes() if (runtime.config/RECEIPT).exists() else None
    with mock.patch.object(runtime_module, 'native_receipt_fact', stub):
        with pytest.raises(Fault, match='runtime_invalid'):
            runtime.freeze()
    # No proof subprocess, installer, drift check or preflight ever ran…
    runtime.run_proof_worker.assert_not_called()
    # …and no stale evidence was deleted or repaired.
    if before is not None:
        assert (runtime.config/RECEIPT).read_bytes() == before
    else:
        assert not (runtime.config/RECEIPT).exists()


def test_deployment_receipt_defects_block_freeze_before_helpers(native_runtime):
    runtime = native_runtime
    root = runtime.deployment/'deployments'/runtime.name
    (runtime.config/RECEIPT).write_bytes(RECEIPT_BYTES)
    (runtime.config/'pin.lock').write_text(native_pin(hashlib.sha256(RECEIPT_BYTES).hexdigest()))
    # Stale unpaired receipt on the deployment side (legacy deployment pin).
    (root/RECEIPT).write_bytes(RECEIPT_BYTES)
    (root/'pin.lock').write_text(LEGACY_PIN)
    with mock.patch.object(runtime_module, 'native_receipt_fact', pairing_stub()):
        with pytest.raises(Fault, match='runtime_invalid'):
            runtime.freeze()
    runtime.run_proof_worker.assert_not_called()
    assert (root/RECEIPT).read_bytes() == RECEIPT_BYTES  # preserved, not removed

    # Native-expecting deployment pin without its receipt also fails closed.
    (root/RECEIPT).unlink()
    (root/'pin.lock').write_text(native_pin('5'*64))
    with mock.patch.object(runtime_module, 'native_receipt_fact', pairing_stub()):
        with pytest.raises(Fault, match='runtime_invalid'):
            runtime.freeze()
    runtime.run_proof_worker.assert_not_called()

    # A fully paired deployment receipt freezes cleanly.
    (root/RECEIPT).write_bytes(RECEIPT_BYTES)
    (root/'pin.lock').write_text(native_pin(hashlib.sha256(RECEIPT_BYTES).hexdigest()))
    with mock.patch.object(runtime_module, 'native_receipt_fact', pairing_stub()):
        runtime.freeze()
    runtime.run_proof_worker.assert_called_once()


def test_shared_pairing_failure_is_closed(native_runtime):
    with mock.patch.object(runtime_module, 'native_receipt_fact', side_effect=ValueError('unavailable')):
        with pytest.raises(Fault, match='runtime_invalid'):
            native_runtime.freeze()
    native_runtime.run_proof_worker.assert_not_called()


# -- LKG receipt mutation and presence drift ----------------------------------

def test_lkg_presence_drift_refused_in_both_directions(native_runtime):
    runtime = native_runtime
    stub = pairing_stub()
    with mock.patch.object(runtime_module, 'native_receipt_fact', stub):
        # Frozen absence, then a stale receipt appears live: refuse, and
        # never copy or record it into the snapshot.
        runtime.freeze()
        (runtime.config/RECEIPT).write_bytes(RECEIPT_BYTES)
        path = lkg_dir(runtime)
        with pytest.raises(Fault, match='lkg_invalid'):
            runtime.snapshot_lkg(path)
        assert not (path/RECEIPT).exists()
        assert not (path/'manifest.json').exists()
        assert (runtime.config/RECEIPT).read_bytes() == RECEIPT_BYTES  # stale evidence kept

        # Frozen presence, then the live receipt is removed: refuse.
        install_receipt(runtime)
        runtime.freeze()
        (runtime.config/RECEIPT).unlink()
        with pytest.raises(Fault, match='lkg_invalid'):
            runtime.snapshot_lkg(path)


def test_lkg_receipt_mutation_refused_at_snapshot_and_validation(native_runtime):
    runtime = native_runtime
    stub = pairing_stub()
    with mock.patch.object(runtime_module, 'native_receipt_fact', stub):
        install_receipt(runtime)
        runtime.freeze()
        path = lkg_dir(runtime)
        # Mutated live receipt between freeze and snapshot: refuse.
        (runtime.config/RECEIPT).write_bytes(b'{"version":1,"fixture":"mutated"}\n')
        with pytest.raises(Fault, match='lkg_invalid'):
            runtime.snapshot_lkg(path)

        # Mutated snapshot copy: validation refuses with or without a
        # manifest patched to the mutated fact (the frozen generation wins).
        install_receipt(runtime)
        runtime.freeze()
        binding = runtime.snapshot_lkg(path)
        (path/RECEIPT).write_bytes(b'{"version":1,"fixture":"mutated"}\n')
        with pytest.raises(Fault, match='lkg_invalid'):
            runtime.validate_lkg(path, binding)
        manifest = json.loads((path/'manifest.json').read_text())
        manifest[RECEIPT] = file_fact(path/RECEIPT)
        atomic_json(path/'manifest.json', manifest)
        with pytest.raises(Fault, match='lkg_invalid'):
            runtime.validate_lkg(path, binding)

        # A receipt file sitting in the snapshot dir while the manifest
        # records absence is refused (absence must be exact on disk).
        (path/RECEIPT).unlink()
        manifest[RECEIPT] = None
        atomic_json(path/'manifest.json', manifest)
        (path/RECEIPT).write_bytes(RECEIPT_BYTES)
        with pytest.raises(Fault, match='lkg_invalid'):
            runtime.validate_lkg(path, binding)


def test_restore_refuses_drift_and_preserves_evidence(native_runtime):
    runtime = native_runtime
    stub = pairing_stub()
    with mock.patch.object(runtime_module, 'native_receipt_fact', stub):
        install_receipt(runtime)
        runtime.freeze()
        path = lkg_dir(runtime)
        binding = runtime.snapshot_lkg(path)

        # Live receipt mutated after snapshot: restore refuses up front (the
        # freeze inside restore fails closed) and preserves the mutated bytes.
        mutated = b'{"version":1,"fixture":"mutated-live"}\n'
        (runtime.config/RECEIPT).write_bytes(mutated)
        with pytest.raises(Fault, match='runtime_invalid'):
            runtime.restore_lkg(path, binding)
        assert (runtime.config/RECEIPT).read_bytes() == mutated
        assert (path/RECEIPT).read_bytes() == RECEIPT_BYTES

        # Live receipt removed after snapshot: restore refuses (missing
        # receipt for a native-expecting pin) and writes nothing.
        install_receipt(runtime)
        runtime.freeze()
        binding = runtime.snapshot_lkg(path)
        (runtime.config/RECEIPT).unlink()
        with pytest.raises(Fault, match='runtime_invalid'):
            runtime.restore_lkg(path, binding)
        assert not (runtime.config/RECEIPT).exists()
        assert (path/RECEIPT).read_bytes() == RECEIPT_BYTES


# -- engine-level: prepare blocks on a bad receipt before gates/preflight ----

def test_prepare_blocks_on_unpaired_receipt_before_gates(native_runtime):
    runtime = native_runtime
    (runtime.config/RECEIPT).write_bytes(RECEIPT_BYTES)  # legacy pin: unpaired
    identity = {'pid': 4242, 'uid': os.getuid(), 'birth': [1, 1]}
    runtime.fixture = True  # plan records must label this disposable world
    with mock.patch.object(runtime_module, 'native_receipt_fact', pairing_stub()), \
         mock.patch.object(runtime, 'inspect', return_value={'identity': identity, 'managed': True}):
        # The controller must share the runtime's already-bound latch: a
        # first-bound Runtime rejects any other valid latch by design.
        controller = Controller(runtime.state, runtime, shutdown=runtime.shutdown)
        result = controller.handle({'op': 'prepare'})
    assert result['status'] == 'blocked' and result['code'] == 'runtime_invalid'
    # No gate, preflight or cold-start helper subprocess was ever launched.
    runtime.run_proof_worker.assert_not_called()
    assert (runtime.config/RECEIPT).read_bytes() == RECEIPT_BYTES


# Actual metadata validator, in addition to the labeled controller seam above.
from test_native_runtime_identity import native_case


def test_real_shared_metadata_pairing_and_drift(native_runtime, native_case):
    runtime, case = native_runtime, native_case
    absent = runtime.freeze()
    for target in (runtime.config, runtime.deployment/'deployments'/runtime.name):
        (target/'pin.lock').write_bytes(case.pin.read_bytes())
        (target/RECEIPT).write_bytes((case.pin.parent/RECEIPT).read_bytes())
    paired = runtime.freeze()
    assert paired['asset_digest'] != absent['asset_digest']
    path = lkg_dir(runtime)
    binding = runtime.snapshot_lkg(path)
    runtime.validate_lkg(path, binding)
    runtime.restore_lkg(path, binding)
    runtime.run_proof_worker.reset_mock()
    changed = (runtime.config/RECEIPT).read_bytes()+b' '
    (runtime.config/RECEIPT).write_bytes(changed)
    with pytest.raises(Fault, match='runtime_invalid'):
        runtime.freeze()
    runtime.run_proof_worker.assert_not_called()
    assert (runtime.config/RECEIPT).read_bytes() == changed
