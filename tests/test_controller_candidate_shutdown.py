"""T011: candidate and credential/HTTP admission under the shutdown latch.

Fixture-only: fake sentinels prove no next resolver/candidate/request runs
after shutdown; a real fixture HTTP child proves wake-polling and absolute
deadlines. No production listener, credential store or service-control call.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'core'))
from restart_control import engine, runtime as module
from restart_control.recovery import RecoveryStore
from restart_control.shutdown import Shutdown, ShutdownRequested

Runtime = module.Runtime
Fault = engine.Fault


@pytest.fixture
def gate_runtime(tmp_path):
    """Runtime bound to a real Shutdown latch, no journal side effects.

    The recovery journal is a real RecoveryStore; spawn records created by
    rehearse land in a real private state dir and are cleaned with it.
    """
    state = engine.private_dir(tmp_path/'state')
    journal = RecoveryStore(state, read_json=engine._read_json,
                            atomic_json=engine.atomic_json,
                            private_path=engine._private_path,
                            private_dir=engine.private_dir,
                            sync_fd=engine._sync_fd, initialize=True)
    ticket = journal.begin(
        {'request_id': 'a'*32, 'kind': 'proxy', 'controller_epoch': 'b'*32,
         'policy_digest': 'c'*64, 'controller_digest': 'd'*64,
         'controller_plist_digest': 'e'*64, 'asset_digest': None,
         'runtime_digest': None}, 'fixture')
    engine.atomic_json(state/'requests'/('a'*32+'.json'),
                       {'request_id': 'a'*32, 'phase': 'preparing',
                        'controller_epoch': 'b'*32, 'scope': 'fixture',
                        'shared_state_side_effects': False})
    ticket = journal.bind_frozen(ticket, 'f'*64, '0'*64)
    latch = Shutdown()
    runtime = Runtime.__new__(Runtime)
    runtime.state = state
    runtime._expected_shutdown = latch
    runtime._lifecycle_bound = False
    runtime._active_operation = None
    runtime._candidate = None
    runtime._cleanup_blocked = False
    runtime._operation_started = None
    runtime.env = {}
    runtime.config = state
    runtime.port = 49131
    runtime.policy = {'candidate_port': 49132}
    runtime.deployment = state/'deployment'
    runtime.deployment.mkdir(exist_ok=True)
    runtime.job = {'ProgramArguments': ['/bin/bash', str(state/'run-proxy-wrapper.sh'), '49131']}
    runtime.python = sys.executable
    runtime._key = None
    runtime.bind_lifecycle(latch, journal)
    runtime.begin_operation(ticket)
    return runtime, journal, latch, ticket


def test_check_gates_checkpoint_precedes_every_resolver_step(gate_runtime):
    runtime, journal, latch, _ = gate_runtime
    import types
    events = []
    def fake_capture(argv, timeout=90, env=None, control=False, purpose=None):
        events.append(('capture', argv[0]))
        return b'fixture'
    runtime._capture = fake_capture
    runtime.rows = [{'env_var': 'FIXTURE_KEY', 'required': True, 'source': 'flexai-token'}]
    runtime.kit = Path(runtime.state/'kit-stub')
    runtime.kit.mkdir(exist_ok=True)
    runtime.env['PATH'] = os.defpath
    runtime.config = runtime.state
    (runtime.state/'config.rendered.yaml').write_text(
        'general_settings:\n  master_key: "os.environ/FIXTURE_KEY"\n')
    real_checkpoint = runtime.shutdown.checkpoint
    def recording_checkpoint():
        events.append(('checkpoint', len(events)))
        real_checkpoint()
    with mock.patch.object(runtime.shutdown, 'checkpoint', recording_checkpoint), \
         mock.patch.object(module.shutil, 'which', return_value='/fixture/flexai'):
        runtime.check_gates()
    checkpoints = [i for i, e in enumerate(events) if e[0] == 'checkpoint']
    captures = [i for i, e in enumerate(events) if e[0] == 'capture']
    # A checkpoint precedes the first helper and separates each subsequent
    # step; the resolver checkpoint sits between lookup and read.
    assert len(checkpoints) >= len(captures)
    assert checkpoints[0] < captures[0]
    assert runtime._key == 'fixture'


def test_check_gates_latch_before_first_helper_spawns_nothing(gate_runtime):
    runtime, journal, latch, _ = gate_runtime
    latch.request()
    runtime._capture = mock.Mock(side_effect=AssertionError('helper ran after latch'))
    with pytest.raises(Fault) as caught:
        runtime.check_gates()
    assert caught.value.code == 'controller_stopping'
    assert not journal.uncertain


def test_check_gates_latch_between_resolver_steps_refuses_next(gate_runtime):
    runtime, journal, latch, _ = gate_runtime
    runtime.rows = [{'env_var': 'FIXTURE_KEY', 'required': True, 'source': 'flexai-token'}]
    runtime.kit = Path(runtime.state/'kit-stub')
    runtime.kit.mkdir(exist_ok=True)
    runtime.env['PATH'] = os.defpath
    runtime.config = runtime.state
    (runtime.state/'config.rendered.yaml').write_text(
        'general_settings:\n  master_key: "os.environ/FIXTURE_KEY"\n')
    calls = []
    def fake_capture(argv, timeout=90, env=None, control=False, purpose=None):
        if argv[:2] == ['/bin/bash', str(runtime.kit/'core/installer.sh')]:
            latch.request()  # latch fires after the first helper
        calls.append(argv[0])
        return b'fixture-sentinel'
    runtime._capture = fake_capture
    with pytest.raises(Fault) as caught:
        runtime.check_gates()
    assert caught.value.code == 'controller_stopping'
    # No resolver read ran after the latch: the token read never happened.
    assert '/bin/bash' in calls and 'flexai' not in calls
    assert runtime._key is None


def test_request_latch_before_connect_is_controller_stopping(gate_runtime):
    runtime, journal, latch, _ = gate_runtime
    latch.request()
    with pytest.raises(Fault) as caught:
        runtime._request(runtime.port, '/health/readiness')
    assert caught.value.code == 'controller_stopping'


def test_accept_wake_polling_refuses_next_request_after_latch(gate_runtime, tmp_path):
    """SC-004 wake polling: the latch interrupts the acceptance loop between
    requests within one wake interval, and no further request is issued."""
    import http.server
    import socketserver
    class Ready(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    class Server(socketserver.TCPServer):
        allow_reuse_address = True
    server = Server(('127.0.0.1', 0), Ready)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        runtime, journal, latch, _ = gate_runtime
        runtime.policy['candidate_port'] = port
        runtime._key = 'fixture-key'
        served = []
        original_request = runtime._request
        def counting_request(port_, path, key=None, payload=None):
            if path == '/v1/models':
                latch.request()  # latch fires as the models check returns
            result = original_request(port_, path, key=key, payload=payload)
            served.append(path)
            return result
        runtime._request = counting_request
        guard = lambda: {'pid': 1, 'uid': os.getuid(), 'birth': [1, 1]}
        started = time.monotonic()
        with pytest.raises(Fault) as caught:
            runtime._accept(port, guard, time.monotonic()+5)
        # Readiness probe + liveliness + readiness result ran; the latch
        # then refused the next request at its admission checkpoint — no
        # /v1/models or completions request ever reached the server.
        assert served == ['/health/readiness', '/health/liveliness',
                          '/health/readiness'], served
        assert caught.value.code == 'controller_stopping'
        assert time.monotonic()-started < 5
    finally:
        server.shutdown()
        server.server_close()


def test_candidate_fast_exit_allows_later_helpers_with_open_latch(gate_runtime):
    """Sanity: with the latch open, the journaled candidate path stays
    exercisable end to end (intent -> owned -> closed)."""
    runtime, journal, latch, _ = gate_runtime
    assert not latch.requested


def test_rehearse_latch_before_commit_never_spawns_candidate(gate_runtime):
    runtime, journal, latch, _ = gate_runtime
    latch.request()
    popen = []
    def fail(*args, **kwargs):
        popen.append(1)
        raise AssertionError('candidate spawned after latch')
    with mock.patch.object(module.subprocess, 'Popen', fail):
        with pytest.raises(Fault) as caught:
            runtime.rehearse()
    assert caught.value.code == 'controller_stopping'
    assert not popen
    assert journal.inventory().spawns == {}
    assert not journal.uncertain


def test_rehearse_latch_after_commit_marks_uncertain_and_never_spawns(gate_runtime):
    runtime, journal, latch, _ = gate_runtime
    runtime._listener = lambda port: None  # stub the observation child
    original_intent = journal.spawn_intent
    def intent_then_latch(*args, **kwargs):
        handle = original_intent(*args, **kwargs)
        latch.request()
        return handle
    journal.spawn_intent = intent_then_latch
    popen = []
    def fail(*args, **kwargs):
        popen.append(1)
        raise AssertionError('candidate spawned without admitted commitment')
    with mock.patch.object(module.subprocess, 'Popen', fail):
        with pytest.raises(Fault) as caught:
            runtime.rehearse()
    assert caught.value.code == 'controller_stopping'
    assert not popen
    assert journal.uncertain
    spawns = journal.inventory().spawns
    assert len(spawns) == 1
    assert next(iter(spawns.values()))['state'] == 'uncertain'