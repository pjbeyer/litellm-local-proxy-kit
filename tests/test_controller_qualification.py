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
from restart_control.runtime import (CANARY_EVIDENCE_KEYS, CANARY_CHECK_KEYS,
                                     controller_job, controller_supervision)
from restart_control.runtime_identity import file_fact
from restart_control.shutdown import SHUTDOWN_CONTRACT


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
        'ThrottleInterval': 30, 'ExitTimeOut': 60,
        'EnvironmentVariables': {'HOME': str(home),
            'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
            'OSLogRateLimit': '64'},
        'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}
    supervisor.write_bytes(plistlib.dumps(expected, sort_keys=True))
    supervisor.chmod(0o600)
    plan = dict(runtime.freeze(), controller_epoch='a'*32)
    identity = {'pid': os.getpid(), 'uid': os.getuid(), 'ppid': os.getppid(),
                'pgid': os.getpgid(0), 'birth': [1, 1]}
    now, mono = time.time(), time.monotonic()
    # Fixture canary evidence: scope 'fixture' only; the real producer
    # arrives in later tasks and fixture records never become accepted
    # production qualifications.
    evidence = {'version': 1, 'scope': 'fixture', 'nonce': '0'*32,
        'boot_identity': '12345678-1234-1234-1234-123456789abc', 'operator_uid': os.getuid(),
        'controller_identity': identity, 'controller_epoch': plan['controller_epoch'],
        'controller_digest': plan['controller_digest'], 'policy_digest': plan['policy_digest'],
        'controller_plist_digest': hashlib.sha256(supervisor.read_bytes()).hexdigest(),
        'controller_supervision': controller_supervision(expected),
        'shutdown_contract': json.loads(json.dumps(dict(SHUTDOWN_CONTRACT))),
        'canary_label': 'local.restart-canary.'+'0'*32, 'canary_plist_digest': 'd'*64,
        'incarnations': [{'pid': 41001, 'uid': os.getuid(), 'ppid': 1, 'pgid': 41001,
                          'birth': [2, 0], 'runs': 1},
                         {'pid': 41002, 'uid': os.getuid(), 'ppid': 1, 'pgid': 41002,
                          'birth': [3, 0], 'runs': 2}],
        'checks': {key: True for key in CANARY_CHECK_KEYS}, 'detail_digest': 'e'*64,
        'issued_at': now-1, 'issued_monotonic': mono-1}
    assert set(evidence) == CANARY_EVIDENCE_KEYS
    evidence_dir = runtime.state/'canary'/'evidence'
    evidence_dir.mkdir(parents=True, exist_ok=True)
    # Closed detail: the evidence's detail_digest names a real validated
    # detail file at its true content digest, cross-bound to the same
    # nonce/boot/identity/epoch as the evidence envelope. The detail
    # carries the full ordered step record with the SECOND incarnation's
    # identity in the shutdown block, exactly as a real lane would write.
    detail_dir = runtime.state/'canary'/'detail'
    detail_dir.mkdir(parents=True, exist_ok=True)
    from restart_control.canary import STEP_ORDER, STEP_APPLICABILITY, validate_detail
    second = {'pid': 41002, 'uid': os.getuid(), 'ppid': 1,
              'pgid': 41002, 'birth': [3, 0]}
    first = {'pid': 41001, 'uid': os.getuid(), 'ppid': 1,
             'pgid': 41001, 'birth': [2, 0]}
    steps = []
    for index, name in enumerate(STEP_ORDER):
        rule = STEP_APPLICABILITY[name]
        which = first if rule['identity'] == 'first' else second
        steps.append({'name': name, 'started_monotonic': mono+index,
                      'completed_monotonic': mono+index+0.5,
                      'outcome': 'passed',
                      'identity': dict(which) if rule['identity'] else None,
                      'runs': rule['runs'],
                      'loaded_exit_seconds': 60 if rule['loaded'] else None,
                      'loaded_environment_digest': 'f'*64 if rule['loaded'] else None,
                      'exit_code': rule['exit_code']})
    detail = {'version': 1, 'nonce': evidence['nonce'],
              'boot_identity': evidence['boot_identity'],
              'controller_identity': evidence['controller_identity'],
              'controller_epoch': evidence['controller_epoch'],
              'steps': steps, 'shutdown': {
                  'identity': second,
                  'requested_monotonic': mono-2, 'exit_observed_monotonic': mono-1,
                  'cleanup_completed_monotonic': mono-0.5,
                  'deadline_seconds': SHUTDOWN_CONTRACT['shutdown_deadline_seconds']}}
    validate_detail(detail, expected={'nonce': evidence['nonce'],
                                      'boot_identity': evidence['boot_identity'],
                                      'controller_identity': evidence['controller_identity'],
                                      'controller_epoch': evidence['controller_epoch']})
    detail_blob = json.dumps(detail, sort_keys=True).encode()
    detail_path = detail_dir/(hashlib.sha256(detail_blob).hexdigest()+'.json')
    detail_path.write_bytes(detail_blob)
    detail_path.chmod(0o600)
    evidence['detail_digest'] = hashlib.sha256(detail_blob).hexdigest()
    blob = json.dumps(evidence, sort_keys=True).encode()
    evidence_path = evidence_dir/(hashlib.sha256(blob).hexdigest()+'.json')
    evidence_path.write_bytes(blob)
    evidence_path.chmod(0o600)
    canary_digest = file_fact(evidence_path)['sha256']
    record = {'version': 2, 'operator_uid': os.getuid(),
        'controller_label': expected['Label'], 'controller_identity': identity,
        'controller_digest': plan['controller_digest'], 'policy_digest': plan['policy_digest'],
        'plist_digest': file_fact(runtime.plist)['sha256'], 'controller_epoch': plan['controller_epoch'],
        'issued_at': now-1, 'expires_at': now+60, 'issued_monotonic': mono-1,
        'expires_monotonic': mono+60, 'canary_evidence_digest': canary_digest,
        'shutdown_contract': json.loads(json.dumps(dict(SHUTDOWN_CONTRACT))),
        'controller_plist_digest': hashlib.sha256(supervisor.read_bytes()).hexdigest(),
        'controller_supervision': controller_supervision(expected)}
    atomic_json(runtime.state/'qualification.json', record)
    value = SimpleNamespace(runtime=runtime, home=home, supervisor=supervisor, expected=expected,
                            record=record, plan=plan, identity=identity,
                            evidence=evidence, evidence_path=evidence_path)
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
    def observe_only(self, argv, timeout, env=None, control=False):
        assert argv == ['/bin/launchctl', 'print', f'gui/{os.getuid()}/'+context.expected['Label']]
        return 0, payload.encode()
    with mock.patch('restart_control.runtime.Path.home', return_value=context.home), \
         mock.patch('restart_control.runtime.process_identity', return_value={**context.identity, 'ppid': 1}), \
         mock.patch('restart_control.runtime._identity', side_effect=lambda pid: {k: v for k, v in dict(context.identity, pid=pid).items() if k in ('pid', 'uid', 'birth')}), \
         mock.patch.object(type(context.runtime), '_observe', autospec=True, side_effect=observe_only):
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
    elif variant == 'exit_timeout': changes['ExitTimeOut'] = 1000  # old generation
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


