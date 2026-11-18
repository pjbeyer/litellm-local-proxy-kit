"""Isolated fixtures only: Git provenance, HTTP children and mocked launchctl.

No production listener, credential store or real service-control call is used.
"""
import importlib.util
import json
import marshal
import os
from pathlib import Path
import plistlib
import py_compile
import select
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

import pytest

# Helper/candidate tests spawn a real subprocess helper that records its kernel
# identity; it cannot inherit the parent-stub on Linux.
from conftest import REQUIRES_REAL_MAC
from conftest import real_mac_available

DARWIN_ONLY = pytest.mark.skipif(not real_mac_available(),
                                 reason="binds the darwin kernel boot identity (real Mac required)")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'core'))
from restart_control.engine import Fault, atomic_json, private_dir
from restart_control.runtime import Runtime
from restart_control.runtime_identity import KIT_INPUTS, PROXY_INPUTS, CONTROLLER_INPUTS, file_fact, source_facts
from restart_lifecycle_adapter import FixtureLifecycleAdapter


@pytest.fixture
def private_root():
    with tempfile.TemporaryDirectory(prefix='rt-', dir=os.environ['TMPDIR']) as name:
        yield Path(name)


@pytest.fixture
def package(private_root):
    root = private_root
    source = root/'litellm'
    source.mkdir()
    (source/'__init__.py').write_text('VALUE = 1\n')
    info = root/'litellm-1.2.3.dist-info'
    info.mkdir()
    (info/'METADATA').write_text('Name: litellm\nVersion: 1.2.3\n')
    (info/'RECORD').write_text('litellm/__init__.py,,\n')
    env = {'HOME': str(root), 'PATH': os.defpath, 'TMPDIR': os.environ['TMPDIR'],
           'PYTHONPATH': str(root), 'PYTHONDONTWRITEBYTECODE': '1'}
    def git(*args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(root), *args], env=env,
                                       stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q')
    def commit():
        git('add', 'litellm')
        if (root/'enterprise').exists():
            git('add', 'enterprise')
        git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        tip = git('rev-parse', 'HEAD')
        (root/'pin.lock').write_text('base_version = "1.2.3"\npatch_tip = "'+tip+'"\n')
    commit()
    def proof():
        result = subprocess.run([sys.executable, '-B', str(ROOT/'core/restart_control/runtime_identity.py'),
                                str(root/'pin.lock')], env=env, capture_output=True, timeout=30)
        return result.returncode, json.loads(result.stdout)
    return root, source, info, git, commit, proof


def test_clean_pinned_tree_and_verified_cache(package):
    root, source, _, _, _, proof = package
    py_compile.compile(str(source/'__init__.py'), doraise=True)
    code, body = proof()
    assert code == 0, body
    assert len(body['runtime_digest']) == 64


def test_corrupt_cache_refused_before_import_and_not_repaired(package):
    root, source, _, _, _, proof = package
    path = Path(py_compile.compile(str(source/'__init__.py'), doraise=True))
    marker = source/'executed.fixture'
    altered = compile('from pathlib import Path\nPath(__file__).with_name("executed.fixture").write_text("executed")\n',
                      str(source/'__init__.py'), 'exec', dont_inherit=True)
    bad = path.read_bytes()[:16]+marshal.dumps(altered)
    path.write_bytes(bad)
    assert proof() == (78, {'error': 'runtime_invalid'})
    assert not marker.exists()
    assert path.read_bytes() == bad


@pytest.mark.parametrize('ignored', [False, True])
def test_untracked_and_ignored_code_refused(package, ignored):
    root, source, _, _, _, proof = package
    if ignored:
        (root/'.gitignore').write_text('litellm/extra.py\n')
    (source/'extra.py').write_text('VALUE = 2\n')
    assert proof()[0] == 78


