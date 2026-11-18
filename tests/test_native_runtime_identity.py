"""Native runtime proof: real Git/files, synthetic metadata/nm, no native loads."""
import ctypes
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent/'core/restart_control/runtime_identity.py').is_file())
REL = 'litellm/rust_bridge/_native.abi3.so'


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def native_case(tmp_path, monkeypatch):
    root = tmp_path/'runtime'
    package = root/'litellm'
    (package/'rust_bridge').mkdir(parents=True)
    (package/'__init__.py').write_text('# fixture package; never imported\n')
    (root/'litellm-rust').mkdir()
    (root/'.cargo').mkdir()
    (root/'litellm-rust/Cargo.toml').write_text('[workspace]\nmembers = []\n')
    (root/'litellm-rust/Cargo.lock').write_text('version = 3\n')
    (root/'.cargo/config.toml').write_text('[net]\nretry = 1\n')
    git_env = {'HOME':str(tmp_path/'home'),'PATH':os.defpath,
               'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,
               'GIT_CONFIG_SYSTEM':os.devnull,'GIT_TERMINAL_PROMPT':'0',
               'GIT_OPTIONAL_LOCKS':'0','TMPDIR':os.environ['TMPDIR']}
    def git(*args):
        return subprocess.check_output(['git','-c','core.hooksPath='+os.devnull,
            '-c','init.templateDir=','-C',str(root),*args],text=True,env=git_env).strip()
    git('init','--object-format=sha1','-q')
    git('add','.')
    git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture source')
    head = git('rev-parse','HEAD')
    tree = git('rev-parse','HEAD:litellm-rust')
    pin_path = tmp_path/'pin.lock'
    binary = root/REL
    # Mach-O header shape only. nm is a labeled fixture; this payload is never loaded.
    platform_command = struct.pack('<IIIIII',0x32,24,1,0x0b0000,0x0f0000,0)
    binary.write_bytes(struct.pack('<IiiIIIII',0xfeedfacf,0x0100000c,0,6,1,24,0,0)+platform_command+b'fixture bytes')
    baseline = {'base_version':'0.1.0','patch_tip':head}
    spec = importlib.util.spec_from_file_location('native_proof_fixture',ROOT/'core/restart_control/runtime_identity.py')
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source_map = {p:{'git_mode':'100644','sha256':sha((root/p).read_bytes())} for p in (
        '.cargo/config.toml','litellm-rust/Cargo.toml','litellm-rust/Cargo.lock')}
    receipt = {'version':1,'source_head':head,'source_tree':tree,
      'source_digest':module.digest(source_map),'source_files':len(source_map),
      'cargo_lock_sha256':source_map['litellm-rust/Cargo.lock']['sha256'],
      'root_cargo_config_sha256':source_map['.cargo/config.toml']['sha256'],
      'baseline_pin_digest':module.digest(baseline),'artifact_path':REL,
      'artifact_sha256':sha(binary.read_bytes()),'recipe':'cargo-locked-offline-dev-abi3-arm64-v1',
      'profile':'dev','features':['extension-module'],'default_features':True,
      'target':'aarch64-apple-darwin','cargo_sha256':'a'*64,'rustc_sha256':'b'*64,
      'cargo_version':'cargo fixture','rustc_version':'rustc fixture',
      'build_recipe_sha256':'c'*64,'build_log_sha256':'d'*64,
      'abi_tag':'cp310-abi3','pyo3_cfg_sha256':'e'*64,
      'pyo3_ffi_cfg_sha256':'f'*64,'pyo3_features_sha256':'0'*64}
    policy = {'version':1,'path':REL,'sha256':sha(binary.read_bytes()),
              'source_tree':tree,'receipt_sha256':'','baseline_pin_digest':module.digest(baseline)}
    imports = []
    class Distribution:
        def read_text(self,name):
            return 'fixture metadata' if name in ('METADATA','RECORD') else None
        def locate_file(self,name):
            return root/name
    def import_fixture(name):
        imports.append(name)
        return SimpleNamespace(__file__=str(package/'__init__.py'))
    module.importlib = SimpleNamespace(
       util=SimpleNamespace(find_spec=lambda name:SimpleNamespace(origin=str(package/'__init__.py')),
                            source_from_cache=importlib.util.source_from_cache,
                            MAGIC_NUMBER=importlib.util.MAGIC_NUMBER),
       metadata=SimpleNamespace(version=lambda name:'0.1.0',distribution=lambda name:Distribution()),
       import_module=import_fixture)
    module.sys = SimpleNamespace(dont_write_bytecode=True,executable=sys.executable,prefix=sys.prefix,
                                 implementation=sys.implementation,platform='darwin',version_info=sys.version_info)
    module.platform = SimpleNamespace(machine=lambda:'arm64')
    real_run = subprocess.run
    nm = {'stdout':b'fixture _PyInit__native\n','returncode':0}
    def fixture_run(argv,**kwargs):
        if argv[0] == '/usr/bin/nm':
            assert argv[1:] == ['-gU', str(binary)]
            return subprocess.CompletedProcess(argv,nm['returncode'],nm['stdout'],b'')
        if argv[0] == '/usr/bin/otool':
            assert argv[1:] == ['-L', str(binary)]
            return subprocess.CompletedProcess(argv,0,b'fixture:\n\t/usr/lib/libSystem.B.dylib\n',b'')
        assert len(argv)>=4 and argv[0]=='git' and argv[1]=='-C'
        assert Path(argv[2]).resolve().is_relative_to(root.resolve())
        assert argv[3] in {'rev-parse','ls-tree'}, 'unexpected fixture subprocess'
        kwargs['env']=git_env
        return real_run(argv,**kwargs)
    module.subprocess = SimpleNamespace(run=fixture_run)
    def fixture_observation(argv, **kwargs):
        capture = argv[0] == 'git' or kwargs.get('env') is not None
        result = fixture_run(argv, capture_output=capture) if capture else fixture_run(argv)
        if result.returncode:
            raise ValueError('native_observation_invalid')
        return result.stdout
    module._tool_output = fixture_observation
    def refuse_native(*args, **kwargs):
        raise AssertionError('native loader must not run in this fixture')
    monkeypatch.setattr(ctypes,'CDLL',refuse_native)
    monkeypatch.setattr(ctypes,'PyDLL',refuse_native)
    monkeypatch.setattr(importlib.machinery.ExtensionFileLoader,'create_module',refuse_native)
    monkeypatch.setattr(importlib.machinery.ExtensionFileLoader,'exec_module',refuse_native)
    def save(rebind_binary=False):
        if rebind_binary:
            policy['sha256'] = receipt['artifact_sha256'] = sha(binary.read_bytes())
        raw = json.dumps(receipt,sort_keys=True).encode()
        (tmp_path/'native-build.json').write_bytes(raw)
        policy['receipt_sha256'] = sha(raw)
        text = f'base_version = "0.1.0"\npatch_tip = "{baseline["patch_tip"]}"\n[native_artifact]\n'
        for key,value in policy.items():
            text += key+' = '+(str(value) if type(value) is int else json.dumps(value))+'\n'
        pin_path.write_text(text)
    save()
    return SimpleNamespace(root=root,package=package,pin=pin_path,binary=binary,policy=policy,
      receipt=receipt,save=save,module=module,imports=imports,nm=nm,git=git)


