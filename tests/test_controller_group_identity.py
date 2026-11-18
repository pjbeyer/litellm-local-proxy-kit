"""Bounded Darwin identity observations; no process control in the subject."""
import ctypes
import errno
import os
import re
import select
import subprocess
import sys
import time

import pytest

from core.restart_control import process_identity as identity


@pytest.fixture(autouse=True)
def mock_only_native_fence(request, monkeypatch):
    if request.node.name in {
        'test_retained_disposable_group_has_full_kernel_identities',
        'test_real_kernel_boot_identity_has_fixed_uuid_shape',
    }:
        return

    def blocked(*args, **kwargs):
        raise AssertionError('unmocked native or process call')

    monkeypatch.setattr(ctypes.util, 'find_library', blocked)
    monkeypatch.setattr(ctypes, 'CDLL', blocked)
    monkeypatch.setattr(subprocess, 'Popen', blocked)


def _identity(pid, pgid=71, uid=501, birth=1):
    return {'pid': pid, 'uid': uid, 'ppid': 1, 'pgid': pgid,
            'birth': [birth, 2]}


class GroupABI:
    def __init__(self, scans):
        self.scans = iter(scans)
        self.sizes = []
        self.argtypes = self.restype = None

    def __call__(self, selector, pgid, buffer, size):
        assert selector == 2
        assert pgid == 71
        self.sizes.append(size)
        pids, returned, error = next(self.scans)
        ctypes.set_errno(error)
        slots = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int32))
        for index, pid in enumerate(pids[:size // 4]):
            slots[index] = pid
        return len(pids) * 4 if returned is None else returned


@pytest.fixture
def group_abi(monkeypatch):
    monkeypatch.setattr(identity.sys, 'platform', 'darwin')
    def install(scans, identities=None):
        call = GroupABI(scans)
        monkeypatch.setattr(identity.ctypes.util, 'find_library', lambda name: 'fixture-proc')
        monkeypatch.setattr(identity.ctypes, 'CDLL',
                            lambda name, use_errno: type('Library', (), {'proc_listpids': call})())
        monkeypatch.setattr(identity, 'process_identity',
                            lambda pid: (identities or {}).get(pid, _identity(pid)))
        return call
    return install


def test_group_grows_full_buffer_and_returns_sorted_full_identities(group_abi):
    first = tuple(range(100, 116))
    call = group_abi([(first, None, 0), ((102, 100), None, 0),
                      ((100, 102), None, 0)])
    assert identity.process_group_identities(71) == (_identity(100), _identity(102))
    assert call.sizes == [64, 128, 64]
    assert call.argtypes == [ctypes.c_uint32, ctypes.c_uint32,
                             ctypes.c_void_p, ctypes.c_int]
    assert call.restype == ctypes.c_int


@pytest.mark.parametrize('pgid', [0, -1, True, False, 1.0, '71', None, 2**31])
def test_invalid_group_refuses_before_abi(group_abi, pgid):
    group_abi([])
    with pytest.raises(RuntimeError, match='^group_identity_unavailable$'):
        identity.process_group_identities(pgid)


@pytest.mark.parametrize('returned', [-1, 1, 3, 5, 65, 2**31])
def test_invalid_return_byte_count_refuses(group_abi, returned):
    group_abi([((100,), returned, 0)])
    with pytest.raises(RuntimeError, match='^group_identity_unavailable$'):
        identity.process_group_identities(71)


@pytest.mark.parametrize('pids,returned,scans', [
    pytest.param((100,), 4.0, 1, id='float-four-bytes'),
    pytest.param((), False, 2, id='false-empty-scans'),
    pytest.param((), True, 1, id='true-byte-count'),
])
def test_noninteger_return_byte_count_refuses(group_abi, pids, returned, scans):
    call = group_abi([(pids, returned, 0)] * scans)
    with pytest.raises(RuntimeError, match='^group_identity_unavailable$'):
        identity.process_group_identities(71)
    assert len(call.sizes) == 1


def test_zero_with_errno_refuses_but_two_clean_empty_scans_prove_empty(group_abi):
    group_abi([((), 0, errno.EPERM)])
    with pytest.raises(RuntimeError, match='^group_identity_unavailable$'):
        identity.process_group_identities(71)
    group_abi([((), 0, 0), ((), 0, 0)])
    assert identity.process_group_identities(71) == ()


def test_group_clears_errno_before_each_scan(group_abi):
    call = group_abi([((), 0, 0), ((), 0, 0)])
    ctypes.set_errno(errno.EPERM)
    assert identity.process_group_identities(71) == ()
    assert len(call.sizes) == 2


def test_nonzero_unreported_pid_slot_refuses(group_abi):
    group_abi([((100, 101), 4, 0), ((100,), 4, 0)])
    with pytest.raises(RuntimeError, match='^group_identity_unavailable$'):
        identity.process_group_identities(71)


@pytest.mark.parametrize('pids', [(0,), (100, 100), (-3,)])
def test_invalid_pid_entries_refuse(group_abi, pids):
    group_abi([(pids, None, 0)])
    with pytest.raises(RuntimeError, match='^group_identity_unavailable$'):
        identity.process_group_identities(71)


@pytest.mark.parametrize('second', [(), (100, 102)])
def test_membership_churn_refuses(group_abi, second):
    group_abi([((100,), None, 0), (second, None, 0)])
    with pytest.raises(RuntimeError, match='^group_identity_unavailable$'):
        identity.process_group_identities(71)


@pytest.mark.parametrize('changed', [
    _identity(100, pgid=72), _identity(101), _identity(100, uid=502),
    _identity(100, birth=3), {**_identity(100), 'birth': [0, 2]},
])
def test_changed_or_invalid_identity_refuses(monkeypatch, group_abi, changed):
    reads = iter([_identity(100), changed])
    group_abi([((100,), None, 0), ((100,), None, 0)])
    monkeypatch.setattr(identity, 'process_identity', lambda pid: next(reads))
    with pytest.raises(RuntimeError, match='^group_identity_unavailable$'):
        identity.process_group_identities(71)


@pytest.mark.parametrize('observed', [
    pytest.param(None, id='missing-identity'),
    pytest.param([], id='wrong-container'),
    pytest.param({**_identity(100), 'extra': 1}, id='extra-key'),
    pytest.param({key: value for key, value in _identity(100).items() if key != 'uid'},
                 id='missing-key'),
    pytest.param({**_identity(100), 'pid': True}, id='pid-bool'),
    pytest.param({**_identity(100), 'pid': 100.0}, id='pid-float'),
    pytest.param({**_identity(100), 'uid': True}, id='uid-bool'),
    pytest.param({**_identity(100), 'uid': 501.0}, id='uid-float'),
    pytest.param({**_identity(100), 'uid': -1}, id='uid-negative'),
    pytest.param({**_identity(100), 'ppid': True}, id='ppid-bool'),
    pytest.param({**_identity(100), 'ppid': 1.0}, id='ppid-float'),
    pytest.param({**_identity(100), 'ppid': -1}, id='ppid-negative'),
    pytest.param({**_identity(100), 'pgid': True}, id='pgid-bool'),
    pytest.param({**_identity(100), 'pgid': 71.0}, id='pgid-float'),
    pytest.param({**_identity(100), 'birth': (1, 2)}, id='birth-tuple'),
    pytest.param({**_identity(100), 'birth': [1]}, id='birth-short'),
    pytest.param({**_identity(100), 'birth': [1, 2, 3]}, id='birth-long'),
    pytest.param({**_identity(100), 'birth': [True, 2]}, id='birth-seconds-bool'),
    pytest.param({**_identity(100), 'birth': [1.0, 2]}, id='birth-seconds-float'),
    pytest.param({**_identity(100), 'birth': [1, True]}, id='birth-useconds-bool'),
    pytest.param({**_identity(100), 'birth': [1, 2.0]}, id='birth-useconds-float'),
    pytest.param({**_identity(100), 'birth': [-1, 2]}, id='birth-seconds-negative'),
    pytest.param({**_identity(100), 'birth': [1, -1]}, id='birth-useconds-negative'),
    pytest.param({**_identity(100), 'birth': [1, 1000000]}, id='birth-useconds-high'),
])
def test_invalid_initial_identity_refuses(group_abi, monkeypatch, observed):
    group_abi([((100,), None, 0), ((100,), None, 0)])
    reads = iter([observed, observed])
    monkeypatch.setattr(identity, 'process_identity', lambda pid: next(reads))
    with pytest.raises(RuntimeError, match='^group_identity_unavailable$'):
        identity.process_group_identities(71)


@pytest.mark.parametrize('field,value', [
    pytest.param('pid', 100.0, id='pid-float'),
    pytest.param('uid', 501.0, id='uid-float'),
    pytest.param('ppid', True, id='ppid-bool'),
    pytest.param('ppid', 1.0, id='ppid-float'),
    pytest.param('pgid', 71.0, id='pgid-float'),
    pytest.param('birth', [1.0, 2], id='birth-seconds-float'),
    pytest.param('birth', [1, 2.0], id='birth-useconds-float'),
])
def test_python_equal_invalid_reread_identity_refuses(group_abi, monkeypatch, field, value):
    original = _identity(100)
    reread = {**original, field: value}
    assert reread == original
    group_abi([((100,), None, 0), ((100,), None, 0)])
    reads = iter([original, reread])
    monkeypatch.setattr(identity, 'process_identity', lambda pid: next(reads))
    with pytest.raises(RuntimeError, match='^group_identity_unavailable$'):
        identity.process_group_identities(71)


@pytest.mark.parametrize('observed', [
    pytest.param(None, id='missing-identity'),
    pytest.param([], id='wrong-container'),
    pytest.param({**_identity(100), 'extra': 1}, id='extra-key'),
    pytest.param({key: value for key, value in _identity(100).items() if key != 'uid'},
                 id='missing-key'),
    pytest.param({**_identity(100), 'birth': (1, 2)}, id='birth-tuple'),
    pytest.param({**_identity(100), 'birth': [1]}, id='birth-short'),
    pytest.param({**_identity(100), 'birth': [1, 2, 3]}, id='birth-long'),
    pytest.param({**_identity(100), 'birth': [1, -1]}, id='birth-useconds-negative'),
    pytest.param({**_identity(100), 'birth': [1, 1000000]}, id='birth-useconds-high'),
])
def test_invalid_reread_identity_refuses(group_abi, monkeypatch, observed):
    group_abi([((100,), None, 0), ((100,), None, 0)])
    reads = iter([_identity(100), observed])
    monkeypatch.setattr(identity, 'process_identity', lambda pid: next(reads))
    with pytest.raises(RuntimeError, match='^group_identity_unavailable$'):
        identity.process_group_identities(71)


def test_group_cap_exhaustion_refuses(group_abi):
    call = group_abi([((1,) * count, None, 0) for count in
                      (16, 32, 64, 128, 256, 512, 1024, 2048, 4096)])
    with pytest.raises(RuntimeError, match='^group_identity_unavailable$'):
        identity.process_group_identities(71)
    assert call.sizes[-1] == 4096 * 4


@pytest.mark.parametrize('failure', ['load', 'symbol', 'permission'])
def test_group_abi_failures_refuse(monkeypatch, group_abi, failure):
    if failure == 'permission':
        group_abi([((), 0, errno.EPERM)])
    else:
        group_abi([])
        if failure == 'load':
            monkeypatch.setattr(identity.ctypes, 'CDLL', lambda *a, **k: (_ for _ in ()).throw(OSError()))
        else:
            monkeypatch.setattr(identity.ctypes, 'CDLL', lambda *a, **k: object())
    with pytest.raises(RuntimeError, match='^group_identity_unavailable$'):
        identity.process_group_identities(71)


class BootABI:
    def __init__(self, raw, size=37, result=0, error=0):
        self.raw, self.size, self.result, self.error = raw, size, result, error
        self.argtypes = self.restype = None
        self.calls = 0

    def __call__(self, name, buffer, size_pointer, new, new_size):
        assert name == b'kern.bootsessionuuid'
        assert new is None and new_size == 0
        self.calls += 1
        capacity = ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_size_t))
        assert capacity[0] == 37
        ctypes.set_errno(self.error)
        ctypes.memmove(buffer, self.raw, min(len(self.raw), 37))
        capacity[0] = self.size
        return self.result


