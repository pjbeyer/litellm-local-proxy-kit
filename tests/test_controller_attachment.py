"""Offline attachment and admission checks against disposable private state."""
import os
import stat
from pathlib import Path
from unittest import mock

import pytest

from core.restart_control.engine import Controller, Fault, private_dir
from core.restart_control.recovery import RecoveryStore
from core.restart_control.runtime import Runtime
from core.restart_control.shutdown import Shutdown, ShutdownRequested


class Backend:
    label = 'fixture.proxy'
    fixture = False

    def __init__(self, state):
        self.state = state
        self.calls = []

    def bind_lifecycle(self, shutdown, recovery):
        self.shutdown = shutdown
        self.recovery = recovery

    def release_credentials(self):
        self.calls.append('release')

    def inspect(self):
        self.calls.append('inspect')
        raise AssertionError('backend reached')


def test_new_private_state_attaches_one_real_store(tmp_path):
    state = tmp_path/'state'
    shutdown = Shutdown()
    backend = Backend(state)
    controller = Controller(state, backend, shutdown=shutdown)
    assert controller.shutdown is backend.shutdown is shutdown
    assert controller.recovery is backend.recovery
    assert type(controller.recovery) is RecoveryStore
    assert sorted(p.name for p in state.iterdir()) == [
        'approvals', 'controller.lock', 'recovery', 'requests', 'snapshots']
    assert sorted(p.name for p in (state/'recovery').iterdir()) == ['operations', 'spawns']
    reopened = RecoveryStore(state, read_json=controller.recovery._read_json,
        atomic_json=controller.recovery._atomic_json,
        private_path=controller.recovery._private_path,
        private_dir=controller.recovery._private_dir,
        sync_fd=controller.recovery._sync_fd)
    reopened.require_clear()


@pytest.mark.parametrize('name', ['requests', 'recovery'])
def test_existing_partial_state_is_preserved_and_blocks(tmp_path, name):
    state = private_dir(tmp_path/'state')
    private_dir(state/name)
    before = sorted(p.name for p in state.iterdir())
    backend = Backend(state)
    controller = Controller(state, backend, shutdown=Shutdown())
    assert sorted(p.name for p in state.iterdir())[:len(before)] == before
    assert controller.handle({'op': 'status'})['status'] == 'blocked'
    assert controller.handle({'op': 'prepare'}) == {'status': 'blocked', 'code': 'recovery_invalid'}
    assert backend.calls == []
    assert not (state/'recovery'/'operations').exists()


def _inventory(root):
    return {str(path.relative_to(root)): (stat.S_IFMT(path.lstat().st_mode),
            stat.S_IMODE(path.lstat().st_mode),
            os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None)
            for path in (root, *root.rglob('*'))}


@pytest.mark.parametrize('name', ['snapshots', 'controller.lock'])
@pytest.mark.parametrize('damage', ['missing', 'wrong_type', 'wrong_mode', 'symlink'])
def test_existing_controller_view_damage_is_preserved_and_blocks(tmp_path, name, damage):
    state = tmp_path/'state'
    Controller(state, Backend(state), shutdown=Shutdown())
    path = state/name
    if path.is_dir():
        path.rmdir()
    else:
        path.unlink()
    if damage == 'wrong_type':
        path.write_bytes(b'fixture') if name == 'snapshots' else path.mkdir()
    elif damage == 'wrong_mode':
        path.mkdir() if name == 'snapshots' else path.write_bytes(b'fixture')
        path.chmod(0o755 if name == 'snapshots' else 0o644)
    elif damage == 'symlink':
        target = tmp_path/'target'
        target.mkdir() if name == 'snapshots' else target.write_bytes(b'fixture')
        path.symlink_to(target)
    before = _inventory(state)
    backend = Backend(state)
    controller = Controller(state, backend, shutdown=Shutdown())
    assert controller.handle({'op': 'status'}) == {'status': 'blocked', 'code': 'recovery_invalid'}
    assert controller.handle({'op': 'prepare'}) == {'status': 'blocked', 'code': 'recovery_invalid'}
    assert _inventory(state) == before
    assert not hasattr(backend, 'recovery')
    assert backend.calls == []


