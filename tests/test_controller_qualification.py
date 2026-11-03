"""Qualification fixtures: no real launchctl, production state or credentials."""
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from test_restart_runtime import runtime, private_root
from restart_control.engine import Fault, atomic_json
from restart_control.runtime_identity import file_fact


@pytest.fixture
def context(runtime, private_root):
    home = private_root/'qualification-home'
    supervisor = home/'Library/LaunchAgents'/(runtime.policy['controller_label']+'.plist')
    supervisor.parent.mkdir(parents=True)
    expected = {
        'Label': runtime.policy['controller_label'],
        'ProgramArguments': [runtime.python, '-I', str(runtime.controller_root/'restart_controller.py'),
                             '--policy', str(runtime.policy_path), 'serve'],
        'WorkingDirectory': str(runtime.controller_root), 'RunAtLoad': True, 'KeepAlive': True,
        'ThrottleInterval': 30, 'ExitTimeOut': 1000,
        'EnvironmentVariables': {'HOME': str(home),
            'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
        'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}
    supervisor.write_bytes(plistlib.dumps(expected, sort_keys=True))
    supervisor.chmod(0o600)
    plan = dict(runtime.freeze(), controller_epoch='a'*32)
    identity = {'pid': os.getpid(), 'uid': os.getuid(), 'birth': [1, 1]}
    now, mono = time.time(), time.monotonic()
    record = {'version': 1, 'operator_uid': os.getuid(),
        'controller_label': expected['Label'], 'controller_identity': identity,
        'controller_digest': plan['controller_digest'], 'policy_digest': plan['policy_digest'],
        'plist_digest': file_fact(runtime.plist)['sha256'], 'controller_epoch': plan['controller_epoch'],
        'issued_at': now-1, 'expires_at': now+60, 'issued_monotonic': mono-1,
        'expires_monotonic': mono+60, 'canary_evidence_digest': 'b'*64,
        'controller_plist_digest': hashlib.sha256(supervisor.read_bytes()).hexdigest(),
        'controller_supervision': {key: expected[key] for key in ('KeepAlive', 'ThrottleInterval', 'RunAtLoad', 'ExitTimeOut')}}
    atomic_json(runtime.state/'qualification.json', record)
    value = SimpleNamespace(runtime=runtime, home=home, supervisor=supervisor, expected=expected,
                            record=record, plan=plan, identity=identity)
    yield value


def loaded_text(context, changes=None):
    """Explicit fake launchctl print payload, parsed by the real implementation."""
    job = copy.deepcopy(context.expected)
    changes = changes or {}
    job.update(changes)
    env = dict(job['EnvironmentVariables'])
    env.setdefault('XPC_SERVICE_NAME', job['Label'])
    args = '\n'.join('\t\t'+arg for arg in job['ProgramArguments'])
    environment = '\n'.join('\t\t'+key+' => '+val for key, val in env.items())
    return (f'gui/{os.getuid()}/{job["Label"]} = {{\n'
            f'\tpath = {changes.get("loaded_path", context.supervisor)}\n'
            f'\tprogram = {changes.get("loaded_program", job["ProgramArguments"][0])}\n'
            f'\tworking directory = {job["WorkingDirectory"]}\n'
            f'\targuments = {{\n{args}\n\t}}\n'
            f'\tenvironment = {{\n{environment}\n\t}}\n'
            f'\tminimum runtime = {job["ThrottleInterval"]}\n'
            f'\texit timeout = {job["ExitTimeOut"]}\n'
            f'\tpid = {context.identity["pid"]}\n\truns = 1\n}}\n')


def qualify(context, text=None):
    payload = loaded_text(context) if text is None else text
    def print_only(argv, **kwargs):
        assert argv == ['/bin/launchctl', 'print', f'gui/{os.getuid()}/'+context.expected['Label']]
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr='')
    with mock.patch('restart_control.runtime.Path.home', return_value=context.home), \
         mock.patch('restart_control.runtime.process_identity', return_value={**context.identity, 'ppid': 1}), \
         mock.patch('restart_control.runtime._identity', side_effect=lambda pid: dict(context.identity, pid=pid)), \
         mock.patch('restart_control.runtime.subprocess.run', side_effect=print_only):
        context.runtime.qualify(context.plan)


def test_matching_generated_and_loaded_controller_is_fixture_control(context):
    qualify(context)


@pytest.mark.parametrize('variant', ['program', 'argv', 'policy_argv', 'path', 'workdir', 'environment', 'extra_environment', 'service_name', 'throttle', 'exit_timeout'])
def test_matching_birth_cannot_hide_wrong_loaded_controller(context, variant):
    qualify(context)  # green control before the single metadata mutation
    expected = context.expected
    changes = {}
    if variant == 'program': changes['loaded_program'] = '/usr/bin/false'
    elif variant == 'argv': changes['ProgramArguments'] = [expected['ProgramArguments'][0], '-c', 'pass']
    elif variant == 'policy_argv':
        args = list(expected['ProgramArguments']); args[-2] += '.different'; changes['ProgramArguments'] = args
    elif variant == 'path': changes['loaded_path'] = str(context.supervisor)+'.different'
    elif variant == 'workdir': changes['WorkingDirectory'] = str(context.home)
    elif variant == 'environment': changes['EnvironmentVariables'] = dict(expected['EnvironmentVariables'], PATH='/usr/bin')
    elif variant == 'extra_environment': changes['EnvironmentVariables'] = dict(expected['EnvironmentVariables'], FIXTURE_UNEXPECTED='1')
    elif variant == 'service_name': changes['EnvironmentVariables'] = dict(expected['EnvironmentVariables'], XPC_SERVICE_NAME='fixture.wrong')
    elif variant == 'exit_timeout': changes['ExitTimeOut'] = 60
    else: changes['ThrottleInterval'] = 90
    with pytest.raises(Fault, match='qualification_invalid'):
        qualify(context, loaded_text(context, changes))


@pytest.mark.parametrize('key,value', [('KeepAlive', False), ('RunAtLoad', False), ('ThrottleInterval', 31), ('ExitTimeOut', 1)])
def test_matching_birth_cannot_hide_wrong_controller_plist(context, key, value):
    qualify(context)
    changed = dict(context.expected, **{key: value})
    context.supervisor.write_bytes(plistlib.dumps(changed, sort_keys=True))
    with pytest.raises(Fault, match='qualification_invalid'):
        qualify(context)


@pytest.mark.parametrize('variant', ['wrong_uid', 'absent_daemon', 'boolean_version', 'wrong_epoch',
                                   'missing_binding', 'wrong_digest', 'wrong_supervision', 'bool_coercion', 'identity_coercion'])
def test_operator_record_bindings_fail_closed(context, variant):
    qualify(context)
    record = copy.deepcopy(context.record)
    if variant == 'wrong_uid': record['operator_uid'] += 1
    elif variant == 'boolean_version': record['version'] = True
    elif variant == 'wrong_epoch': record['controller_epoch'] = 'c'*32
    elif variant == 'missing_binding': record.pop('controller_plist_digest')
    elif variant == 'wrong_digest': record['controller_plist_digest'] = 'c'*64
    elif variant == 'wrong_supervision': record['controller_supervision']['KeepAlive'] = False
    elif variant == 'bool_coercion': record['controller_supervision']['KeepAlive'] = 1
    elif variant == 'identity_coercion': record['controller_identity']['birth'][0] = True
    atomic_json(context.runtime.state/'qualification.json', record)
    with pytest.raises(Fault, match='qualification_invalid'):
        qualify(context, '' if variant == 'absent_daemon' else None)


@pytest.mark.parametrize('variant', ['missing', 'symlink', 'public_mode', 'malformed', 'unowned'])
def test_controller_plist_must_be_private_owned_and_exact(context, variant):
    qualify(context)
    if variant == 'missing': context.supervisor.unlink()
    elif variant == 'symlink':
        target = context.supervisor.with_suffix('.fixture-target')
        context.supervisor.rename(target)
        context.supervisor.symlink_to(target)
    elif variant == 'public_mode': context.supervisor.chmod(0o644)
    elif variant == 'malformed': context.supervisor.write_bytes(b'fixture-invalid-plist')
    if variant == 'unowned':
        real_stat = Path.stat
        def stat_fixture(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == context.supervisor and kwargs.get('follow_symlinks', True):
                return SimpleNamespace(st_uid=os.getuid()+1)
            return info
        with mock.patch('restart_control.runtime.Path.stat', autospec=True, side_effect=stat_fixture):
            with pytest.raises(Fault, match='qualification_invalid'):
                qualify(context)
    else:
        with pytest.raises(Fault, match='qualification_invalid'):
            qualify(context)


@pytest.mark.parametrize('field', ['program', 'working directory', 'minimum runtime', 'exit timeout'])
def test_unavailable_loaded_controller_fields_are_not_inferred(context, field):
    qualify(context)
    text = '\n'.join(line for line in loaded_text(context).splitlines() if not line.strip().startswith(field+' ='))
    with pytest.raises(Fault, match='qualification_invalid'):
        qualify(context, text)


def test_installer_and_qualification_share_generated_definition(context):
    from restart_control.runtime import controller_job
    with mock.patch('restart_control.runtime.Path.home', return_value=context.home):
        assert controller_job(context.runtime.policy, context.runtime.policy_path, context.runtime.python) == context.expected


@pytest.mark.parametrize('variant', ['duplicate', 'nested_only', 'malformed', 'wrong_with_nested_match'])
def test_loaded_exit_timeout_observation_is_unique_top_level_and_exact(context, variant):
    qualify(context)
    line='\texit timeout = 1000\n'
    text=loaded_text(context)
    assert text.count(line)==1
    if variant=='duplicate':text=text.replace(line,line+'\texit timeout = 60\n')
    elif variant=='nested_only':text=text.replace(line,'\tresource coalition = {\n\t\texit timeout = 1000\n\t}\n')
    elif variant=='malformed':text=text.replace(line,'\texit timeout = true\n')
    else:text=text.replace(line,'\texit timeout = 60\n\tresource coalition = {\n\t\texit timeout = 1000\n\t}\n')
    with pytest.raises(Fault, match='qualification_invalid'):
        qualify(context,text)