@pytest.mark.parametrize('line', ['base_version = "9.9.9"', 'patch_tip = "000000000000"', 'patch_tip = "HEAD~1"'])
def test_pin_cannot_be_rebound_or_repaired(package, line):
    root, _, _, _, _, proof = package
    pin = root/'pin.lock'
    key = line.split(' = ')[0]
    pin.write_text('\n'.join(line if row.startswith(key) else row for row in pin.read_text().splitlines())+'\n')
    assert proof()[0] == 78


def test_tracked_source_content_not_git_diff_is_authority(package):
    root, source, _, git, _, proof = package
    git('update-index', '--assume-unchanged', 'litellm/__init__.py')
    (source/'__init__.py').write_text('VALUE = 9\n')
    assert not git('diff', '--name-only')
    assert proof()[0] == 78


def test_distribution_metadata_changes_digest(package):
    _, _, info, _, _, proof = package
    before = proof()
    assert before[0] == 0
    with (info/'METADATA').open('a') as out:
        out.write('Summary: fixture metadata mutation\n')
    after = proof()
    assert after[0] == 0
    assert before[1] != after[1]


def test_tracked_in_tree_enterprise_link_is_verified(package):
    root, source, _, _, commit, proof = package
    (root/'enterprise').mkdir()
    (root/'enterprise/__init__.py').write_text('VALUE = 1\n')
    (source/'enterprise').symlink_to('../enterprise', target_is_directory=True)
    commit()
    assert proof()[0] == 0
    (root/'enterprise/__init__.py').write_text('VALUE = 2\n')
    assert proof()[0] == 78


def test_escaping_tracked_link_refused(package):
    root, source, _, _, commit, proof = package
    (source/'escape').symlink_to(root.parent, target_is_directory=True)
    commit()
    assert proof()[0] == 78


@pytest.fixture
def runtime(private_root):
    kit, config = private_root/'kit', private_root/'config'
    config.mkdir()
    for name in KIT_INPUTS:
        target = kit/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/name, target)
    label = 'fixture.local.proxy'
    for name in (*PROXY_INPUTS, label+'.plist', label+'.rotate.plist'):
        (config/name).write_text('fixture-owned input\n')
    manifest = '[[credential]]\nenv_var = "FIXTURE_HTTP_KEY"\nop_ref = "op://fixture/item/field"\nrequired = true\n'
    (config/'credentials.toml').write_text(manifest)
    job = {'Label': label, 'WorkingDirectory': str(config),
           'ProgramArguments': ['/bin/bash', str(config/'run-proxy-wrapper.sh'), '49131'],
           'EnvironmentVariables': {'LITELLM_PROXY_LABEL': label,
              'LITELLM_PROXY_CONFIG': str(config/'config.rendered.yaml'),
              'CREDENTIALS_MANIFEST': str(config/'credentials.toml'),
              'LITELLM_PROXY_PYTHON': sys.executable, 'LITELLM_DEPLOYMENT': 'fixture'},
           'KeepAlive': True, 'RunAtLoad': True, 'ThrottleInterval': 30}
    data = plistlib.dumps(job)
    (config/(label+'.plist')).write_bytes(data)
    auto = private_root/'autoload.plist'
    auto.write_bytes(data)
    deployment = private_root/'deployment'
    (deployment/'deployments/fixture').mkdir(parents=True)
    (deployment/'deploy.conf.sh').write_text('fixture deployment\n')
    for name in ('overlay.yaml', 'credentials.toml', 'pin.lock'):
        (deployment/'deployments/fixture'/name).write_text('fixture source\n')
    legacy_pin = 'base_version = "1.2.3"\npatch_tip = "'+'0'*40+'"\n'
    (config/'pin.lock').write_text(legacy_pin)
    (deployment/'deployments/fixture/pin.lock').write_text(legacy_pin)
    state = private_root/'state'
    policy = {'version': 1, 'kit_root': str(kit), 'deployment_root': str(deployment),
              'plist': str(config/(label+'.plist')), 'auto_plist': str(auto),
              'state_dir': str(state), 'scratch_dir': str(private_root/'scratch'),
              'candidate_port': 49132, 'models': ['fixture-direct', 'fixture-router'],
              'controller_root': str(kit/'core'), 'controller_label': 'fixture.controller'}
    policy_path = private_root/'policy.json'
    atomic_json(policy_path, policy)
    with mock.patch.dict(os.environ, {'OP_SERVICE_ACCOUNT_TOKEN': 'fixture-only',
                                     'PYTHONPATH': 'fixture-only', 'PYTHONHOME': 'fixture-only'}):
        class FixtureRuntime(Runtime):
            # The offline fixture runtime class: qualification accepts
            # only fixture/isolated-launchd evidence scopes here, never
            # intended-controller (production) evidence.
            fixture = True
        obj = FixtureRuntime(policy_path, test_lifecycle=FixtureLifecycleAdapter(namespace='restart_control'))
    obj.run_proof_worker = mock.Mock(return_value=json.dumps({'runtime_digest': 'a'*64}).encode())
    return obj


