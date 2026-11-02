"""Check-pin installed/repository seams: real CLI, no package/native imports."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('layout',['repository','flat'])
def test_direct_cli_suppresses_helper_bytecode_and_diagnostics(tmp_path,layout):
    checker=tmp_path/'check_pin.py'
    shutil.copy2(ROOT/'core/check_pin.py',checker)
    helper=(tmp_path/'restart_control/runtime_identity.py' if layout=='repository'
            else tmp_path/'runtime_identity.py')
    helper.parent.mkdir(exist_ok=True)
    shutil.copy2(ROOT/'core/restart_control/runtime_identity.py',helper)
    pin=tmp_path/'pin.lock'
    pin.write_text('base_version = "fixture"\nbase_version = "repeated"\npatch_tip = "'+'0'*40+'"\n')
    env={k:v for k,v in os.environ.items() if k not in (
        'PYTHONPATH','PYTHONHOME','PYTHONDONTWRITEBYTECODE')}
    result=subprocess.run([sys.executable,'-S',str(checker),str(pin)],
                          capture_output=True,text=True,env=env,timeout=10)
    assert result.returncode==1
    assert result.stdout=='' and result.stderr=='FAIL: pinned runtime provenance invalid\n'
    assert not list(tmp_path.rglob('__pycache__'))
    assert pin.read_text().count('base_version')==2  # refusal never repairs


def test_missing_adjacent_helper_never_uses_pythonpath_shadow(tmp_path):
    checker=tmp_path/'check_pin.py';shutil.copy2(ROOT/'core/check_pin.py',checker)
    shadow=tmp_path/'shadow';shadow.mkdir()
    marker=tmp_path/'untrusted-import'
    (shadow/'runtime_identity.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').write_text("bad")\n')
    env=dict(os.environ,PYTHONPATH=str(shadow))
    result=subprocess.run([sys.executable,'-B',str(checker),'unused'],
                          capture_output=True,text=True,env=env,timeout=10)
    assert result.returncode==1 and not marker.exists()
    assert result.stderr=='FAIL: pinned runtime provenance invalid\n'


def test_repository_layout_missing_helper_does_not_fall_back(tmp_path):
    checker=tmp_path/'check_pin.py';shutil.copy2(ROOT/'core/check_pin.py',checker)
    (tmp_path/'restart_control').mkdir()
    marker=tmp_path/'fallback'
    (tmp_path/'runtime_identity.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').write_text("bad")\n')
    result=subprocess.run([sys.executable,'-B',str(checker),'unused'],capture_output=True,text=True,timeout=10)
    assert result.returncode==1 and not marker.exists()
