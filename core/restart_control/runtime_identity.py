"""Closed input registry and pinned-runtime content proof.

The script entrypoint runs under the PINNED interpreter, reports only hashes,
never imports controller credentials and never changes runtime files/caches.
"""
from __future__ import annotations
import hashlib
import importlib.metadata
import importlib.util
import json
import marshal
import os
import platform
import struct
import selectors
import time
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # provisioned strict parser on Python 3.10
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import unquote, urlparse

KIT_INPUTS = (
    'core/installer.sh', 'core/drift_check.py', 'core/render_config.py',
    'core/install_launchagent.py', 'core/parse_credentials.py',
    'core/check_credential_coverage.py', 'core/check_patch_sentinel.py',
    'core/check_pin.py', 'core/run-proxy-wrapper.sh', 'core/aws-sso-cred-shim.sh',
    'core/rotate-logs.sh', 'core/harvest-filter-metrics.sh',
    'core/config.template.header.txt', 'launchd/proxy.plist.tmpl',
    'launchd/proxy.rotate.plist.tmpl', 'core/restart_controller.py',
    'core/install_restart_controller.py', 'core/restart_control/__init__.py',
    'core/restart_control/engine.py', 'core/restart_control/wire.py',
    'core/restart_control/runtime.py', 'core/restart_control/runtime_identity.py',
    'core/restart_control/process_identity.py',
)
CONTROLLER_INPUTS = tuple(p for p in KIT_INPUTS if '/restart_control/' in p or
                          p in ('core/restart_controller.py', 'core/install_restart_controller.py'))