def test_attested_native_fixture_passes_full_proof(native_case):
    case = native_case
    assert len(case.module.prove_runtime(case.pin)['runtime_digest']) == 64
    assert case.imports == ['litellm']


@pytest.mark.parametrize('variant', ['missing_receipt','receipt_mutation','unknown_pin_field',
    'boolean_version','wrong_path','wrong_source_tree','wrong_baseline','wrong_receipt_head',
    'unknown_receipt_field','wrong_recipe','wrong_features','binary_mutation','binary_missing',
    'binary_symlink','wrong_architecture','wrong_file_type','missing_export','extra_native',
    'source_mutation','untracked_rust_source','duplicate_json','duplicate_toml',
    'extra_macho','wrong_abi'])
def test_native_invalid_states_fail_before_package_import(native_case,variant):
    c = native_case
    if variant == 'missing_receipt': (c.pin.parent/'native-build.json').unlink()
    elif variant == 'receipt_mutation': (c.pin.parent/'native-build.json').write_text('{}')
    elif variant == 'unknown_pin_field': c.policy['extra']='x'; c.save()
    elif variant == 'boolean_version': c.policy['version']=True; c.save()
    elif variant == 'wrong_path': c.policy['path']='litellm/rust_bridge/other.so'; c.save()
    elif variant == 'wrong_source_tree': c.policy['source_tree']='0'*40; c.save()
    elif variant == 'wrong_baseline': c.policy['baseline_pin_digest']='0'*64; c.save()
    elif variant == 'wrong_receipt_head': c.receipt['source_head']='0'*40; c.save()
    elif variant == 'unknown_receipt_field': c.receipt['extra']='x'; c.save()
    elif variant == 'wrong_recipe': c.receipt['recipe']='other'; c.save()
    elif variant == 'wrong_features': c.receipt['features']=[]; c.save()
    elif variant == 'binary_mutation': c.binary.write_bytes(c.binary.read_bytes()+b'changed')
    elif variant == 'binary_missing': c.binary.unlink()
    elif variant == 'binary_symlink':
        target=c.binary.with_suffix('.fixture'); c.binary.rename(target); c.binary.symlink_to(target.name)
    elif variant == 'wrong_architecture':
        data=bytearray(c.binary.read_bytes()); data[4:8]=struct.pack('<I',0x01000007)
        c.binary.write_bytes(data); c.save(rebind_binary=True)
    elif variant == 'wrong_file_type':
        data=bytearray(c.binary.read_bytes()); data[12:16]=struct.pack('<I',2)
        c.binary.write_bytes(data); c.save(rebind_binary=True)
    elif variant == 'missing_export': c.nm['stdout']=b''
    elif variant == 'extra_native': (c.package/'extra.dylib').write_bytes(b'fixture')
    elif variant == 'extra_macho': (c.package/'opaque.bin').write_bytes(c.binary.read_bytes())
    elif variant == 'wrong_abi': c.receipt['abi_tag']='cp311'; c.save()
    elif variant == 'source_mutation': (c.root/'litellm-rust/Cargo.toml').write_text('# changed')
    elif variant == 'untracked_rust_source': (c.root/'litellm-rust/unregistered.rs').write_text('// fixture')
    elif variant == 'duplicate_json':
        raw=json.dumps(c.receipt)[:-1]+',"version":1}'
        (c.pin.parent/'native-build.json').write_text(raw)
        c.policy['receipt_sha256']=sha(raw.encode())
        text=c.pin.read_text(); old=next(l for l in text.splitlines() if l.startswith('receipt_sha256 ='))
        c.pin.write_text(text.replace(old,'receipt_sha256 = '+json.dumps(c.policy['receipt_sha256'])))
    elif variant == 'duplicate_toml': c.pin.write_text(c.pin.read_text()+'version = 1\n')
    with pytest.raises((ValueError,FileNotFoundError)):
        c.module.prove_runtime(c.pin)
    assert not c.imports, 'invalid artifact state reached a package import'