def test_clean_plist_environment_and_registered_sources(runtime):
    assert not {'OP_SERVICE_ACCOUNT_TOKEN', 'PYTHONPATH', 'PYTHONHOME'}.intersection(runtime.env)
    assert runtime.env['TMPDIR'] == str(runtime.scratch)
    assert set(source_facts(runtime.kit)) == set(KIT_INPUTS)
    assert set(runtime.freeze()) == {'asset_digest', 'runtime_digest', 'policy_digest', 'controller_digest'}


@pytest.mark.parametrize('change', ['source', 'manifest', 'extra_helper', 'policy'])
def test_loaded_generation_is_immutable(runtime, change):
    runtime.freeze()
    if change == 'source':
        (runtime.kit/'core/check_pin.py').write_text('# fixture mutation\n')
    elif change == 'manifest':
        with (runtime.config/'credentials.toml').open('a') as out:
            out.write('# fixture mutation\n')
    elif change == 'extra_helper':
        (runtime.kit/'core/unregistered.py').write_text('# fixture helper\n')
    else:
        body = json.loads(runtime.policy_path.read_text())
        body['candidate_port'] += 1
        atomic_json(runtime.policy_path, body)
    with pytest.raises(Fault, match='runtime_invalid'):
        runtime.freeze()


def test_lkg_snapshot_bytes_modes_and_manifest_bound_to_generation(runtime):
    frozen = runtime.freeze()
    path = runtime.state/'fixture-lkg'
    path.mkdir(mode=0o700)
    binding = runtime.snapshot_lkg(path)
    runtime.validate_lkg(path, binding)
    selected = path/'run-proxy-wrapper.sh'
    selected.write_text('fixture mutated bytes\n')
    manifest = json.loads((path/'manifest.json').read_text())
    manifest[selected.name] = file_fact(selected)
    atomic_json(path/'manifest.json', manifest)
    with pytest.raises(Fault, match='lkg_invalid'):
        runtime.validate_lkg(path, binding)
    assert binding['asset_digest'] == frozen['asset_digest']


def test_runtime_drift_refuses_restore_without_writes(runtime):
    runtime.freeze()
    path = runtime.state/'fixture-lkg'
    path.mkdir(mode=0o700)
    binding = runtime.snapshot_lkg(path)
    old = (runtime.config/'run-proxy-wrapper.sh').read_bytes()
    runtime.run_proof_worker.return_value = json.dumps({'runtime_digest': 'b'*64}).encode()
    with pytest.raises(Fault, match='lkg_invalid'):
        runtime.restore_lkg(path, binding)
    assert (runtime.config/'run-proxy-wrapper.sh').read_bytes() == old


