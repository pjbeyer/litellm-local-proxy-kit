"""Explicit legacy fixture admission pending per-phase journal integration."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'core'))
from core.restart_control.engine import FixtureLifecyclePort as CorePort
from core.restart_control.shutdown import Shutdown as CoreShutdown
from restart_control.engine import FixtureLifecyclePort as DirectPort
from restart_control.shutdown import Shutdown as DirectShutdown


class _RecordHandle:
    """Test-only record locator; grants no journal authority."""
    __slots__ = ('spawn_id', 'purpose', '_adapter')

    def __init__(self, adapter, spawn_id, purpose):
        self.spawn_id = spawn_id
        self.purpose = purpose
        self._adapter = adapter


class FixtureLifecycleAdapter(CorePort, DirectPort):
    def __init__(self, *, namespace='restart_control'):
        shutdown_type = {'core': CoreShutdown, 'restart_control': DirectShutdown}.get(namespace)
        if shutdown_type is None:
            raise ValueError('fixture_namespace_invalid')
        self.shutdown = shutdown_type()
        self.admissions = 0
        self.spawns = {}
        self.uncertain = False
        self.transitions = []

    def require_clear(self, active=None):
        self.admissions += 1

    def bind_backend(self, backend):
        self.backend = backend

    def end_operation(self, active):
        self.admissions += 1

    def spawn_intent(self, ticket, spawn_id, purpose):
        if self.uncertain or spawn_id in self.spawns:
            raise RuntimeError('fixture_store_invalid')
        handle = _RecordHandle(self, spawn_id, purpose)
        self.spawns[spawn_id] = {'state': 'intent', 'identity': None, 'purpose': purpose}
        self.transitions.append('intent')
        return handle

    def spawn_owned(self, handle, identity):
        if not isinstance(handle, _RecordHandle) or handle._adapter is not self:
            raise RuntimeError('fixture_store_invalid')
        if self.uncertain or handle.spawn_id not in self.spawns:
            raise RuntimeError('fixture_store_invalid')
        row = self.spawns[handle.spawn_id]
        if row['state'] != 'intent':
            raise RuntimeError('fixture_store_invalid')
        if set(identity) != {'pid', 'uid', 'pgid', 'birth'}:
            raise RuntimeError('fixture_store_invalid')
        row['state'] = 'owned'
        row['identity'] = dict(identity)
        self.transitions.append('owned')

    def spawn_closed(self, handle, identity, *, cleanup_proven):
        if not isinstance(handle, _RecordHandle) or handle._adapter is not self:
            raise RuntimeError('fixture_store_invalid')
        if not cleanup_proven or self.uncertain or handle.spawn_id not in self.spawns:
            raise RuntimeError('fixture_store_invalid')
        row = self.spawns[handle.spawn_id]
        if row['state'] != 'owned' or row['identity'] != identity:
            raise RuntimeError('fixture_store_invalid')
        row['state'] = 'closed'
        self.transitions.append('closed')

    def mark_uncertain(self, handle):
        if not isinstance(handle, _RecordHandle) or handle._adapter is not self:
            raise RuntimeError('fixture_store_invalid')
        if handle.spawn_id not in self.spawns:
            raise RuntimeError('fixture_store_invalid')
        self.uncertain = True
        row = self.spawns[handle.spawn_id]
        if row['state'] != 'uncertain':
            row['state'] = 'uncertain'
        self.transitions.append('uncertain')