def test_recovery_gate_runs_inside_lock_before_backend_or_request_write(tmp_path):
    state = tmp_path/'state'
    backend = Backend(state)
    controller = Controller(state, backend, shutdown=Shutdown())
    (state/'recovery'/'operations'/'broken.json').write_text('{}')
    result = controller.handle({'op': 'prepare'})
    assert result == {'status': 'blocked', 'code': 'recovery_invalid'}
    assert backend.calls == []
    assert list((state/'requests').iterdir()) == []
    assert controller.handle({'op': 'status'}) == {'status': 'blocked', 'code': 'recovery_invalid'}
    assert backend.calls == []


@pytest.mark.parametrize('payload', [{'op': 'prepare'},
    {'op': 'restart', 'request_id': 'a'*32, 'asset_digest': 'b'*64}])
def test_recovery_gate_observes_mutex_ownership(tmp_path, payload):
    state = tmp_path/'state'
    backend = Backend(state)
    controller = Controller(state, backend, shutdown=Shutdown())
    observed = []
    original = controller.recovery.require_clear
    def require_clear(*args, **kwargs):
        observed.append(controller._mutex.locked())
        return original(*args, **kwargs)
    with mock.patch.object(controller.recovery, 'require_clear', side_effect=require_clear):
        with mock.patch.object(controller, '_prepare', return_value={'status': 'prepared'}), \
                mock.patch.object(controller, '_restart', return_value={'status': 'restarted'}):
            assert controller.handle(payload)['status'] in {'prepared', 'restarted'}
    assert observed == [True]
    assert backend.calls == ['release']
    assert list((state/'requests').iterdir()) == []


def test_shutdown_at_lock_acquisition_blocks_before_backend(tmp_path):
    state = tmp_path/'state'
    shutdown = Shutdown()
    backend = Backend(state)
    controller = Controller(state, backend, shutdown=shutdown)
    original = controller._locked
    from contextlib import contextmanager
    @contextmanager
    def latch_at_lock():
        with original():
            shutdown.request()
            yield
    controller._locked = latch_at_lock
    assert controller.handle({'op': 'prepare'}) == {'status': 'blocked', 'code': 'controller_stopping'}
    assert backend.calls == []
    assert list((state/'requests').iterdir()) == []


@pytest.mark.parametrize('error', [ValueError('checkpoint failed'), RuntimeError('checkpoint failed')])
@pytest.mark.parametrize('payload', [{'op': 'prepare'},
    {'op': 'restart', 'request_id': 'a'*32, 'asset_digest': 'b'*64}])
def test_unrelated_checkpoint_failure_is_internal_error_before_writes(tmp_path, error, payload):
    state = tmp_path/'state'
    shutdown = Shutdown()
    backend = Backend(state)
    controller = Controller(state, backend, shutdown=shutdown)
    with mock.patch.object(shutdown, 'checkpoint', side_effect=error):
        assert controller.handle(payload) == {'status': 'blocked', 'code': 'internal_error'}
    assert backend.calls == []
    assert list((state/'requests').iterdir()) == []


def test_unattached_runtime_cannot_spawn_or_rehearse(tmp_path):
    runtime = object.__new__(Runtime)
    runtime.state = private_dir(tmp_path/'state')
    runtime._candidate = None
    runtime._cleanup_blocked = False
    runtime._operation_started = None
    runtime.env = {}
    runtime.config = tmp_path
    runtime.policy = {'candidate_port': 49132}
    runtime.job = {'ProgramArguments': ['/bin/sh', '/dev/null', '49131']}
    with mock.patch('core.restart_control.runtime.subprocess.Popen', side_effect=AssertionError('spawn attempted')) as popen:
        with pytest.raises(Fault) as caught:
            runtime._capture(['/usr/bin/true'])
        assert caught.value.code == 'recovery_invalid'
        with pytest.raises(Fault) as caught:
            runtime.rehearse()
        assert caught.value.code == 'recovery_invalid'
        popen.assert_not_called()


def test_runtime_binding_rejects_wrong_latch_root_and_replacement(tmp_path):
    state = tmp_path/'state'
    backend = Backend(state)
    shutdown = Shutdown()
    controller = Controller(state, backend, shutdown=shutdown)
    runtime = object.__new__(Runtime)
    runtime.state = state
    runtime._expected_shutdown = shutdown
    runtime._key = None
    runtime._operation_started = None
    runtime.bind_lifecycle(shutdown, controller.recovery)
    with pytest.raises(Fault):
        runtime.bind_lifecycle(shutdown, controller.recovery)
    other = object.__new__(Runtime)
    other.state = state
    other._expected_shutdown = shutdown
    with pytest.raises(Fault):
        other.bind_lifecycle(Shutdown(), mock.Mock())
    other.state = tmp_path/'other'
    with pytest.raises(Fault):
        other.bind_lifecycle(shutdown, controller.recovery)