PROXY_INPUTS = (
    'config.rendered.yaml', 'run-proxy-wrapper.sh', 'parse_credentials.py',
    'credentials.toml', 'pin.lock', 'check_patch_sentinel.py', 'check_pin.py',
    'check_credential_coverage.py', 'aws-sso-cred-shim.sh', 'rotate-logs.sh',
    'harvest-filter-metrics.sh', 'runtime_identity.py',
)
PROXY_OPTIONAL_INPUTS = ('native-build.json',)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def file_fact(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('non_regular_input')
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            hasher.update(chunk)
    after = path.lstat()
    if (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) != (
            after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
        raise ValueError('input_changed')
    return {'sha256': hasher.hexdigest(), 'mode': stat.S_IMODE(info.st_mode)}


def source_facts(root):
    root = Path(root)
    facts = {name: file_fact(root / name) for name in KIT_INPUTS}
    actual = set()
    for path in (root/'core').rglob('*'):
        if '__pycache__' in path.parts:
            continue
        if path.is_file() and (path.suffix in ('.py', '.sh', '.txt') or
                               path.stat().st_mode & 0o111):
            actual.add(path.relative_to(root).as_posix())
    if actual != {name for name in KIT_INPUTS if name.startswith('core/')}:
        raise ValueError('source_registry_mismatch')
    return facts


def _git(root, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    result = subprocess.run(['git', '-C', str(root), *args], env=env,
                            capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError('runtime_git_unavailable')
    return result.stdout


NATIVE_PATH = 'litellm/rust_bridge/_native.abi3.so'
NATIVE_FIELDS = {'version', 'path', 'sha256', 'source_tree', 'receipt_sha256', 'baseline_pin_digest'}
RECEIPT_FIELDS = {'version', 'source_head', 'source_tree', 'source_digest', 'source_files',
    'cargo_lock_sha256', 'root_cargo_config_sha256', 'baseline_pin_digest', 'artifact_path',
    'artifact_sha256', 'recipe', 'profile', 'features', 'default_features', 'target',
    'cargo_sha256', 'rustc_sha256', 'cargo_version', 'rustc_version', 'build_recipe_sha256',
    'build_log_sha256', 'abi_tag', 'pyo3_cfg_sha256', 'pyo3_ffi_cfg_sha256', 'pyo3_features_sha256'}
SYSTEM_LIBRARIES = {'/usr/lib/libSystem.B.dylib', '/usr/lib/libiconv.2.dylib',
    '/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation',
    '/System/Library/Frameworks/Security.framework/Versions/A/Security'}
MACH_MAGICS = {bytes.fromhex(s) for s in ('feedface', 'cefaedfe', 'feedfacf', 'cffaedfe',
                                       'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}


def read_pin(path):
    """One strict TOML interpretation shared by standalone and installed gates."""
    path = Path(path)
    if path.lstat().st_size > 65536:
        raise ValueError('pin_invalid')
    fact = file_fact(path)
    raw = path.read_bytes()
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != fact['sha256']:
        raise ValueError('pin_invalid')
    pin = tomllib.loads(raw.decode('utf-8'))
    if (not isinstance(pin, dict) or not isinstance(pin.get('base_version'), str)
            or not pin['base_version'] or not isinstance(pin.get('patch_tip'), str)
            or not re.fullmatch('[0-9a-f]{7,40}', pin['patch_tip'])):
        raise ValueError('pin_invalid')
    return pin


def _strict_pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise ValueError('native_receipt_invalid')
        result[key] = value
    return result


def _json_constant(_):
    raise ValueError('native_receipt_invalid')


def native_source_facts(root):
    """Recount all supported repository-local Cargo inputs from actual Git blobs."""
    root = Path(root)
    result = {}
    entries = _git(root, 'ls-tree', '-rz', 'HEAD', '--', 'litellm-rust', '.cargo')
    for row in entries.split(b'\0'):
        if not row:
            continue
        meta, raw_name = row.split(b'\t', 1)
        mode, kind, oid = meta.split()
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            raise ValueError('native_source_mapping')
        rel = raw_name.decode('utf-8')
        path = root/rel
        fact = file_fact(path)
        data = path.read_bytes()
        if (bool(fact['mode'] & 0o111) != (mode == b'100755')
                or hashlib.sha256(data).hexdigest() != fact['sha256']
                or hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest() != oid.decode()):
            raise ValueError('native_source_changed')
        result[rel] = {'git_mode': mode.decode(), 'sha256': fact['sha256']}
        if len(result) > 20000:
            raise ValueError('runtime_bound')
    for required in ('litellm-rust/Cargo.toml', 'litellm-rust/Cargo.lock', '.cargo/config.toml'):
        if required not in result:
            raise ValueError('native_source_mapping')
    seen = 0
    for base in (root/'litellm-rust', root/'.cargo'):
        if base.is_symlink() or not base.is_dir():
            raise ValueError('native_source_mapping')
        for directory, dirs, files in os.walk(base, followlinks=False):
            parent = Path(directory)
            if parent == root/'litellm-rust':
                dirs[:] = [name for name in dirs if name != 'target']
            if any((parent/name).is_symlink() for name in dirs):
                raise ValueError('native_source_mapping')
            seen += len(dirs)+len(files)
            if seen > 50000:
                raise ValueError('runtime_bound')
            for name in files:
                if (parent/name).relative_to(root).as_posix() not in result:
                    raise ValueError('untracked_native_source')
    return result


def _tool_output(argv, timeout=30, limit=4*1024*1024):
    """Bound observation during pipe reads, not after buffering all output."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    chunks, total = [], 0
    deadline = time.monotonic()+timeout
    try:
        with selectors.DefaultSelector() as watch:
            watch.register(proc.stdout, selectors.EVENT_READ)
            while watch.get_map():
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise ValueError('native_observation_timeout')
                events = watch.select(min(remaining, 0.1))
                for key, _ in events:
                    data = os.read(key.fd, min(65536, limit-total+1))
                    if not data:
                        watch.unregister(key.fileobj)
                        continue
                    total += len(data)
                    if total > limit:
                        raise ValueError('native_observation_bound')
                    chunks.append(data)
        if proc.wait(timeout=max(0.001, deadline-time.monotonic())):
            raise ValueError('native_observation_invalid')
        return b''.join(chunks)
    finally:
        if proc.poll() is None:
            proc.kill()  # only the retained observer child, never a proxy PID
        proc.wait(timeout=5)
        proc.stdout.close()


def _native_abi(path):
    """Inspect data only; exact platform dependency allowlist, no dylib loading."""
    if (sys.platform != 'darwin' or platform.machine() != 'arm64'
            or sys.implementation.name != 'cpython' or sys.version_info[:2] < (3, 10)):
        raise ValueError('native_abi_invalid')
    with Path(path).open('rb') as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise ValueError('native_abi_invalid')
        magic, cpu, subtype, kind, count, size, flags, reserved = struct.unpack('<IiiIIIII', header)
        if (magic != 0xfeedfacf or cpu != 0x0100000c or subtype != 0 or reserved != 0
                or kind != 6 or count > 8192 or size > 1048576):
            raise ValueError('native_abi_invalid')
        commands = stream.read(size)
    if len(commands) != size:
        raise ValueError('native_abi_invalid')
    loads = {0xc, 0x80000018, 0x8000001f, 0x80000023, 0x20}
    safe = {0x19, 0x80000022, 2, 0xb, 0xd, 0x1b, 0x32, 0x2a,
            0x26, 0x29, 0x1d, 0x80000034, 0x80000033}
    offset, macos_platform = 0, False
    for _ in range(count):
        if offset+8 > size:
            raise ValueError('native_abi_invalid')
        command, length = struct.unpack_from('<II', commands, offset)
        if length < 8 or length % 8 or offset+length > size or command not in loads | safe:
            raise ValueError('native_dependency_invalid')
        if command == 0x32:  # LC_BUILD_VERSION: PLAT_MACOS, not iOS/simulator
            if length < 24 or macos_platform:
                raise ValueError('native_abi_invalid')
            target, minimum, sdk, tools = struct.unpack_from('<IIII', commands, offset+8)
            if target != 1 or tools > 128 or length != 24+8*tools:
                raise ValueError('native_abi_invalid')
            macos_platform = True
        if command in loads:
            if length < 24:
                raise ValueError('native_dependency_invalid')
            start = struct.unpack_from('<I', commands, offset+8)[0]
            if not 24 <= start < length:
                raise ValueError('native_dependency_invalid')
            raw = commands[offset+start:offset+length]
            if b'\0' not in raw or raw.split(b'\0', 1)[0].decode('utf-8') not in SYSTEM_LIBRARIES:
                raise ValueError('native_dependency_invalid')
        offset += length
    if offset != size or not macos_platform:
        raise ValueError('native_abi_invalid')
    observed = _tool_output(['/usr/bin/nm', '-gU', str(path)])
    if re.search(rb'(?m)(?:^|\s)_PyInit__native\s*$', observed) is None:
        raise ValueError('native_abi_invalid')


def _native_metadata(pin_path, pin, head=None):
    receipt_path = Path(pin_path).with_name('native-build.json')
    if 'native_artifact' not in pin:
        if os.path.lexists(receipt_path):
            raise ValueError('native_receipt_unpaired')
        return None
    item = pin['native_artifact']
    if (not isinstance(item, dict) or set(item) != NATIVE_FIELDS
            or type(item['version']) is not int or item['version'] != 1 or item['path'] != NATIVE_PATH
            or any(not isinstance(item[k], str) or not re.fullmatch('[0-9a-f]{64}', item[k])
                   for k in ('sha256', 'receipt_sha256', 'baseline_pin_digest'))
            or not isinstance(item['source_tree'], str) or not re.fullmatch('[0-9a-f]{40}', item['source_tree'])):
        raise ValueError('native_pin_invalid')
    baseline = {key: value for key, value in pin.items() if key != 'native_artifact'}
    if digest(baseline) != item['baseline_pin_digest']:
        raise ValueError('native_baseline_changed')
    if receipt_path.lstat().st_size > 65536:
        raise ValueError('native_receipt_invalid')
    receipt_fact = file_fact(receipt_path)
    info = receipt_path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('native_receipt_invalid')
    data = receipt_path.read_bytes()
    if (len(data) > 65536 or receipt_fact['sha256'] != item['receipt_sha256']
            or hashlib.sha256(data).hexdigest() != item['receipt_sha256']):
        raise ValueError('native_receipt_invalid')
    record = json.loads(data, object_pairs_hook=_strict_pairs, parse_constant=_json_constant)
    if (not isinstance(record, dict) or set(record) != RECEIPT_FIELDS
            or type(record['version']) is not int or record['version'] != 1
            or type(record['source_files']) is not int or not 0 < record['source_files'] <= 20000
            or record['default_features'] is not True or record['features'] != ['extension-module']
            or record['profile'] != 'dev' or record['target'] != 'aarch64-apple-darwin'
            or record['recipe'] != 'cargo-locked-offline-dev-abi3-arm64-v1' or record['abi_tag'] != 'cp310-abi3'
            or record['artifact_path'] != NATIVE_PATH
            or not isinstance(record['source_head'], str) or not re.fullmatch('[0-9a-f]{40}', record['source_head'])
            or (head is not None and record['source_head'] != head)
            or record['source_tree'] != item['source_tree'] or record['artifact_sha256'] != item['sha256']
            or record['baseline_pin_digest'] != item['baseline_pin_digest']):
        raise ValueError('native_receipt_invalid')
    for key in RECEIPT_FIELDS:
        if key.endswith('_sha256') or key in ('source_digest', 'baseline_pin_digest'):
            if not isinstance(record[key], str) or not re.fullmatch('[0-9a-f]{64}', record[key]):
                raise ValueError('native_receipt_invalid')
    for name in ('cargo', 'rustc'):
        if not isinstance(record[name+'_version'], str) or not re.fullmatch(name+r' [^\r\n]{1,200}', record[name+'_version']):
            raise ValueError('native_receipt_invalid')
    return item, record, receipt_fact


def native_receipt_fact(pin_path):
    """Metadata-only shared strict pairing; never locate/import the package."""
    data = _native_metadata(pin_path, read_pin(pin_path))
    return data[2] if data is not None else None


def native_artifact_fact(root, pin_path, pin, head):
    data = _native_metadata(pin_path, pin, head)
    if data is None:
        return None
    item, record, receipt_fact = data
    receipt_path = Path(pin_path).with_name('native-build.json')
    sources = native_source_facts(root)
    tree = _git(root, 'rev-parse', 'HEAD:litellm-rust').decode().strip()
    if (tree != item['source_tree'] or len(sources) != record['source_files']
            or digest(sources) != record['source_digest']
            or sources['litellm-rust/Cargo.lock']['sha256'] != record['cargo_lock_sha256']
            or sources['.cargo/config.toml']['sha256'] != record['root_cargo_config_sha256']):
        raise ValueError('native_source_changed')
    path = Path(root)/NATIVE_PATH
    fact = file_fact(path)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022 or fact['sha256'] != item['sha256']:
        raise ValueError('native_artifact_invalid')
    _native_abi(path)
    if file_fact(path) != fact or file_fact(receipt_path) != receipt_fact:
        raise ValueError('native_input_changed')
    return {'artifact': fact, 'receipt': receipt_fact, 'source_digest': digest(sources),
            'baseline_pin_digest': item['baseline_pin_digest']}


def prove_runtime(pin_path):
    # All compile/marshal operations below compare data only. No cached code is
    # executed; malformed or unmatched caches fail the isolated helper process.
    pin = read_pin(pin_path)
    # Locate without executing a possibly stale cache. Verify the tree/caches
    # first; importing for the final path check must not repair cache evidence.
    sys.dont_write_bytecode = True
    spec = importlib.util.find_spec('litellm')
    if spec is None or spec.origin is None:
        raise ValueError('runtime_source_mapping')
    package = Path(spec.origin).resolve().parent
    root = Path(_git(package, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
    head = _git(root, 'rev-parse', 'HEAD').decode().strip()
    version = importlib.metadata.version('litellm')
    if version != pin['base_version'] or not head.startswith(pin['patch_tip']):
        raise ValueError('runtime_pin_mismatch')
    pending, roots, expected, tracked, links = [package], set(), {}, set(), set()
    while pending:
        base = pending.pop()
        if base in roots:
            continue
        roots.add(base)
        if len(roots) > 32:
            raise ValueError('runtime_source_mapping')
        relative = base.relative_to(root).as_posix()
        entries = _git(root, 'ls-tree', '-rz', 'HEAD', '--', relative)
        if not entries:
            raise ValueError('runtime_source_mapping')
        for row in entries.split(b'\0'):
            if not row:
                continue
            metadata, raw_path = row.split(b'\t', 1)
            mode, kind, oid = metadata.split()
            if kind != b'blob' or mode not in (b'100644', b'100755', b'120000'):
                raise ValueError('runtime_source_mapping')
            rel = raw_path.decode('utf-8')
            path = root/rel
            if mode == b'120000':
                if not path.is_symlink():
                    raise ValueError('runtime_source_changed')
                data = os.readlink(path).encode()
                target = path.resolve(strict=True)
                target.relative_to(root)  # tracked enterprise links may stay in the same pinned tree
                links.add(path)
                pending.append(target)
                fact = {'sha256': hashlib.sha256(data).hexdigest(), 'mode': 'symlink'}
            else:
                fact = file_fact(path)
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != fact['sha256']:
                    raise ValueError('runtime_source_changed')
                if bool(fact['mode'] & 0o111) != (mode == b'100755'):
                    raise ValueError('runtime_source_changed')
                tracked.add(path.resolve())
            algorithm = 'sha1' if len(oid) == 40 else 'sha256'
            blob = hashlib.new(algorithm, b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
            if blob != oid.decode():
                raise ValueError('runtime_source_changed')
            expected[rel] = fact
            if len(expected) > 100000:
                raise ValueError('runtime_bound')
    if not expected or Path(spec.origin).resolve() not in tracked:
        raise ValueError('runtime_source_mapping')
    native = native_artifact_fact(root, pin_path, pin, head)
    native_path = root/NATIVE_PATH if native is not None else None
    seen = 0
    for base in roots:
        paths = base.rglob('*') if base.is_dir() else [base]
        for path in paths:
            seen += 1
            if seen > 200000:
                raise ValueError('runtime_bound')
            if path.is_symlink():
                if path not in links:
                    raise ValueError('untracked_runtime_code')
                continue
            if not path.is_file():
                continue
            with path.open('rb') as stream:
                native_magic = stream.read(4) in MACH_MAGICS
            if path.suffix in ('.so', '.pyd', '.dylib', '.bundle') or native_magic:
                if native_path is None or path != native_path:
                    raise ValueError('untracked_runtime_code')
            if path.suffix == '.py' and path.resolve() not in tracked:
                raise ValueError('untracked_runtime_code')
            if path.suffix != '.pyc':
                continue
            if '__pycache__' in path.parts:
                if sys.implementation.cache_tag not in path.name:
                    continue
                cache_pattern = r'.+\.'+re.escape(sys.implementation.cache_tag)+r'(?:\.opt-[12])?\.pyc'
                if not re.fullmatch(cache_pattern, path.name):
                    raise ValueError('unverified_bytecode')
                source = Path(importlib.util.source_from_cache(str(path))).resolve()
            else:
                source = path.with_suffix('.py').resolve()
            if source not in tracked:
                raise ValueError('unmapped_bytecode')
            data = path.read_bytes()
            if len(data) < 16 or len(data) > 64 * 1024 * 1024 or data[:4] != importlib.util.MAGIC_NUMBER:
                raise ValueError('unverified_bytecode')
            if int.from_bytes(data[4:8], 'little') not in (0, 1, 3):
                raise ValueError('unverified_bytecode')
            level = re.search(r'\.opt-([12])\.pyc$', path.name)
            optimization = int(level.group(1)) if level else 0
            # Trusted pinned-tree cache data is decoded only in this bounded
            # helper and compared, never executed (marshal is not used on RPC).
            cached = marshal.loads(data[16:])
            if not hasattr(cached, 'co_filename'):
                raise ValueError('unverified_bytecode')
            compiled = compile(source.read_bytes(), cached.co_filename, 'exec',
                               dont_inherit=True, optimize=optimization)
            if cached != compiled:
                raise ValueError('unverified_bytecode')
    distribution = importlib.metadata.distribution('litellm')
    metadata = {}
    for name in ('METADATA', 'WHEEL', 'direct_url.json', 'entry_points.txt', 'INSTALLER', 'top_level.txt', 'RECORD'):
        data = distribution.read_text(name)
        metadata[name] = hashlib.sha256(data.encode()).hexdigest() if data is not None else None
    if metadata['RECORD'] is None or metadata['METADATA'] is None:
        raise ValueError('distribution_unverifiable')
    mapped = Path(str(distribution.locate_file('litellm/__init__.py'))).resolve() == package/'__init__.py'
    direct = distribution.read_text('direct_url.json')
    if not mapped and direct is not None:
        origin = json.loads(direct)
        url = urlparse(origin.get('url', ''))
        mapped = (origin.get('dir_info', {}).get('editable') is True and url.scheme == 'file'
                  and not url.netloc and Path(unquote(url.path)).resolve() == root)
    if not mapped:
        raise ValueError('distribution_unverifiable')
    module = importlib.import_module('litellm')
    origin = getattr(module, '__file__', None)
    if not isinstance(origin, str) or Path(origin).resolve() != package/'__init__.py':
        raise ValueError('runtime_source_mapping')
    identity = {'interpreter': file_fact(Path(sys.executable).resolve()),
                'prefix': str(Path(sys.prefix).resolve()), 'package': str(package),
                'version': version, 'head': head, 'files': expected,
                'distribution_metadata': metadata, 'native': native}
    return {'runtime_digest': digest(identity)}


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('invalid_input')
        print(json.dumps(prove_runtime(sys.argv[1])))
    except Exception:
        print('{"error":"runtime_invalid"}')
        raise SystemExit(78)
