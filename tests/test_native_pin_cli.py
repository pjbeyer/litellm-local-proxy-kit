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
    result=subprocess.run([sys.executable,'-S',str(checker),*(['--repository'] if layout=='repository' else []),str(pin)],
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
    result=subprocess.run([sys.executable,'-B',str(checker),'--repository','unused'],capture_output=True,text=True,timeout=10)
    assert result.returncode==1 and not marker.exists()


@pytest.mark.parametrize('flat_present',[False,True])
def test_installed_cli_never_executes_unmanaged_nested_helper(tmp_path,flat_present):
    checker=tmp_path/'check_pin.py';shutil.copy2(ROOT/'core/check_pin.py',checker)
    nested=tmp_path/'restart_control';nested.mkdir()
    marker=tmp_path/'executed'
    (nested/'runtime_identity.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').write_text("bad")\n')
    if flat_present:shutil.copy2(ROOT/'core/restart_control/runtime_identity.py',tmp_path/'runtime_identity.py')
    result=subprocess.run([sys.executable,'-B',str(checker),'unused'],capture_output=True,text=True,timeout=10)
    assert result.returncode==1 and not marker.exists()


@pytest.mark.parametrize('layout',['flat','repository'])
def test_valid_timestamp_cache_cannot_replace_verified_helper_source(tmp_path,layout):
    import importlib.util
    import marshal
    import struct
    checker=tmp_path/'check_pin.py';shutil.copy2(ROOT/'core/check_pin.py',checker)
    helper=(tmp_path/'runtime_identity.py' if layout=='flat' else tmp_path/'restart_control/runtime_identity.py')
    helper.parent.mkdir(exist_ok=True)
    helper.write_text('def prove_runtime(path):\n    raise ValueError("fixture refusal")\n')
    marker=tmp_path/'cache-executed'
    bad='from pathlib import Path\nPath('+repr(str(marker))+').write_text("bad")\ndef prove_runtime(path): pass\n'
    info=helper.stat()
    cache=Path(importlib.util.cache_from_source(str(helper)))
    cache.parent.mkdir(exist_ok=True)
    cache.write_bytes(importlib.util.MAGIC_NUMBER+struct.pack('<III',0,int(info.st_mtime),info.st_size)+
                      marshal.dumps(compile(bad,str(helper),'exec')))
    before=cache.read_bytes()
    result=subprocess.run([sys.executable,'-B',str(checker),*(['--repository'] if layout=='repository' else []),'unused'],
                          capture_output=True,text=True,timeout=10)
    assert result.returncode==1 and not marker.exists()
    assert cache.read_bytes()==before