def test_runtime_first_bind_rejects_other_valid_latch(tmp_path):
    state = tmp_path/'state'
    expected = Shutdown()
    controller = Controller(state, Backend(state), shutdown=expected)
    runtime = object.__new__(Runtime)
    runtime.state = state
    runtime._expected_shutdown = expected
    with pytest.raises(Fault) as caught:
        runtime.bind_lifecycle(Shutdown(), controller.recovery)
    assert caught.value.code == 'recovery_invalid'
    runtime.bind_lifecycle(expected, controller.recovery)
    assert runtime.shutdown is expected


def test_runtime_receives_latch_before_policy_read(tmp_path):
    shutdown = Shutdown()
    seen = {}
    class ProbeRuntime(Runtime):
        def __new__(cls, *args, **kwargs):
            instance = object.__new__(cls)
            seen['instance'] = instance
            return instance
    def read_policy(_):
        assert seen['instance'].shutdown is shutdown
        raise Fault('runtime_invalid')
    with mock.patch('core.restart_control.runtime._read_json', side_effect=read_policy):
        with pytest.raises(Fault):
            ProbeRuntime(tmp_path/'unused.json', shutdown=shutdown)


def test_runtime_prelatched_constructor_reads_no_policy(tmp_path):
    shutdown = Shutdown()
    shutdown.request()
    with mock.patch('core.restart_control.runtime._read_json') as read_policy:
        with pytest.raises(ShutdownRequested):
            Runtime(tmp_path/'unused.json', shutdown=shutdown)
    read_policy.assert_not_called()


def test_shutdown_during_runtime_policy_read_stops_before_later_startup(tmp_path):
    shutdown = Shutdown()
    def read_policy(_):
        shutdown.request()
        return {}
    with mock.patch('core.restart_control.runtime._read_json', side_effect=read_policy), \
            mock.patch('core.restart_control.runtime.subprocess.Popen', side_effect=AssertionError('spawn')) as popen:
        with pytest.raises(ShutdownRequested):
            Runtime(tmp_path/'unused.json', shutdown=shutdown)
    popen.assert_not_called()


def test_runtime_releases_context_but_leaves_uncertain_record(tmp_path):
    state = tmp_path/'state'
    controller = Controller(state, Backend(state), shutdown=Shutdown())
    store = controller.recovery
    binding = {'request_id': 'a'*32, 'kind': 'proxy', 'controller_epoch': 'b'*32,
               'policy_digest': 'c'*64, 'controller_digest': 'd'*64,
               'controller_plist_digest': 'e'*64, 'asset_digest': None,
               'runtime_digest': None}
    ticket = store.begin(binding, 'production')
    runtime = object.__new__(Runtime)
    runtime.state = state
    runtime._expected_shutdown = controller.shutdown
    runtime._key = 'ephemeral-fixture'
    runtime._operation_started = 1.0
    runtime.bind_lifecycle(controller.shutdown, store)
    runtime.begin_operation(ticket)
    runtime.release_credentials()
    assert runtime._key is None
    assert runtime._operation_started is None
    assert runtime._active_operation is None
    assert (state/'recovery'/'operations'/('a'*32+'.json')).exists()
    with pytest.raises(Exception):
        store.require_clear()
    assert controller.handle({'op': 'prepare'}) == {'status': 'blocked', 'code': 'recovery_invalid'}


def test_bound_runtime_rejects_null_operation_before_recovery_admission(tmp_path):
    state = tmp_path/'state'
    shutdown = Shutdown()
    controller = Controller(state, Backend(state), shutdown=shutdown)
    store = controller.recovery
    store.require_clear()
    runtime = object.__new__(Runtime)
    runtime.state = state
    runtime._expected_shutdown = shutdown
    runtime.bind_lifecycle(shutdown, store)
    with mock.patch.object(store, 'require_clear', wraps=store.require_clear) as require_clear:
        with pytest.raises(Fault) as caught:
            runtime.begin_operation(None)
    assert caught.value.code == 'recovery_invalid'
    assert runtime._active_operation is None
    require_clear.assert_not_called()


