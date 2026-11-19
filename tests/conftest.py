"""Cross-platform test fixtures for the restart-controller offline suite.

The restart controller is a *darwin-only* subsystem by contract: its canary
arm/consume fence reads the kernel bootsessionuuid (sysctl
kern.bootsessionuuid), its owned-child stop proves identity through the Apple
proc_bsdinfo ABI, and its group census reads proc_listpids. None of these
exist off darwin.

On non-darwin runners, the kernel primitives are stubbed to stable fakes so
the IN-PROCESS fencing/census logic keeps REAL coverage on Linux/CI instead
of being skipped.

IMPORTANT (module identity): the test files reach controller sources two ways,
and with no ``core/__init__.py`` those are DIFFERENT module objects:
  - ``sys.path.insert(0, core)`` then ``import restart_control...``  (canary,
    canary_negatives, startup, ...)
  - ``from core.restart_control import ...``                          (recovery,
    interruption, lifecycle_fixtures, ...)
This conftest patches BOTH namespaces so a stub always lands on the instance
each file actually exercises.

test_controller_group_identity.py is exempt: its boot-ABI tests mock ctypes
themselves (platform-agnostic validation) against the real kernel_boot_identity.

Tests that launch a fresh interpreter subprocess to exercise the primitives
themselves (canary-worker, crash_driver, real-transport fixtures, installer/
wrapper/proof-worker helper subprocesses) cannot inherit the stub and are
marked DARWIN_ONLY (skip) in their test files.

Set RC_FORCE_NON_DARWIN=1 to simulate the non-darwin path on a darwin host.
"""

import ctypes
import os
import re
import sys
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_CORE = str(_REPO / "core")

if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

_FAKE_BOOT = "c0ffeec0-ffee-c0ff-eec0-ffeec0ffeec0"


def real_mac_available() -> bool:
    """True when the host exposes the darwin kernel bootsessionuuid (a physical
    Mac or a VM that preserves it). GitHub-hosted macOS VMs do not, so the
    darwin-only subprocess tests cannot pass there.
    """
    if sys.platform != "darwin":
        return False
    try:
        call = ctypes.CDLL(None, use_errno=True).sysctlbyname
        call.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
                         ctypes.c_size_t]
        call.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(37)
        size = ctypes.c_size_t(37)
        ctypes.set_errno(0)
        result = call(b"kern.bootsessionuuid", buffer, ctypes.byref(size),
                      None, 0)
        if (type(result) is not int or result != 0 or ctypes.get_errno()
                or size.value != 37 or buffer.raw[36] != 0
                or b"\0" in buffer.raw[:36]):
            return False
        value = buffer.raw[:36].decode("ascii")
        return bool(re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-"
                                 r"[0-9a-fA-F]{12}", value)
                and int(value.replace("-", ""), 16) != 0)
    except Exception:
        return False


REQUIRES_REAL_MAC = pytest.mark.skipif(
    # An explicit CI declaration is the deterministic gate: GitHub-hosted
    # macOS runners are VMs whose subprocess children cannot satisfy the
    # fixture handshake (readiness select() never fires). The bootsessionuuid
    # probe alone is NOT a discriminator — GH macOS VMs report one — so
    # CI=true on darwin forces the skip and a real Mac (no CI env) runs
    # everything. RC_FORCE_REAL_MAC=1 re-enables locally for CI simulation.
    (os.environ.get("CI") == "true" and not os.environ.get("RC_FORCE_REAL_MAC"))
    or (sys.platform == "darwin" and not real_mac_available()),
    reason="darwin subprocess tests require a physical Mac (CI VMs cannot "
           "run them)")


def _boot():
    return _FAKE_BOOT


def _process_identity(pid):
    return {"pid": int(pid), "uid": os.getuid(), "ppid": 1,
            "pgid": os.getpgid(pid), "birth": [1, 0]}


def _group_identities(pgid):
    """Return a stable one-member group for the caller's pid."""
    me = _process_identity(os.getpid())
    if me["pgid"] != pgid:
        return ()
    return (me,)


def _non_darwin() -> bool:
    return sys.platform != "darwin" or os.environ.get("RC_FORCE_NON_DARWIN") == "1"


def _in_group_identity_abi_test(node) -> bool:
    module = getattr(node, "module", None)
    if module is None:
        return False
    return getattr(module, "__name__", "") == "test_controller_group_identity"


def _patch(monkeypatch, rt):  # rt = a runtime_identity module object
    monkeypatch.setattr(rt, "kernel_boot_identity", _boot)
    monkeypatch.setattr(rt, "kernel_process_identity", _process_identity)


def _patch_process_identity(monkeypatch, pi):  # pi = a process_identity module object
    monkeypatch.setattr(pi, "boot_identity", _boot)
    monkeypatch.setattr(pi, "kernel_process_identity", _process_identity)
    monkeypatch.setattr(pi, "kernel_boot_identity", _boot)
    monkeypatch.setattr(pi, "process_identity", _process_identity)
    monkeypatch.setattr(pi, "process_group_identities", _group_identities)


@pytest.fixture(autouse=True)
def _stub_kernel_primitives(monkeypatch, request):
    if not _non_darwin() or _in_group_identity_abi_test(request.node):
        yield
        return

    # top-level restart_control.* (tests that insert core/ on sys.path)
    import restart_control.runtime_identity as rt_top
    import restart_control.process_identity as pi_top
    import restart_control.runtime as runtime_top  # binds process_identity at import
    _patch(monkeypatch, rt_top)
    _patch_process_identity(monkeypatch, pi_top)
    # runtime bound the STUB at import (this fixture imports runtime too).
    # Ensure any runtime module instance uses the stub functions:
    monkeypatch.setattr(runtime_top, "process_identity", _process_identity)
    monkeypatch.setattr(runtime_top, "process_group_identities", _group_identities)

    # core.restart_control.* (tests that import from core)
    for mod in ("runtime_identity", "process_identity", "runtime"):
        try:
            core_mod = __import__(
                f"core.restart_control.{mod}", fromlist=["*"])
            if mod == "runtime_identity":
                _patch(monkeypatch, core_mod)
            elif mod == "process_identity":
                _patch_process_identity(monkeypatch, core_mod)
            else:
                monkeypatch.setattr(core_mod, "process_identity", _process_identity)
                monkeypatch.setattr(
                    core_mod, "process_group_identities", _group_identities)
        except ImportError:
            pass
    yield
