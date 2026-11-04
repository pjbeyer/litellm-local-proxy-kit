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
    'harvest-filter-metrics.sh',
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def file_fact(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('non_regular_input')
    data = path.read_bytes()
    after = path.lstat()
    if (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) != (
            after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
        raise ValueError('input_changed')
    return {'sha256': hashlib.sha256(data).hexdigest(), 'mode': stat.S_IMODE(info.st_mode)}


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


def prove_runtime(pin_path):
    # All compile/marshal operations below compare data only. No cached code is
    # executed; malformed or unmatched caches fail the isolated helper process.
    text = Path(pin_path).read_text()
    version_match = re.search(r'base_version\s*=\s*"([^\"]+)"', text)
    tip_match = re.search(r'patch_tip\s*=\s*"([0-9a-f]+)"', text)
    if not version_match or not tip_match:
        raise ValueError('pin_invalid')
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
    if version != version_match.group(1) or not head.startswith(tip_match.group(1)):
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
            if path.suffix in ('.py', '.so', '.pyd', '.dylib') and path.resolve() not in tracked:
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
                'distribution_metadata': metadata}
    return {'runtime_digest': digest(identity)}


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('invalid_input')
        print(json.dumps(prove_runtime(sys.argv[1])))
    except Exception:
        print('{"error":"runtime_invalid"}')
        raise SystemExit(78)