def test_cli_registers_handlers_before_policy_and_shares_latch(tmp_path, monkeypatch):
    from core import restart_controller as entry
    from restart_control import runtime as cli_runtime
    runtime_type = cli_runtime.Runtime
    handlers = {}
    seen = {}
    def register(sig, handler):
        handlers[sig] = handler
    def read_policy(_):
        assert len(handlers) == 2
        seen['latch'] = handlers[next(iter(handlers))].__closure__[0].cell_contents
        return {'state_dir': str(tmp_path)}
    def runtime(_, *, shutdown):
        assert len(handlers) == 2
        assert shutdown is seen['latch']
        instance = object.__new__(runtime_type)
        instance.state = tmp_path/'state'
        instance.label = 'fixture.proxy'
        instance._expected_shutdown = shutdown
        seen['runtime'] = instance
        return instance
    def serve(_, controller, view):
        assert view.shutdown is controller.shutdown is seen['runtime'].shutdown
        assert controller.recovery is seen['runtime'].recovery
        assert view.is_set() is False
        handlers[next(iter(handlers))]()
        assert view.is_set() is True
    monkeypatch.setattr(entry.signal, 'signal', register)
    monkeypatch.setattr(entry, '_read_json', read_policy)
    monkeypatch.setattr(cli_runtime, 'Runtime', runtime)
    monkeypatch.setattr(entry, 'serve', serve)
    assert entry.main(['--policy', str(tmp_path/'unused.json'), 'serve']) == 0
    assert seen['latch'] is seen['runtime'].shutdown


def test_cli_shutdown_during_policy_read_prevents_runtime_start(tmp_path, monkeypatch, capsys):
    from core import restart_controller as entry
    from restart_control import runtime as cli_runtime
    handlers = {}
    monkeypatch.setattr(entry.signal, 'signal', lambda sig, handler: handlers.setdefault(sig, handler))
    def read_policy(_):
        handlers[next(iter(handlers))]()
        return {'state_dir': str(tmp_path)}
    monkeypatch.setattr(entry, '_read_json', read_policy)
    monkeypatch.setattr(cli_runtime, 'Runtime', lambda _, **__: pytest.fail('runtime started'))
    assert entry.main(['--policy', str(tmp_path/'unused.json'), 'serve']) == 78
    assert 'controller_stopping' in capsys.readouterr().out


def test_invalid_recovery_blocks_restart_before_request_lookup(tmp_path):
    state = tmp_path/'state'
    backend = Backend(state)
    controller = Controller(state, backend, shutdown=Shutdown())
    (state/'recovery'/'operations'/'broken.json').write_text('{}')
    result = controller.handle({'op': 'restart', 'request_id': 'a'*32,
                                'asset_digest': 'b'*64})
    assert result == {'status': 'blocked', 'code': 'recovery_invalid'}
    assert backend.calls == []
    assert list((state/'requests').iterdir()) == []


def test_constructor_test_injection_rejects_wrong_types(tmp_path):
    from restart_lifecycle_adapter import FixtureLifecycleAdapter
    state = tmp_path/'state'
    backend = Backend(state)
    with pytest.raises(Fault) as caught:
        Controller(state, backend, shutdown=object())
    assert caught.value.code == 'invalid_request'
    with pytest.raises(Fault) as caught:
        Controller(state, backend, shutdown=Shutdown(), test_lifecycle=object())
    assert caught.value.code == 'invalid_request'
    with pytest.raises(Fault) as caught:
        Runtime(tmp_path/'unused.json', shutdown=Shutdown(), test_lifecycle=object())
    assert caught.value.code == 'invalid_request'
    with pytest.raises(Fault) as caught:
        Runtime(tmp_path/'unused.json', shutdown=Shutdown(),
            test_lifecycle=FixtureLifecycleAdapter(namespace='core'))
    assert caught.value.code == 'invalid_request'


def test_shared_fixture_uses_each_controller_namespace_latch(tmp_path):
    from restart_lifecycle_adapter import FixtureLifecycleAdapter
    from restart_control.engine import Controller as DirectController
    from restart_control.shutdown import Shutdown as DirectShutdown
    for namespace, controller_type, shutdown_type in (
            ('core', Controller, Shutdown),
            ('restart_control', DirectController, DirectShutdown)):
        state = tmp_path/namespace
        backend = Backend(state)
        backend.fixture = True
        controller = controller_type(state, backend,
            test_lifecycle=FixtureLifecycleAdapter(namespace=namespace))
        assert type(controller.shutdown) is shutdown_type
        assert controller.recovery.backend is backend