@pytest.mark.parametrize('variant', ['duplicate', 'nested_only', 'malformed', 'wrong_with_nested_match', 'clamped_declaration'])
def test_loaded_exit_timeout_observation_is_unique_top_level_and_exact(context, variant):
    qualify(context)
    line='\texit timeout = 60\n'
    text=loaded_text(context)
    assert text.count(line)==1
    if variant=='duplicate':text=text.replace(line,line+'\texit timeout = 1000\n')
    elif variant=='nested_only':text=text.replace(line,'\tresource coalition = {\n\t\texit timeout = 60\n\t}\n')
    elif variant=='malformed':text=text.replace(line,'\texit timeout = true\n')
    elif variant=='clamped_declaration':text=text.replace(line,'\texit timeout = 59.5\n')
    else:text=text.replace(line,'\texit timeout = 1000\n\tresource coalition = {\n\t\texit timeout = 60\n\t}\n')
    with pytest.raises(Fault, match='qualification_invalid'):
        qualify(context,text)


@pytest.mark.parametrize('variant', ['missing', 'wrong_value', 'wrong_type', 'conflicting_duplicate'])
def test_loaded_logging_environment_is_declared_exactly(context, variant):
    """OSLogRateLimit is a declared key, not an implicitly ignored extra."""
    qualify(context)
    text=loaded_text(context)
    if variant=='missing':
        text='\n'.join(l for l in text.splitlines()
                       if not l.strip().startswith('OSLogRateLimit =>'))
    elif variant=='wrong_value':
        text=text.replace('OSLogRateLimit => 64','OSLogRateLimit => 128')
    elif variant=='wrong_type':
        text=text.replace('OSLogRateLimit => 64','OSLogRateLimit => true')
    else:
        env_line='\t\tOSLogRateLimit => 64\n'
        # a CONFLICTING repeat cannot attest which value launchd loaded
        text=text.replace(env_line, env_line+'\t\tOSLogRateLimit => 128\n')
    text=text+'\n'
    with pytest.raises(Fault, match='qualification_invalid'):
        qualify(context,text)


def test_identical_repeated_environment_value_is_not_ambiguous(context):
    """launchd re-emits injected defaults alongside the plist's copy.

    Live gate-4 finding: launchctl print lists OSLogRateLimit twice
    (launchd's own injected default and the plist's declared copy)
    with the SAME value. An identical repeat attests one loaded
    value; only a conflicting repeat is ambiguous and refuses.
    """
    qualify(context)
    env_line='\t\tOSLogRateLimit => 64\n'
    text=loaded_text(context).replace(env_line, env_line+env_line)+'\n'
    qualify(context, text)


# -- T015: qualification v2 shutdown-contract and evidence bindings -----------

@pytest.mark.parametrize('variant', ['missing', 'extra_field', 'legacy_version', 'boolean_version',
                                   'wrong_deadline', 'wrong_logging', 'extra_contract_key'])