@pytest.fixture
def boot_abi(monkeypatch):
    monkeypatch.setattr(identity.sys, 'platform', 'darwin')
    def install(raw, size=37, result=0, error=0):
        call = BootABI(raw, size, result, error)
        monkeypatch.setattr(identity.ctypes, 'CDLL',
                            lambda name, use_errno: type('Library', (), {'sysctlbyname': call})())
        return call
    return install


def test_boot_uuid_is_kernel_value_and_fixed_read(monkeypatch, boot_abi):
    value = '01234567-89AB-CDEF-0123-456789ABCDEF'
    monkeypatch.setenv('BOOT_IDENTITY', 'untrusted')
    call = boot_abi(value.encode() + b'\0')
    assert identity.boot_identity() == value
    assert call.calls == 1
    assert call.argtypes == [ctypes.c_char_p, ctypes.c_void_p,
                             ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
                             ctypes.c_size_t]
    assert call.restype == ctypes.c_int


def test_boot_success_with_errno_refuses(boot_abi):
    boot_abi(b'01234567-89ab-cdef-0123-456789abcdef\0', error=errno.EPERM)
    with pytest.raises(RuntimeError, match='^boot_identity_unavailable$'):
        identity.boot_identity()


def test_boot_all_zero_uuid_refuses(boot_abi):
    boot_abi(b'00000000-0000-0000-0000-000000000000\0', size=37)
    with pytest.raises(RuntimeError, match='^boot_identity_unavailable$'):
        identity.boot_identity()


