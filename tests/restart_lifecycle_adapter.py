"""Explicit legacy fixture admission pending per-phase journal integration."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'core'))
from core.restart_control.engine import FixtureLifecyclePort as CorePort
from core.restart_control.shutdown import Shutdown as CoreShutdown
from restart_control.engine import FixtureLifecyclePort as DirectPort
from restart_control.shutdown import Shutdown as DirectShutdown


class FixtureLifecycleAdapter(CorePort, DirectPort):
    def __init__(self, *, namespace='restart_control'):
        shutdown_type = {'core': CoreShutdown, 'restart_control': DirectShutdown}.get(namespace)
        if shutdown_type is None:
            raise ValueError('fixture_namespace_invalid')
        self.shutdown = shutdown_type()
        self.admissions = 0

    def require_clear(self, active=None):
        self.admissions += 1

    def bind_backend(self, backend):
        self.backend = backend

    def end_operation(self, active):
        self.admissions += 1