def test_shutdown_contract_binding_downgrades_in_turn(context, variant):
    """Each independent shutdown-contract binding refuses on its own."""
    qualify(context)
    record = copy.deepcopy(context.record)
    if variant == 'missing':
        record.pop('shutdown_contract')
    elif variant == 'extra_field':
        record['unexpected_binding'] = 1
    elif variant == 'legacy_version':
        record['version'] = 1  # legacy v1 records refuse; no automatic upgrade
    elif variant == 'boolean_version':
        record['version'] = True
    elif variant == 'wrong_deadline':
        record['shutdown_contract']['shutdown_deadline_seconds'] = 100
    elif variant == 'wrong_logging':
        record['shutdown_contract']['logging_environment']['OSLogRateLimit'] = '128'
    else:
        record['shutdown_contract']['future_key'] = 'fixture'
    atomic_json(context.runtime.state/'qualification.json', record)
    with pytest.raises(Fault, match='qualification_invalid'):
        qualify(context)


@pytest.mark.parametrize('variant', ['missing_file', 'digest_mismatch', 'public_mode', 'unowned',
                                   'symlink', 'extra_key', 'wrong_scope', 'false_check', 'bad_checks'])
def test_canary_evidence_file_bindings_fail_closed(context, variant):
    """The evidence digest references a validated fixed-path file, not a string."""
    qualify(context)
    record = copy.deepcopy(context.record)
    evidence = copy.deepcopy(context.evidence)
    if variant == 'missing_file':
        context.evidence_path.unlink()
        atomic_json(context.runtime.state/'qualification.json', record)
        with pytest.raises(Fault, match='qualification_invalid'):
            qualify(context)
        return
    if variant == 'digest_mismatch':
        # Record points at a digest that has no matching evidence file.
        record['canary_evidence_digest'] = 'f'*64
    elif variant == 'public_mode':
        context.evidence_path.chmod(0o644)
    elif variant == 'unowned':
        real_stat = Path.stat
        def stat_fixture(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == context.evidence_path and kwargs.get('follow_symlinks', True):
                return SimpleNamespace(st_uid=os.getuid()+1)
            return info
        atomic_json(context.runtime.state/'qualification.json', record)
        with mock.patch('restart_control.runtime.Path.stat', autospec=True, side_effect=stat_fixture):
            with pytest.raises(Fault, match='qualification_invalid'):
                qualify(context)
        return
    elif variant == 'symlink':
        target = context.evidence_path.with_suffix('.target')
        context.evidence_path.rename(target)
        context.evidence_path.symlink_to(target)
    elif variant == 'extra_key':
        evidence['unexpected'] = 1
        blob = json.dumps(evidence, sort_keys=True).encode()
        record['canary_evidence_digest'] = hashlib.sha256(blob).hexdigest()
        path = context.evidence_path.parent/(hashlib.sha256(blob).hexdigest()+'.json')
        path.write_bytes(blob); path.chmod(0o600)
    elif variant == 'wrong_scope':
        evidence['scope'] = 'intended-controller'  # fixture files cannot claim production scope
        blob = json.dumps(evidence, sort_keys=True).encode()
        record['canary_evidence_digest'] = hashlib.sha256(blob).hexdigest()
        path = context.evidence_path.parent/(hashlib.sha256(blob).hexdigest()+'.json')
        path.write_bytes(blob); path.chmod(0o600)
    elif variant == 'false_check':
        evidence['checks']['cleanup'] = False
        blob = json.dumps(evidence, sort_keys=True).encode()
        record['canary_evidence_digest'] = hashlib.sha256(blob).hexdigest()
        path = context.evidence_path.parent/(hashlib.sha256(blob).hexdigest()+'.json')
        path.write_bytes(blob); path.chmod(0o600)
    else:
        evidence['checks'] = {**evidence['checks'], 'surprise': True}
        blob = json.dumps(evidence, sort_keys=True).encode()
        record['canary_evidence_digest'] = hashlib.sha256(blob).hexdigest()
        path = context.evidence_path.parent/(hashlib.sha256(blob).hexdigest()+'.json')
        path.write_bytes(blob); path.chmod(0o600)
    atomic_json(context.runtime.state/'qualification.json', record)
    with pytest.raises(Fault, match='qualification_invalid'):
        qualify(context)


def test_sibling_evidence_never_qualifies_another_identity(context):
    """An isolated sibling controller's evidence cannot qualify this record."""
    qualify(context)
    # Re-point the record at a second, separately-owned evidence file that
    # binds a different controller identity (a sibling test context).
    sibling = copy.deepcopy(context.evidence)
    sibling['controller_identity'] = {'pid': 99999, 'uid': os.getuid(), 'birth': [9, 9]}
    blob = json.dumps(sibling, sort_keys=True).encode()
    digest = hashlib.sha256(blob).hexdigest()
    path = context.evidence_path.parent/(digest+'.json')
    path.write_bytes(blob); path.chmod(0o600)
    record = copy.deepcopy(context.record)
    record['canary_evidence_digest'] = digest
    atomic_json(context.runtime.state/'qualification.json', record)
    with pytest.raises(Fault, match='qualification_invalid'):
        qualify(context)
