"""Explicit legacy fixture admission pending per-phase journal integration."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'core'))
from core.restart_control.engine import FixtureLifecyclePort as CorePort
from core.restart_control.shutdown import Shutdown as CoreShutdown
from restart_control.engine import FixtureLifecyclePort as DirectPort
from restart_control.shutdown import Shutdown as DirectShutdown


class _OperationTicket:
    """Test-only operation locator; grants no journal authority."""
    __slots__ = ('request_id', 'binding')

    def __init__(self, request_id, binding):
        self.request_id = request_id
        self.binding = binding


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
        self.active = None
        self.operations = {}
        self.fence = None

    def require_clear(self, active=None):
        self.admissions += 1
        if self.uncertain:
            raise RuntimeError('fixture_store_invalid')
        if self.fence is not None and (active is None or active is not self.active):
            raise RuntimeError('fixture_store_invalid')

    def bind_backend(self, backend):
        self.backend = backend

    def end_operation(self, active):
        self.admissions += 1
        if active is not self.active:
            raise RuntimeError('fixture_store_invalid')
        self.active = None

    # -- T005/T006 operation/fence surface (engine drives these in T012) --
    def begin(self, binding, scope):
        canary = binding.get('kind') == 'platform-canary'
        if (self.uncertain or self.active is not None
                or binding.get('kind') not in ('proxy', 'platform-canary')
                or binding.get('asset_digest') is not None
                or binding.get('runtime_digest') is not None
                or not isinstance(binding.get('request_id'), str)
                or binding['request_id'] in self.operations):
            raise RuntimeError('fixture_store_invalid')
        for key in ('controller_epoch', 'policy_digest', 'controller_digest',
                    'controller_plist_digest'):
            value = binding.get(key)
            if not isinstance(value, str) or len(value) != (32 if key == 'controller_epoch' else 64):
                raise RuntimeError('fixture_store_invalid')
            if any(c not in '0123456789abcdef' for c in value):
                raise RuntimeError('fixture_store_invalid')
        ticket = _OperationTicket(binding['request_id'],
                                  tuple(sorted(binding.items())))
        self.operations[binding['request_id']] = {
            'binding': dict(binding), 'scope': scope,
            'phase': 'canary_pending' if canary else 'preparing',
            'outcome': None, 'primary_control_committed': False}
        self.active = ticket
        self.transitions.append('begin')
        return ticket

    # -- T019 canary lifecycle surface (startup orchestration drives it) --
    def canary_phase(self, ticket, phase):
        row = self._active_row(ticket)
        if (row['binding'].get('kind') != 'platform-canary'
                or phase != 'canary_running'
                or row['phase'] != 'canary_pending'
                or row['outcome'] is not None):
            raise RuntimeError('fixture_store_invalid')
        row['phase'] = phase
        self.transitions.append('canary_running')
        return ticket

    def canary_terminal(self, ticket, outcome):
        row = self._active_row(ticket)
        if (row['binding'].get('kind') != 'platform-canary'
                or outcome not in ('canary_passed', 'canary_blocked')
                or row['phase'] not in ('canary_pending', 'canary_running')
                or row['outcome'] is not None):
            raise RuntimeError('fixture_store_invalid')
        for child in self.spawns.values():
            if (child['binding'].get('request_id') == ticket.request_id
                    and child['state'] != 'closed'):
                raise RuntimeError('fixture_store_invalid')
        row['phase'], row['outcome'] = outcome, outcome
        self.active = None
        self.transitions.append(outcome)
        return ticket

    def bind_frozen(self, ticket, asset_digest, runtime_digest):
        row = self._active_row(ticket)
        if (row['phase'] != 'preparing' or row['outcome'] is not None
                or row['binding']['asset_digest'] is not None
                or not isinstance(asset_digest, str) or len(asset_digest) != 64
                or not isinstance(runtime_digest, str) or len(runtime_digest) != 64):
            raise RuntimeError('fixture_store_invalid')
        for child in self.spawns.values():
            if child['binding'].get('request_id') == ticket.request_id:
                if child['state'] != 'closed':
                    raise RuntimeError('fixture_store_invalid')
                child['binding'] = {**child['binding'],
                                    'asset_digest': asset_digest,
                                    'runtime_digest': runtime_digest}
        row['binding'] = {**row['binding'], 'asset_digest': asset_digest,
                          'runtime_digest': runtime_digest}
        self.transitions.append('frozen')
        refreshed = _OperationTicket(ticket.request_id,
                                     tuple(sorted(row['binding'].items())))
        self.active = refreshed
        return refreshed

    def operation_ticket(self, request_id):
        row = self.operations.get(request_id)
        if row is None or self.active is not None:
            raise RuntimeError('fixture_store_invalid')
        return _OperationTicket(request_id,
                                tuple(sorted(row['binding'].items())))

    def reopen_restart(self, ticket):
        row = self._row(ticket)
        if (self.active is not None and ticket is not self.active
                or row['phase'] != 'prepared' or row['outcome'] != 'prepared'
                or row['primary_control_committed'] is not False
                or row['binding']['asset_digest'] is None
                or self.fence is not None):
            raise RuntimeError('fixture_store_invalid')
        if not self._approval_on_disk(ticket.request_id, consumed=False):
            raise RuntimeError('fixture_store_invalid')
        row['phase'], row['outcome'] = 'restart_pending', None
        self.active = _OperationTicket(ticket.request_id,
                                        tuple(sorted(row['binding'].items())))
        self.transitions.append('reopen')
        return self.active

    def arm_mutation(self, ticket):
        row = self._active_row(ticket)
        if (row['phase'] != 'restart_pending' or row['outcome'] is not None
                or self.fence is not None):
            raise RuntimeError('fixture_store_invalid')
        self.fence = {'binding': dict(row['binding']), 'phase': 'restart_pending',
                      'state': 'armed'}
        self.transitions.append('armed')
        return ticket

    def mark_mutation_uncertain(self, ticket):
        if ticket is not self.active:
            raise RuntimeError('fixture_store_invalid')
        self.uncertain = True
        if self.fence is not None:
            self.fence['state'] = 'uncertain'

    _PHASE_EDGES = {
        'restart_pending': ('intent_consumed',),
        'intent_consumed': ('stopping',),
        'stopping': ('starting', 'rollback_stopping'),
        'starting': ('postchecking', 'rollback_stopping'),
        'postchecking': ('rollback_stopping',),
        'rollback_stopping': ('rollback_restoring',),
        'rollback_restoring': ('rollback_starting',),
        'rollback_starting': ('rollback_postchecking',),
        'rollback_postchecking': (),
    }

    def phase(self, ticket, phase, *, committed_action=None):
        row = self._active_row(ticket)
        if (self.fence is None or self.fence['binding'] != row['binding']
                or self.fence['state'] != 'armed'):
            raise RuntimeError('fixture_store_invalid')
        if phase == 'manual_recovery_required':
            # Fixture scope closes the failed operation and releases the
            # fence: there is no operator repair API in the legacy rig, and
            # a fresh prepare must stay admissible. The production store
            # keeps the uncertain fence until manual reconciliation.
            row['phase'] = row['outcome'] = phase
            self.fence = None
            self.active = None
            self.transitions.append('manual')
            return ticket
        if phase not in self._PHASE_EDGES[row['phase']]:
            raise RuntimeError('fixture_store_invalid')
        if committed_action is not None:
            if (phase != 'stopping' or row['primary_control_committed']
                    or not isinstance(committed_action, tuple)
                    or len(committed_action) != 2
                    or committed_action[0] is not self.shutdown):
                raise RuntimeError('fixture_store_invalid')
            purpose = ('service-stop' if phase in ('stopping', 'rollback_stopping')
                       else 'service-start' if phase in ('starting', 'rollback_starting')
                       else None)
            if purpose is None or getattr(committed_action[1], 'purpose', None) != purpose:
                raise RuntimeError('fixture_store_invalid')
            if committed_action[0].claim(committed_action[1]) != purpose:
                raise RuntimeError('fixture_store_invalid')
            row['primary_control_committed'] = True
        row['phase'] = phase
        self.fence['phase'] = phase
        self.transitions.append('phase:' + phase)
        return ticket

    def finish(self, ticket):
        row = self._active_row(ticket)
        if row['outcome'] is not None:
            raise RuntimeError('fixture_store_invalid')
        if row['phase'] == 'preparing':
            # The fixture cannot see the engine's request record; the engine
            # validates that pairing itself before calling finish.
            row['phase'] = row['outcome'] = 'prepared'
        elif row['phase'] in ('postchecking', 'rollback_postchecking'):
            # Accepted terminal: the request record is the authority, the
            # journal index follows it exactly like the real store.
            plan = json.loads((self.backend.approval_root.parent / 'requests'
                               / (ticket.request_id + '.json')).read_text())
            if plan.get('phase') not in ('restarted', 'rolled_back'):
                raise RuntimeError('fixture_store_invalid')
            row['phase'] = plan['phase']
            row['outcome'] = plan['phase']
            self.fence = None
        else:
            raise RuntimeError('fixture_store_invalid')
        self.active = None
        self.transitions.append('finish')
        return ticket

    # -- internals -------------------------------------------------------
    def _row(self, ticket):
        if not isinstance(ticket, _OperationTicket):
            raise RuntimeError('fixture_store_invalid')
        row = self.operations.get(ticket.request_id)
        if row is None or tuple(sorted(row['binding'].items())) != ticket.binding:
            raise RuntimeError('fixture_store_invalid')
        return row

    def _active_row(self, ticket):
        row = self._row(ticket)
        if ticket is not self.active:
            raise RuntimeError('fixture_store_invalid')
        return row

    def _approval_on_disk(self, request_id, *, consumed):
        # The approval check against real fixture private state lives with the
        # rig: the engine writes approvals under the shared state root.
        approvals = self.backend.approval_root
        path = approvals / (request_id + ('.consumed' if consumed else '.json'))
        return path.is_file()

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