def test_installer_owned_layout_and_isolated_daemon_status(runtime, private_root):
    import install_restart_controller as installer
    from restart_control.wire import client
    home = private_root/'fixture-home'
    (home/'Library/LaunchAgents').mkdir(parents=True)
    policy = dict(runtime.policy, kit_root=str(ROOT), controller_root=str(private_root/'installed'))
    atomic_json(runtime.policy_path, policy)
    with mock.patch.object(installer.Path, 'home', return_value=home):
        assert installer.install(runtime.policy_path)['activation'] == 'not_performed'
        assert installer.install(runtime.policy_path, verify=True)['status'] == 'verified'
        with pytest.raises(Fault):
            installer.install(runtime.policy_path)
    job = plistlib.loads((home/'Library/LaunchAgents/fixture.controller.plist').read_bytes())
    # Execute the installed -I layout directly, NOT launchctl. It is deliberately
    # unqualified and never receives prepare/restart or accesses production.
    proc = subprocess.Popen(job['ProgramArguments'], cwd=job['WorkingDirectory'],
           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={'HOME': str(home),
           'PATH': os.defpath, 'TMPDIR': os.environ['TMPDIR']})
    try:
        endpoint = runtime.state/'controller.sock'
        deadline = time.monotonic()+10
        while not endpoint.exists() and proc.poll() is None and time.monotonic()<deadline:
            time.sleep(.02)
        assert endpoint.exists()
        assert client(endpoint, {'op': 'status'})['status'] == 'ok'
    finally:
        if proc.poll() is None:
            proc.terminate()  # retained disposable fixture daemon only
        proc.wait(timeout=10)
        proc.stdout.close(); proc.stderr.close()
    assert proc.returncode == 0
    assert not endpoint.exists()


def test_freeze_compares_installed_closure_not_live_kit_head(runtime, private_root):
    """lpkit-j4z: an installed bundle must be validated against its
    install.json `sources` closure snapshot, NOT against live kit HEAD bytes.
    A stale bundle (installed from an older kit) still validates when its
    copies match the receipt; a bundle that genuinely diverges from its own
    receipt still refuses controller_copy_changed."""
    bundle = private_root / "installed"
    private_dir(bundle)
    (bundle / "restart_control").mkdir(mode=0o700)
    sources = {}
    for name in CONTROLLER_INPUTS:
        target = bundle / name.removeprefix("core/")
        if name == "core/restart_control/runtime.py":
            # Simulate a stale bundle: this installed copy differs from the
            # current kit HEAD bytes (a later kit commit advanced HEAD).
            target.write_bytes((runtime.kit / name).read_bytes() + b"# stale\n")
        else:
            shutil.copy2(runtime.kit / name, target)
        sources[name] = file_fact(target)
    # The receipt records the byte closure actually installed.
    atomic_json(bundle / "install.json", {"version": 1, "sources": sources})
    runtime.controller_root = bundle

    # Pre-fix this refused controller_copy_changed (bundle != kit HEAD). With
    # the fix freeze() validates against install.json.sources and passes.
    frozen = runtime.freeze()
    assert set(frozen) == {"asset_digest", "runtime_digest", "policy_digest",
                           "controller_digest"}

    # A bundle that diverges from its own receipt still refuses: patch one
    # installed copy, leave the receipt untouched.
    (bundle / "restart_control" / "canary.py").write_text("// tampered\n")
    with pytest.raises(Fault, match="runtime_invalid"):
        runtime.freeze()


def test_fast_candidate_exit_is_reaped_and_allows_later_helpers(runtime):
    del runtime.run_proof_worker  # exercise the real fixed helper, not the fixture seam
    runtime._listener = lambda _: None
    def gone(_):
        runtime._candidate.wait(timeout=5)
        raise Fault('primary_changed')
    with mock.patch('restart_control.runtime._identity', side_effect=gone):
        with pytest.raises(Fault, match='candidate_failed'):
            runtime.rehearse()
    assert runtime._candidate is None
    assert not runtime._cleanup_blocked
    assert runtime._capture([sys.executable, '-c', 'print("fixture-ok")']) == b'fixture-ok\n'


