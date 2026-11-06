# tests/test_sink_watchdog.py
import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


WATCHDOG = Path(__file__).resolve().parents[1] / 'integrations/hermes/proxy-lifecycle-guard/guard_probe_watchdog.py'
VALID = '{"ts":"2026-11-06T12:00:00+0000","category":"VETO","code":"LGV-001","tool":"terminal"}\n'


def harness(tmp_path):
    deploy = tmp_path / 'private-deploy'
    deploy.mkdir(mode=0o700)
    policy = tmp_path / 'policy.json'
    policy.write_text('{}', encoding='utf-8')
    tree = ast.parse(WATCHDOG.read_text(encoding='utf-8'))
    selected: list[ast.stmt] = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in {'_probe_eval', '_sink_ok'}]
    assert {node.name for node in selected if isinstance(node, ast.FunctionDef)} == {'_probe_eval', '_sink_ok'}
    namespace = {'os': os, 'sys': sys, 'json': json, 'subprocess': subprocess,
                 'DEPLOY_DIR': deploy, 'POLICY': str(policy),
                 'SINK_FILE': tmp_path / 'sink.jsonl'}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(WATCHDOG), 'exec'), namespace)
    return namespace


def install_stub(namespace):
    source = '''
import json
from datetime import datetime, timezone
SINK_PATH = __SINK__
written = 0
failed = 0
def sink_start():
    global written, failed
    written = failed = 0
    return True
def _sink_persist(home, raw, cap):
    global written
    with open(SINK_PATH, 'ab') as output:
        output.write(raw)
    written += 1
    return True
def evaluate(tool, args, policy):
    command = args.get('command')
    if command == 'allow':
        return None
    code = 'LGV-999' if command == 'bad-category-code' else 'LGV-001'
    record_code = 'LGV-032' if command == 'wrong-code' else code
    record_tool = 'tool_call' if command == 'wrong-tool' else tool.rsplit('.', 1)[-1]
    record = {'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S%z'), 'category': 'VETO',
              'code': record_code, 'tool': record_tool}
    raw = (bytes((255, 10)) if command == 'invalid-utf8-record' else
           json.dumps(record, separators=(',', ':')).encode('utf-8') + bytes((10,)))
    _sink_persist(None, raw, 512)
    return {'category': 'VETO', 'log_code': code}
def sink_flush(timeout_s):
    return True
def sink_status():
    return {'written': written, 'failed': failed}
def sink_shutdown(timeout_s):
    return True
'''.replace('__SINK__', repr(str(namespace['SINK_FILE'])))
    (namespace['DEPLOY_DIR'] / 'guard.py').write_text(source, encoding='utf-8')


@pytest.mark.parametrize('command', ['bad-category-code', 'wrong-code',
                                     'wrong-tool', 'invalid-utf8-record'])
def test_child_rejects_invalid_code_or_actual_record(tmp_path, command):
    namespace = harness(tmp_path)
    install_stub(namespace)
    assert namespace['_probe_eval'](command).startswith('PROBE_ERROR:')


def test_current_child_receipt_and_allow(tmp_path):
    namespace = harness(tmp_path)
    install_stub(namespace)
    receipt = {'stale': True}
    assert namespace['_probe_eval']('normal', receipt=receipt) == 'VETO'
    assert receipt == {'category': 'VETO', 'code': 'LGV-001',
                       'tool': 'terminal', 'delivered': True}
    assert namespace['_sink_ok']('VETO', 'terminal', receipt) is True
    assert namespace['_probe_eval']('allow', receipt=receipt) == 'ALLOW'
    assert receipt == {}
    assert namespace['_sink_ok']('VETO', 'terminal', receipt) is False


def test_stale_valid_line_cannot_prove_current_job(tmp_path):
    namespace = harness(tmp_path)
    namespace['SINK_FILE'].write_text(VALID, encoding='utf-8')
    assert namespace['_sink_ok']('VETO', 'terminal') is False


