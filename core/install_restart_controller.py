#!/usr/bin/env python3
"""Install a new controller bundle/plist only; NEVER activate or replace a job."""
from pathlib import Path
import argparse
import json
import os
import plistlib
import re
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from restart_control.engine import Fault, _read_json, canonical_digest, private_dir, atomic_json
from restart_control.runtime import POLICY_FIELDS
from restart_control.runtime_identity import CONTROLLER_INPUTS, file_fact


def install(policy_path, verify=False):
    # verify is an installation-byte comparison flag, unrelated to TLS.
    policy_path = Path(policy_path).absolute()
    policy = _read_json(policy_path)
    if (not isinstance(policy, dict) or set(policy) != POLICY_FIELDS
            or type(policy['version']) is not int or policy['version'] != 1):
        raise Fault('runtime_invalid')
    paths = POLICY_FIELDS - {'version', 'candidate_port', 'models', 'controller_label'}
    if any(not isinstance(policy[k], str) or not Path(policy[k]).is_absolute() for k in paths):
        raise Fault('runtime_invalid')
    label = policy['controller_label']
    if (not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', label)
            or type(policy['candidate_port']) is not int or not 1024 <= policy['candidate_port'] <= 65535
            or not isinstance(policy['models'], list) or len(policy['models']) != 2
            or any(not isinstance(m, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,128}', m)
                   for m in policy['models']) or len(set(policy['models'])) != 2):
        raise Fault('runtime_invalid')
    kit = Path(policy['kit_root']).resolve(strict=True)
    if kit != Path(__file__).resolve().parents[1]:
        raise Fault('runtime_invalid')
    proxy = plistlib.loads(Path(policy['plist']).read_bytes())
    if label == proxy['Label']:
        raise Fault('runtime_invalid')
    python = proxy['EnvironmentVariables']['LITELLM_PROXY_PYTHON']
    if not Path(python).is_absolute() or not Path(python).is_file():
        raise Fault('runtime_invalid')
    destination = Path(policy['controller_root'])
    for p in (destination, *destination.parents):
        if p.is_symlink():
            raise Fault('state_io')
    plist_path = Path.home()/'Library/LaunchAgents'/(label+'.plist')
    data = plistlib.dumps({'Label': label,
        'ProgramArguments': [python, '-I', str(destination/'restart_controller.py'), '--policy', str(policy_path), 'serve'],
        'WorkingDirectory': str(destination), 'RunAtLoad': True, 'KeepAlive': True,
        'ThrottleInterval': 30, 'ExitTimeOut': 1000,
        'EnvironmentVariables': {'HOME': str(Path.home()),
            'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
        'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}, sort_keys=True)
    sources = {name: file_fact(kit/name) for name in CONTROLLER_INPUTS}
    receipt = {'version': 1, 'policy_digest': canonical_digest(policy), 'sources': sources}
    if verify:
        private_dir(destination) if destination.exists() else (_ for _ in ()).throw(Fault('state_io'))
        if (plist_path.is_symlink() or plist_path.read_bytes() != data
                or _read_json(destination/'install.json') != receipt
                or any(file_fact(destination/name.removeprefix('core/')) != fact for name, fact in sources.items())):
            raise Fault('runtime_invalid')
        return {'status': 'verified', 'activation': 'not_performed'}
    if destination.exists() or destination.is_symlink() or plist_path.exists() or plist_path.is_symlink():
        raise Fault('state_io')
    if not plist_path.parent.is_dir():
        raise Fault('state_io')
    # Ancestor creation is explicit controller data provisioning, never chmod an
    # existing parent, overwrite an existing bundle, or touch proxy assets.
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_dir(destination)
    private_dir(destination/'restart_control')
    for name in CONTROLLER_INPUTS:
        target = destination/name.removeprefix('core/')
        shutil.copy2(kit/name, target)
        if file_fact(target) != sources[name]:
            raise Fault('runtime_invalid')
    atomic_json(destination/'install.json', receipt)
    fd = os.open(plist_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return {'status': 'installed', 'activation': 'not_performed', 'qualification': 'operator_required'}


def main(argv=None):
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--policy', required=True)
        parser.add_argument('--verify', action='store_true')
        args = parser.parse_args(argv)
        print(json.dumps(install(args.policy, args.verify), sort_keys=True))
        return 0
    except (Exception, KeyboardInterrupt):
        print('{"status":"blocked","code":"runtime_invalid"}')
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