@DARWIN_ONLY
def test_helper_stdout_is_bounded_during_capture(runtime):
    del runtime.run_proof_worker
    children = []
    original = subprocess.Popen
    def retained(*args, **kwargs):
        child = original(*args, **kwargs)
        if args[0][0] == sys.executable:
            children.append(child)
        return child
    started = time.monotonic()
    with mock.patch('restart_control.runtime.HELPER_OUTPUT_LIMIT', 65536), \
         mock.patch('restart_control.runtime.subprocess.Popen', side_effect=retained):
        with pytest.raises(Fault, match='gate_failed'):
            runtime._capture([sys.executable, '-c',
                'import os,time; os.write(1,b"x"*(8*1024*1024)); time.sleep(20)'], timeout=30)
    assert time.monotonic()-started < 5
    assert len(children) == 1 and children[0].poll() is not None
    assert not runtime._cleanup_blocked


def test_missing_qualification_is_a_gate(runtime):
    with pytest.raises(Fault, match='qualification_missing'):
        runtime.qualify({})


HTTP_CHILD = r'''
import json,sys,threading
from http.server import BaseHTTPRequestHandler,HTTPServer
mode=sys.argv[1]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def answer(self,code,body):
  data=json.dumps(body).encode(); self.send_response(code); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
 def do_GET(self):
  if mode=='slow':
   import time
   for byte in b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}':
    try: self.wfile.write(bytes([byte])); self.wfile.flush()
    except OSError: return
    time.sleep(.05)
   return
  if mode=='redirect' and self.path=='/v1/models':
   self.send_response(302); self.send_header('Location',sys.argv[2]); self.end_headers(); return
  if self.path.startswith('/health/'): return self.answer(200,{'fixture':True})
  if self.headers.get('Authorization')!='Bearer fixture-http': return self.answer(401,{'fixture':True})
  self.answer(200,{'data':[{'id':'fixture-model'}]})
 def do_POST(self):
  self.rfile.read(int(self.headers.get('Content-Length','0')))
  self.answer(200,{'choices':[{'message':{'content':'fixture-only-response'}}]})
s=HTTPServer(('127.0.0.1',0),Handler)
print(s.server_address[1],flush=True)
def control():
 sys.stdin.readline(); s.shutdown()
threading.Thread(target=control,daemon=True).start()
s.serve_forever(); s.server_close()
'''


@pytest.fixture
def http_child(private_root):
    children = []
    def start(mode='ok', location=''):
        proc = subprocess.Popen([sys.executable, '-u', '-c', HTTP_CHILD, mode, location],
               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=private_root)
        children.append(proc)
        assert select.select([proc.stdout], [], [], 10)[0]
        return proc, int(proc.stdout.readline())
    yield start
    for proc in children:
        if proc.poll() is None:
            proc.stdin.write(b'shutdown\n'); proc.stdin.flush()
            proc.wait(timeout=5)
        proc.stdin.close(); proc.stdout.close()


@REQUIRES_REAL_MAC
def test_real_fixture_http_acceptance_without_bodies_or_proxy_inheritance(runtime, http_child):
    proc, port = http_child()
    runtime.policy['candidate_port'] = port
    runtime._key = 'fixture-http'
    with mock.patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1'}):
        checks = runtime._accept(port, lambda: {'pid': proc.pid, 'fixture': True}, time.monotonic()+5)
    assert checks['invalid_key'] == 401
    assert checks['models']['count'] == 1
    assert len(checks['completions']) == 2
    assert checks['mcp_parity'] == 'not_established'
    assert 'fixture-only-response' not in json.dumps(checks)
    assert 'fixture-http' not in json.dumps(checks)


@REQUIRES_REAL_MAC
def test_http_redirect_is_refused_before_forwarding_auth(runtime, http_child):
    _, port = http_child('redirect', 'http://127.0.0.1:1/credential-should-not-be-forwarded')
    runtime.policy['candidate_port'] = port
    assert runtime._request(port, '/v1/models', 'fixture-http') == (302, {})