def test_symlinked_old_line_cannot_prove_current_job(tmp_path):
    namespace = harness(tmp_path)
    target = tmp_path / 'old-sink.jsonl'
    target.write_text(VALID, encoding='utf-8')
    link = tmp_path / 'sink-link.jsonl'
    link.symlink_to(target)
    namespace['SINK_FILE'] = link
    assert namespace['_sink_ok']('VETO', 'terminal') is False


@pytest.mark.parametrize('kind', ['valid', 'symlink', 'oversized'])
def test_sink_ok_never_accesses_filesystem(tmp_path, kind):
    namespace = harness(tmp_path)
    target = tmp_path / 'old.jsonl'
    target.write_text(VALID, encoding='utf-8')
    if kind == 'valid':
        namespace['SINK_FILE'] = target
    elif kind == 'symlink':
        link = tmp_path / 'old-link.jsonl'
        link.symlink_to(target)
        namespace['SINK_FILE'] = link
    else:
        huge = tmp_path / 'huge.jsonl'
        huge.write_bytes(b'x' * 200000 + bytes((255,)))
        namespace['SINK_FILE'] = huge
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise OSError('filesystem access in _sink_ok')
    namespace['open'] = forbidden
    namespace['os'] = SimpleNamespace(open=forbidden,
                                      path=SimpleNamespace(isfile=forbidden))
    assert namespace['_sink_ok']('VETO', 'terminal') is False
    assert calls == []


def test_sink_ok_rejects_non_dict_and_invalid_utf8_input(tmp_path):
    namespace = harness(tmp_path)
    sink_ok = namespace['_sink_ok']
    for value in (None, [], json.loads('"VETO"'), bytes((255,))):
        assert sink_ok('VETO', 'terminal', value) is False
    for code, tool, delivered in [('LGV-999', 'terminal', True),
                                  ('LGV-001', 'tool_call', True),
                                  ('LGV-001', 'terminal', 1)]:
        receipt = {'category': 'VETO', 'code': code,
                   'tool': tool, 'delivered': delivered}
        assert sink_ok('VETO', 'terminal', receipt) is False




def test_actual_guard_invalid_policy_delivers_owned_record(tmp_path, monkeypatch):
    import hashlib
    import shutil

    namespace = harness(tmp_path)
    deploy = namespace['DEPLOY_DIR']
    policy = deploy / 'config.json'
    policy.write_bytes(b'{}')
    policy.chmod(0o600)
    namespace['POLICY'] = str(policy)
    source_digests = {}
    for name in ('guard.py', '__init__.py'):
        original = WATCHDOG.parent / name
        copied = deploy / name
        shutil.copyfile(original, copied)
        copied.chmod(0o600)
        copied_digest = hashlib.sha256(copied.read_bytes()).hexdigest()
        assert copied_digest == hashlib.sha256(original.read_bytes()).hexdigest()
        source_digests[name] = copied_digest
    reference = {'policy_digest': hashlib.sha256(policy.read_bytes()).hexdigest(),
                 'source_digests': source_digests}
    integrity = deploy / 'integrity.sha256'
    integrity.write_text(json.dumps(reference), encoding='utf-8')
    integrity.chmod(0o600)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))

    receipt = {}
    assert namespace['_probe_eval']('echo fixture', receipt=receipt) == 'POLICY_INVALID'
    assert receipt == {'category': 'POLICY_INVALID', 'code': 'LGV-030',
                       'tool': 'terminal', 'delivered': True}
    assert namespace['_sink_ok']('POLICY_INVALID', 'terminal', receipt) is True
    files = list((tmp_path / 'state').rglob('*.jsonl'))
    assert files
    records = [json.loads(line) for path in files
               for line in path.read_text(encoding='utf-8').splitlines()]
    assert records
    assert all(set(record) == {'ts', 'category', 'code', 'tool'}
               and record['category'] == 'POLICY_INVALID'
               and record['code'] == 'LGV-030'
               and record['tool'] == 'terminal' for record in records)
