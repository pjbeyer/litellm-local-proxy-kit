"""Cross-platform test fixtures for the restart-controller offline suite.

The restart controller is a *darwin-only* subsystem by contract: its canary
arm/consume fence reads the kernel bootsessionuuid (sysctl
kern.bootsessionuuid) and its owned-child stop proves identity through the
Apple proc_bsdinfo ABI. Neither primitive exists off darwin.

On non-darwin runners:
  - The two kernel primitives are stubbed to stable fakes so the IN-PROCESS
    fencing logic (arm records the stub value, consume compares, owned-stop
    identity checks) keeps real coverage on Linux/CI instead of being
    skipped.
  - test_controller_group_identity.py is exempt: its boot-ABI tests mock the
    ctypes library themselves (platform-agnostic validation), so they run
    against the real kernel_boot_identity without our stub.
  - Tests that launch a fresh interpreter subprocess to exercise the
    primitives themselves (canary-worker, crash_driver, real-transport
    fixtures) cannot inherit the stub and are marked DARWIN_ONLY (skip) in
    their test files.

Set RC_FORCE_NON_DARWIN=1 to simulate the non-darwin path on a darwin host
(for local cross-platform verification).
"""

import os
import sys

import pytest

_FAKE_BOOT = "c0ffeec0-ffee-c0ff-eec0-ffeec0ffeec0"


def _boot():  # stable fake bootsessionuuid
    return _FAKE_BOOT


def _process_identity(pid):  # stable fake process identity
    return {"pid": int(pid), "uid": os.getuid(), "ppid": 1,
            "pgid": os.getpgid(pid), "birth": [1, 0]}


def _non_darwin() -> bool:
    return sys.platform != "darwin" or os.environ.get("RC_FORCE_NON_DARWIN") == "1"


def _in_group_identity_abi_test(node) -> bool:
    """True for the boot-ABI unit tests, which mock ctypes themselves."""
    module = getattr(node, "module", None)
    if module is None:
        return False
    return getattr(module, "__name__", "") == "test_controller_group_identity"


@pytest.fixture(autouse=True)
def _stub_kernel_primitives(monkeypatch, request):
    """Stub the darwin-only kernel primitives when running off darwin."""
    if not _non_darwin() or _in_group_identity_abi_test(request.node):
        yield
        return

    from core.restart_control import runtime_identity
    from core.restart_control import process_identity as _pi

    # runtime_identity module (canary.py / startup.py read these function-local)
    monkeypatch.setattr(runtime_identity, "kernel_boot_identity", _boot)
    monkeypatch.setattr(runtime_identity, "kernel_process_identity", _process_identity)
    # process_identity re-export aliases (runtime.py binds process_identity at import)
    monkeypatch.setattr(_pi, "boot_identity", _boot)
    monkeypatch.setattr(_pi, "kernel_process_identity", _process_identity)
    monkeypatch.setattr(_pi, "process_identity", _process_identity)
    yield
