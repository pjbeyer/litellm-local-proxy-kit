"""Native receipt deployment closure; real installer/drift, synthetic package gates.

Only package-proof/sentinel observations use the interpreter seam. Metadata,
rendering, staging, file copy, verify and drift execute their real implementations.
No real credentials, proxy/native loading, service writes or launchctl are used.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest
from test_installer_contract import sandbox, run, REPO
from test_native_runtime_identity import native_case


def paired_deployment(dep, case):
    (dep/'pin.lock').write_bytes(case.pin.read_bytes())
    (dep/'native-build.json').write_bytes((case.pin.parent/'native-build.json').read_bytes())


def snapshot(directory):
    facts = {}
    for p in directory.rglob('*'):
        info = p.lstat()
        facts[p.relative_to(directory).as_posix()] = (
            info.st_mode, info.st_dev, info.st_ino, info.st_nlink,
            os.readlink(p) if p.is_symlink() else (p.read_bytes() if p.is_file() else None))
    return facts


def drift(root):
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
    result=subprocess.run([sys.executable,'-B',str(REPO/'core/drift_check.py'),
        '--deployment-root',str(root)],env=env,capture_output=True,text=True,timeout=30)
    return result,json.loads(result.stdout)


def test_legacy_helper_shipping_verify_and_explicit_absence(tmp_path):
    root,dep,config=sandbox(tmp_path)
    assert run(root,'apply').returncode==0
    assert (config/'runtime_identity.py').read_bytes()==(REPO/'core/restart_control/runtime_identity.py').read_bytes()
    assert not (config/'native-build.json').exists()
    result,report=drift(root)
    assert result.returncode==0,report
    assert any(c['kind']=='native-receipt' and c['status']=='conformant' for c in report['checks'])
    (config/'runtime_identity.py').write_text('# changed helper\n')
    assert run(root,'--verify').returncode!=0
    assert drift(root)[0].returncode!=0
    assert run(root,'apply').returncode==0
    assert run(root,'--verify').returncode==0


def test_paired_native_receipt_applied_verified_and_real_flat_proof(tmp_path,native_case):
    root,dep,config=sandbox(tmp_path/'installation')
    paired_deployment(dep,native_case)
    assert run(root,'--dry-run').returncode==0
    assert not config.exists()
    result=run(root,'apply'); assert result.returncode==0,result.stdout+result.stderr
    assert (config/'native-build.json').read_bytes()==(dep/'native-build.json').read_bytes()
    assert run(root,'--verify').returncode==0
    assert drift(root)[0].returncode==0
    # Execute the installed proof module, with only metadata/nm I/O synthetic,
    # against real pinned temporary Git files. Nothing native is imported.
    spec=importlib.util.spec_from_file_location('installed_native_proof',config/'runtime_identity.py')
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for name in ('importlib','sys','platform','subprocess','_tool_output'):
        setattr(module,name,getattr(native_case.module,name))
    assert len(module.prove_runtime(config/'pin.lock')['runtime_digest'])==64
    assert native_case.imports==['litellm']


@pytest.mark.parametrize('state',['missing_receipt','changed_receipt','stale_receipt','receipt_symlink',
    'receipt_directory','pin_duplicate','stale_installed_receipt','missing_installed_receipt'])
def test_mixed_native_states_refuse_without_target_changes(tmp_path,native_case,state):
    root,dep,config=sandbox(tmp_path/'installation')
    paired_deployment(dep,native_case)
    assert run(root,'apply').returncode==0
    if state=='missing_receipt':(dep/'native-build.json').unlink()
    elif state=='changed_receipt':(dep/'native-build.json').write_text('{}')
    elif state=='stale_receipt':(dep/'pin.lock').write_text(native_case.pin.read_text().split('[native_artifact]')[0])
    elif state=='receipt_symlink':
        p=config/'native-build.json';p.rename(config/'receipt-original');p.symlink_to('receipt-original')
    elif state=='receipt_directory':
        p=config/'native-build.json';p.unlink();p.mkdir()
    elif state=='pin_duplicate':
        p=dep/'pin.lock';p.write_text(p.read_text()+'version=1\n')
    elif state=='stale_installed_receipt':
        (config/'pin.lock').write_text(native_case.pin.read_text().split('[native_artifact]')[0])
    elif state=='missing_installed_receipt':(config/'native-build.json').unlink()
    before=snapshot(config)
    for mode in ('apply','--verify'):
        result=run(root,mode)
        assert result.returncode!=0,(state,result.stdout,result.stderr)
        assert snapshot(config)==before
    assert drift(root)[0].returncode!=0


def test_native_downgrade_never_silently_deletes_stale_evidence(tmp_path,native_case):
    root,dep,config=sandbox(tmp_path/'installation');paired_deployment(dep,native_case)
    assert run(root,'apply').returncode==0
    (dep/'native-build.json').unlink()
    (dep/'pin.lock').write_text(native_case.pin.read_text().split('[native_artifact]')[0])
    before=snapshot(config)
    assert run(root,'apply').returncode==78
    assert snapshot(config)==before and (config/'native-build.json').is_file()


def test_native_metadata_and_helper_cannot_be_exempted(tmp_path,native_case):
    root,dep,config=sandbox(tmp_path/'installation');paired_deployment(dep,native_case)
    assert run(root,'apply').returncode==0
    (root/'drift-exemptions.yaml').write_text('exempt: [pin.lock, runtime_identity.py, native-build.json]\n')
    (config/'native-build.json').write_text('{}')
    result,report=drift(root)
    assert result.returncode!=0
    assert any(c['kind']=='native-receipt' and c['status']=='divergent' for c in report['checks'])
    paired_deployment(config,native_case)
    (config/'runtime_identity.py').write_text('# changed\n')
    result,report=drift(root)
    assert result.returncode!=0
    assert any(c['target'].endswith('runtime_identity.py') and c['status']=='divergent' for c in report['checks'])


def test_installer_proof_precedes_sentinel_and_failed_proof_never_reaches_it(tmp_path):
    root,_,config=sandbox(tmp_path)
    trace=tmp_path/'trace';python=tmp_path/'python'
    python.write_text('#!/bin/bash\n'+
        'case "$1" in\n'+
        f'*/check_pin.py) printf "proof\\n" >> {shlex.quote(str(trace))}; exit 1;;\n'+
        f'*/check_patch_sentinel.py) printf "sentinel\\n" >> {shlex.quote(str(trace))}; exit 0;;\n'+
        f'*) exec {shlex.quote(sys.executable)} "$@";;\nesac\n')
    result=run(root,'apply')
    assert result.returncode==78
    assert trace.read_text().splitlines()==['proof']
    assert not config.exists()


@pytest.mark.parametrize('preflight',[False,True])
@pytest.mark.parametrize('proof_status',[0,1])
def test_installed_wrapper_orders_proof_before_import_in_all_modes(tmp_path,preflight,proof_status):
    config=tmp_path/'installed';config.mkdir()
    trace=tmp_path/'trace';launch=tmp_path/'launched'
    for name in ('run-proxy-wrapper.sh','parse_credentials.py','check_pin.py','check_patch_sentinel.py'):
        shutil.copy2(REPO/'core'/name,config/name)
    (config/'pin.lock').write_text('base_version="fixture"\npatch_tip="'+'0'*40+'"\n')
    python=tmp_path/'python'
    python.write_text('#!/bin/bash\ncase "$1" in\n'+
        f'*/check_pin.py) printf "proof\\n" >> {shlex.quote(str(trace))}; exit {proof_status};;\n'+
        f'*/check_patch_sentinel.py) printf "sentinel\\n" >> {shlex.quote(str(trace))}; exit 0;;\n'+
        '*) exit 64;;\nesac\n');python.chmod(0o755)
    binary=tmp_path/'litellm';binary.write_text('#!/bin/sh\ntouch '+shlex.quote(str(launch))+'\n');binary.chmod(0o755)
    op=tmp_path/'op';op.write_text('#!/bin/sh\nprintf "fixture-master-value\\n"\n');op.chmod(0o755)
    manifest=tmp_path/'credentials.toml'
    manifest.write_text('[[credential]]\nenv_var="LOCAL_KEY"\nop_ref="op://fixture/item/field"\nrequired=true\n')
    render=tmp_path/'config.yaml';render.write_text('general_settings:\n  master_key: os.environ/LOCAL_KEY\n')
    env={'HOME':str(tmp_path),'PATH':os.defpath,'TMPDIR':os.environ['TMPDIR'],
         'OP_SERVICE_ACCOUNT_TOKEN':'fixture-service-account','OP_BIN':str(op),
         'LITELLM_DEPLOYMENT':'fixture','CREDENTIALS_MANIFEST':str(manifest),
         'LITELLM_PROXY_CONFIG':str(render),'LITELLM_PROXY_PYTHON':str(python)}
    result=subprocess.run(['/bin/bash',str(config/'run-proxy-wrapper.sh'),*(['--preflight'] if preflight else [])],
        env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==(78 if proof_status else 0),result.stdout+result.stderr
    assert trace.read_text().splitlines()==(['proof'] if proof_status else ['proof','sentinel'])
    assert launch.exists()==(not preflight and proof_status==0)
    assert 'fixture-master-value' not in result.stdout+result.stderr


def test_other_repo_flat_helper_mapping_and_strict_native_identity(tmp_path,native_case):
    root,dep,config=sandbox(tmp_path/'installation');paired_deployment(dep,native_case)
    assert run(root,'apply').returncode==0
    other=tmp_path/'other'
    shutil.copytree(REPO/'core',other/'core',ignore=shutil.ignore_patterns('__pycache__'))
    destination=other/'deployments/other';destination.mkdir(parents=True)
    paired_deployment(destination,native_case)
    command=[sys.executable,'-B',str(REPO/'core/drift_check.py'),
             '--deployment-root',str(root),'--other-root',str(other)]
    result=subprocess.run(command,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    (other/'core/restart_control/runtime_identity.py').write_text('# altered\n')
    result=subprocess.run(command,capture_output=True,text=True,timeout=30)
    report=json.loads(result.stdout)
    assert result.returncode!=0
    assert any(c['kind']=='core-hash' and c['target']=='runtime_identity.py' and c['status']=='divergent'
               for c in report['checks'])
    shutil.copy2(REPO/'core/restart_control/runtime_identity.py',other/'core/restart_control/runtime_identity.py')
    (destination/'native-build.json').unlink()
    (destination/'pin.lock').write_text(native_case.pin.read_text().split('[native_artifact]')[0])
    result=subprocess.run(command,capture_output=True,text=True,timeout=30)
    report=json.loads(result.stdout)
    assert result.returncode!=0
    assert any(c['kind']=='pin' and c['status']=='divergent' for c in report['checks'])
    with (destination/'pin.lock').open('a') as stream:stream.write('base_version="duplicate"\n')
    result=subprocess.run(command,capture_output=True,text=True,timeout=30)
    assert result.returncode==2 and result.stderr=='internal error: drift inputs invalid\n'


def test_metadata_cli_validates_actual_pairing_without_package_import(tmp_path,native_case):
    root,dep,_=sandbox(tmp_path/'installation');paired_deployment(dep,native_case)
    helper=REPO/'core/restart_control/runtime_identity.py'
    command=[sys.executable,'-B',str(helper),'--receipt-state',str(dep/'pin.lock')]
    result=subprocess.run(command,capture_output=True,text=True,timeout=10)
    assert result.returncode==0
    assert json.loads(result.stdout)['native_receipt']['sha256']==native_case.policy['receipt_sha256']
    assert not native_case.imports
    (dep/'native-build.json').write_text('{}')
    result=subprocess.run(command,capture_output=True,text=True,timeout=10)
    assert result.returncode==78 and json.loads(result.stdout)=={'error':'runtime_invalid'}


@pytest.mark.parametrize('kind',['symlink','hardlink','nested'])
def test_installed_helper_layout_and_filetype_defects_preserved(tmp_path,kind):
    root,dep,config=sandbox(tmp_path)
    assert run(root,'apply').returncode==0
    helper=config/'runtime_identity.py'
    if kind=='nested':
        extra=config/'restart_control';extra.mkdir()
        (extra/'runtime_identity.py').write_text('raise AssertionError("unmanaged helper executed")\n')
    else:
        saved=config/'helper-evidence';helper.rename(saved)
        if kind=='symlink':helper.symlink_to(saved.name)
        else:os.link(saved,helper)
    before=snapshot(config)
    for mode in ('apply','--verify'):
        result=run(root,mode)
        assert result.returncode==78,result.stdout+result.stderr
        assert snapshot(config)==before
    assert drift(root)[0].returncode!=0