def test_legacy_native_absence_is_explicit(native_case):
    c=native_case
    c.binary.unlink()
    (c.pin.parent/'native-build.json').unlink()
    c.pin.write_text(c.pin.read_text().split('[native_artifact]')[0])
    assert len(c.module.prove_runtime(c.pin)['runtime_digest']) == 64


def test_unpaired_receipt_refuses_legacy_mode(native_case):
    c=native_case
    c.binary.unlink()
    c.pin.write_text(c.pin.read_text().split('[native_artifact]')[0])
    with pytest.raises(ValueError): c.module.prove_runtime(c.pin)
    assert not c.imports


@pytest.mark.parametrize('field', sorted({
    'version','source_head','source_tree','source_digest','source_files',
    'cargo_lock_sha256','root_cargo_config_sha256','baseline_pin_digest',
    'artifact_path','artifact_sha256','recipe','profile','features','default_features',
    'target','cargo_sha256','rustc_sha256','cargo_version','rustc_version',
    'build_recipe_sha256','build_log_sha256','abi_tag','pyo3_cfg_sha256',
    'pyo3_ffi_cfg_sha256','pyo3_features_sha256'}))
def test_every_receipt_field_required(native_case, field):
    c=native_case
    del c.receipt[field]
    c.save()
    with pytest.raises(ValueError): c.module.prove_runtime(c.pin)
    assert not c.imports