def test_boot_noninteger_success_code_refuses(boot_abi):
    boot_abi(b'01234567-89ab-cdef-0123-456789abcdef\0', result=0.0)
    with pytest.raises(RuntimeError, match='^boot_identity_unavailable$'):
        identity.boot_identity()


@pytest.mark.parametrize('raw,size,result', [
    (b'0'*36+b'\0', 36, 0), (b'0'*36+b'\0', 38, 0),
    (b'0'*37, 37, 0), (b'0'*35+b'\0\0', 37, 0),
    (b'0'*35+b'\0X', 37, 0), (b'0'*36+b'\0', 37, -1),
    (b'Z'*36+b'\0', 37, 0),
])
def test_boot_invalid_kernel_response_refuses(boot_abi, raw, size, result):
    boot_abi(raw, size, result)
    with pytest.raises(RuntimeError, match='^boot_identity_unavailable$'):
        identity.boot_identity()


@pytest.mark.parametrize('failure', ['load', 'symbol'])
def test_boot_abi_failures_refuse(monkeypatch, boot_abi, failure):
    boot_abi(b'0'*36+b'\0')
    if failure == 'load':
        monkeypatch.setattr(identity.ctypes, 'CDLL', lambda *a, **k: (_ for _ in ()).throw(OSError()))
    else:
        monkeypatch.setattr(identity.ctypes, 'CDLL', lambda *a, **k: object())
    with pytest.raises(RuntimeError, match='^boot_identity_unavailable$'):
        identity.boot_identity()


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel ABI proof')
def test_retained_disposable_group_has_full_kernel_identities():
    child_code = (
        'import select,sys,time\n'
        'deadline=time.monotonic()+12\n'
        'while time.monotonic()<deadline:\n'
        '    if select.select([sys.stdin],[],[],max(0,deadline-time.monotonic()))[0]:\n'
        '        if not sys.stdin.buffer.read(1): break\n'
    )
    leader_code = (
        'import select,subprocess,sys,time\n'
        'child=None\n'
        'try:\n'
        '    child=subprocess.Popen([sys.executable,"-B","-c",'+repr(child_code)+'], '
        'stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
        '    print(child.pid,flush=True)\n'
        '    deadline=time.monotonic()+15\n'
        '    while time.monotonic()<deadline:\n'
        '        if select.select([sys.stdin],[],[],max(0,deadline-time.monotonic()))[0]:\n'
        '            if not sys.stdin.buffer.read(1): break\n'
        'finally:\n'
        '    if child is not None:\n'
        '        child.stdin.close()\n'
        '        child.wait(timeout=16)\n'
    )
    leader = subprocess.Popen([sys.executable, '-B', '-c', leader_code],
                              start_new_session=True, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        line = b''
        while b'\n' not in line:
            remaining = deadline - time.monotonic()
            assert remaining > 0
            assert select.select([leader.stdout], [], [], remaining)[0]
            chunk = os.read(leader.stdout.fileno(), 64)
            assert chunk
            line += chunk
            assert len(line) <= 64
        descendant_pid = int(line.strip())
        observed = identity.process_group_identities(leader.pid)
        assert tuple(item['pid'] for item in observed) == tuple(sorted((leader.pid, descendant_pid)))
        assert all(item['pgid'] == leader.pid and item['birth'][0] > 0 for item in observed)
        assert observed == tuple(identity.process_identity(item['pid']) for item in observed)
    finally:
        leader.stdin.close()
        try:
            leader.wait(timeout=20)
        finally:
            leader.stdout.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin kernel ABI proof')
def test_real_kernel_boot_identity_has_fixed_uuid_shape():
    value = identity.boot_identity()
    assert re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value)