@REQUIRES_REAL_MAC
def test_absolute_http_deadline_stops_trickled_headers(runtime, http_child):
    _, port = http_child('slow')
    runtime.policy['candidate_port'] = port
    started = time.monotonic()
    with mock.patch('restart_control.runtime.REQUEST_TIMEOUT', .2):
        with pytest.raises(Fault, match='postcheck_failed'):
            runtime._request(port, '/v1/models', 'fixture-http')
    assert time.monotonic()-started < 2


def test_unapproved_http_port_is_refused_before_network(runtime):
    with pytest.raises(Fault, match='postcheck_failed'):
        runtime._request(1, '/v1/models', 'fixture-http')


@pytest.mark.parametrize('runs,parity', [(2, True), (1, False)])
def test_bootstrap_respawn_or_loaded_policy_mismatch_fails_before_http(runtime, runs, parity):
    runtime._bootstrap_digest = file_fact(runtime.plist)
    runtime._started_at, runtime._started_identity = time.monotonic(), None
    runtime._job_state = mock.Mock(return_value={'runs': runs, 'parity': parity,
                                                'identity': {'pid': 42, 'uid': os.getuid(), 'birth': [1, 1]}})
    with pytest.raises(Fault, match='postcheck_failed'):
        runtime.accept_started()


@REQUIRES_REAL_MAC
def test_pending_bootstrap_then_first_incarnation_is_accepted(runtime, http_child):
    proc, port = http_child()
    runtime.port = port
    runtime._key = 'fixture-http'
    runtime._bootstrap_digest = file_fact(runtime.plist)
    runtime._started_at, runtime._started_identity = time.monotonic(), None
    identity = {'pid': proc.pid, 'uid': os.getuid(), 'birth': [1, 1]}
    states = [{'runs': 0, 'identity': None, 'parity': True},
              {'runs': 1, 'identity': identity, 'parity': True}]
    runtime._job_state = lambda: states.pop(0) if len(states) > 1 else states[0]
    runtime._listener = lambda _: identity
    accepted = runtime.accept_started()
    assert accepted['identity'] == identity
    assert json.loads((runtime.state/'bootstrap.json').read_text())['identity'] == identity


def test_not_found_signature_on_stderr_classifies_absence(runtime):
    """launchctl's not-found text is on stderr; absence must classify.

    Live gate-4 finding: launchctl print writes 'Could not find
    service ...' to STDERR (with a leading 'Bad request.' line), while
    _observe returned only stdout — so every real 113 carried an empty
    payload, the signature check missed, and the lane recorded
    unmanaged_listener instead of proven absence. The print
    observation must include stderr in the returned bytes.
    """
    def pipe_with(blob):
        read_end, write_end = os.pipe()
        os.write(write_end, blob)
        os.close(write_end)
        return read_end

    class FakeChild:
        def __init__(self):
            self.stdout = os.fdopen(pipe_with(b''), 'rb')
            self.stderr = os.fdopen(pipe_with(
                b'Bad request.\nCould not find service '
                b'"fixture.local.proxy" in domain for '
                b'user gui: %d\n' % os.getuid()), 'rb')
            self.returncode = 113
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0

    created = []

    def fake_popen(argv, **kwargs):
        created.append(list(argv))
        return FakeChild()

    with mock.patch('restart_control.runtime.subprocess.Popen',
                    side_effect=fake_popen):
        state = runtime._job_state(runtime.label,
                                   expected=mock.Mock(
                                       ProgramArguments=['/bin/x'],
                                       EnvironmentVariables={},
                                       ThrottleInterval=30),
                                   expected_path=runtime.plist)
    assert created and created[0][:3] == ['/bin/launchctl', 'print',
                                          'gui/%d/' % os.getuid() + runtime.label]
    assert state is None  # classified absence, not unmanaged_listener