@pytest.mark.parametrize('field', ['version','path','sha256','source_tree','receipt_sha256','baseline_pin_digest'])
def test_every_native_pin_field_required(native_case, field):
    c=native_case
    del c.policy[field]
    # Save without automatically repairing the intentionally missing digest.
    text=c.pin.read_text()
    c.pin.write_text('\n'.join(l for l in text.splitlines() if not l.startswith(field+' ='))+'\n')
    with pytest.raises(ValueError): c.module.prove_runtime(c.pin)
    assert not c.imports


@pytest.mark.parametrize('field,value', [
    ('source_files', True),('version', True),('source_files',1.0),
    ('default_features',1),('cargo_sha256','not-a-hash'),('cargo_version','cargo\ninjected'),
    ('rustc_version','cargo fixture'),('pyo3_cfg_sha256',None),('source_digest','f'*64),
    ('cargo_lock_sha256','f'*64),('root_cargo_config_sha256','f'*64),
])
def test_receipt_types_and_source_claims_refuse(native_case,field,value):
    c=native_case
    c.receipt[field]=value; c.save()
    with pytest.raises(ValueError): c.module.prove_runtime(c.pin)
    assert not c.imports


def macho_command(c,command,payload=b''):
    length=(8+len(payload)+7)//8*8
    body=struct.pack('<II',command,length)+payload.ljust(length-8,b'\0')
    platform_command=struct.pack('<IIIIII',0x32,24,1,0x0b0000,0x0f0000,0)
    c.binary.write_bytes(struct.pack('<IiiIIIII',0xfeedfacf,0x0100000c,0,6,2,len(body)+24,0,0)+body+platform_command)
    c.save(rebind_binary=True)


@pytest.mark.parametrize('name', ['/usr/lib/libSystem.B.dylib','/usr/lib/libiconv.2.dylib',
    '/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation',
    '/System/Library/Frameworks/Security.framework/Versions/A/Security'])
def test_system_dependency_is_allowed(native_case,name):
    c=native_case
    macho_command(c,0xc,struct.pack('<IIII',24,0,0,0)+name.encode()+b'\0')
    c.module.prove_runtime(c.pin)
    assert c.imports==['litellm']


@pytest.mark.parametrize('command,name', [(0xc,'/mutable/lib.dylib'),(0xc,'@rpath/lib.dylib'),
    (0xc,'/usr/lib/libpython3.11.dylib'),(0x80000018,'/mutable/lib.dylib'),
    (0x8000001f,'/mutable/lib.dylib'),(0x80000023,'/mutable/lib.dylib'),
    (0x20,'/mutable/lib.dylib'),(0x8000001c,'@loader_path'),(0x27,'DYLD_INSERT_LIBRARIES=x'),
    (0xe,'/mutable/dyld'),(0x999,'unknown')])
def test_dependency_and_loader_injection_refuse(native_case,command,name):
    c=native_case
    macho_command(c,command,struct.pack('<IIII',24,0,0,0)+name.encode()+b'\0')
    with pytest.raises(ValueError): c.module.prove_runtime(c.pin)
    assert not c.imports


@pytest.mark.parametrize('variant',['symlink_source_root','extra_nested_input','execute_mode',
    'receipt_hardlink','binary_hardlink','receipt_writable','binary_writable','native_missing_receipt_table',
    'extra_unknown_suffix','truncated_commands','bad_command_size','bad_dependency_offset'])
def test_source_artifact_metadata_and_header_failures(native_case,variant):
    c=native_case
    if variant=='symlink_source_root':
        original=c.root/'.cargo'; target=c.root/'config-target'; original.rename(target); original.symlink_to(target.name)
    elif variant=='extra_nested_input':
        d=c.root/'litellm-rust/nested';d.mkdir();(d/'hidden.data').write_text('new input')
    elif variant=='execute_mode':(c.root/'litellm-rust/Cargo.lock').chmod(0o755)
    elif variant=='receipt_hardlink':os.link(c.pin.parent/'native-build.json',c.pin.parent/'receipt-copy')
    elif variant=='binary_hardlink':os.link(c.binary,c.pin.parent/'binary-copy')
    elif variant=='receipt_writable':(c.pin.parent/'native-build.json').chmod(0o666)
    elif variant=='binary_writable':c.binary.chmod(0o777)
    elif variant=='native_missing_receipt_table':
        (c.pin.parent/'native-build.json').unlink();c.pin.write_text(c.pin.read_text().split('[native_artifact]')[0])
    elif variant=='extra_unknown_suffix':(c.package/'extra').write_bytes(c.binary.read_bytes())
    elif variant=='truncated_commands':
        data=bytearray(c.binary.read_bytes());data[16:24]=struct.pack('<II',1,1000)
        c.binary.write_bytes(data);c.save(rebind_binary=True)
    elif variant=='bad_command_size':
        macho_command(c,0xc,b'x'*24);data=bytearray(c.binary.read_bytes());data[36:40]=struct.pack('<I',7)
        c.binary.write_bytes(data);c.save(rebind_binary=True)
    elif variant=='bad_dependency_offset':macho_command(c,0xc,struct.pack('<IIII',0,0,0,0)+b'/usr/lib/libSystem.B.dylib\0')
    with pytest.raises(ValueError):c.module.prove_runtime(c.pin)
    assert not c.imports


def test_metadata_pairing_is_shared_and_never_imports(native_case):
    c=native_case
    assert c.module.native_receipt_fact(c.pin)['sha256']==c.policy['receipt_sha256']
    assert not c.imports
    (c.pin.parent/'native-build.json').write_text('{}')
    with pytest.raises(ValueError):c.module.native_receipt_fact(c.pin)
    assert not c.imports


def test_duplicate_legacy_toml_refuses_without_native(native_case):
    c=native_case
    c.binary.unlink();(c.pin.parent/'native-build.json').unlink()
    base=c.pin.read_text().split('[native_artifact]')[0]
    c.pin.write_text(base+'base_version = "0.1.0"\n')
    with pytest.raises(ValueError):c.module.prove_runtime(c.pin)
    assert not c.imports


@pytest.mark.parametrize('variant',['arm64e','reserved','ios','missing_platform','duplicate_platform','bad_platform_length'])
def test_platform_and_subtype_must_match(native_case,variant):
    c=native_case
    data=bytearray(c.binary.read_bytes())
    if variant=='arm64e':data[8:12]=struct.pack('<i',2)
    elif variant=='reserved':data[28:32]=struct.pack('<I',1)
    elif variant=='ios':data[40:44]=struct.pack('<I',2)
    elif variant=='missing_platform':data=bytearray(struct.pack('<IiiIIIII',0xfeedfacf,0x0100000c,0,6,0,0,0,0))
    elif variant=='duplicate_platform':
        data=bytearray(struct.pack('<IiiIIIII',0xfeedfacf,0x0100000c,0,6,2,48,0,0)+bytes(data[32:56])*2)
    elif variant=='bad_platform_length':data[52:56]=struct.pack('<I',1)
    c.binary.write_bytes(data);c.save(rebind_binary=True)
    with pytest.raises(ValueError):c.module.prove_runtime(c.pin)
    assert not c.imports


def test_bounded_observer_real_child_controls():
    spec=importlib.util.spec_from_file_location('observer_control',ROOT/'core/restart_control/runtime_identity.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    assert m._tool_output([sys.executable,'-B','-c','print("OK")'],timeout=5,limit=100)==b'OK\n'
    with pytest.raises(ValueError,match='native_observation_bound'):
        m._tool_output([sys.executable,'-B','-c','import os; os.write(1,b"x"*10000)'],timeout=5,limit=100)
    with pytest.raises(ValueError,match='native_observation_timeout'):
        m._tool_output([sys.executable,'-B','-c','import time; time.sleep(5)'],timeout=0.05,limit=100)
