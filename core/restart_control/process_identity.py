"""Read-only process/peer identities (Apple public proc_bsdinfo ABI).

ABI reference: apple-oss-distributions/xnu, bsd/sys/proc_info.h.
No ps environment reads, process control or second-resolution birth guesses.
"""
import ctypes
import ctypes.util
import os
import re
import socket
import struct
import sys


class _BSDInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
        'rgid', 'svuid', 'svgid', 'reserved')] + [
        ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)] + [
        (n, ctypes.c_uint32) for n in ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')] + [
        ('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64),
        ('start_usec', ctypes.c_uint64)]


def process_identity(pid):
    if sys.platform != 'darwin' or type(pid) is not int or pid <= 0:
        raise RuntimeError('identity_unavailable')
    library = ctypes.CDLL(ctypes.util.find_library('proc'), use_errno=True)
    call = library.proc_pidinfo
    call.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                     ctypes.c_void_p, ctypes.c_int]
    call.restype = ctypes.c_int
    info = _BSDInfo()
    size = ctypes.sizeof(info)
    if call(pid, 3, 0, ctypes.byref(info), size) != size or info.pid != pid:
        raise RuntimeError('identity_unavailable')
    if not info.start_sec or info.start_usec >= 1000000:
        raise RuntimeError('identity_unavailable')
    return {'pid': int(info.pid), 'uid': int(info.uid), 'ppid': int(info.ppid),
            'pgid': int(info.pgid), 'birth': [int(info.start_sec), int(info.start_usec)]}


def _valid_group_identity(value, pid, pgid):
    if type(value) is not dict or set(value) != {'pid', 'uid', 'ppid', 'pgid', 'birth'}:
        return False
    if any(type(value[key]) is not int for key in ('pid', 'uid', 'ppid', 'pgid')):
        return False
    birth = value['birth']
    return (value['pid'] == pid and value['pgid'] == pgid and
            value['uid'] >= 0 and value['ppid'] >= 0 and
            type(birth) is list and len(birth) == 2 and
            all(type(part) is int for part in birth) and
            birth[0] > 0 and 0 <= birth[1] < 1000000)


def process_group_identities(pgid):
    """Observe a stable, complete bounded group; this grants no ownership rights.

    proc_listpids returns bytes (libproc.h); PROC_PGRP_ONLY is 2
    (sys/proc_info.h). Full buffers are ambiguous, including at the cap.
    """
    unavailable = RuntimeError('group_identity_unavailable')
    if (sys.platform != 'darwin' or type(pgid) is not int or
            pgid <= 0 or pgid >= 2**31 or ctypes.sizeof(ctypes.c_int32) != 4):
        raise unavailable
    try:
        library_name = ctypes.util.find_library('proc')
        if not library_name:
            raise unavailable
        call = ctypes.CDLL(library_name, use_errno=True).proc_listpids
        call.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                         ctypes.c_void_p, ctypes.c_int]
        call.restype = ctypes.c_int

        def enumerate_once():
            slots = 16
            while slots <= 4096:
                buffer = (ctypes.c_int32 * slots)()
                ctypes.set_errno(0)
                returned = call(2, pgid, buffer, ctypes.sizeof(buffer))
                error = ctypes.get_errno()
                if (type(returned) is not int or returned < 0 or error or
                        returned > ctypes.sizeof(buffer) or returned % 4):
                    raise unavailable
                if returned == ctypes.sizeof(buffer):
                    slots *= 2
                    continue
                if any(buffer[index] != 0 for index in range(returned // 4, slots)):
                    raise unavailable
                pids = tuple(buffer[index] for index in range(returned // 4))
                if any(pid <= 0 for pid in pids) or len(set(pids)) != len(pids):
                    raise unavailable
                return tuple(sorted(pids))
            raise unavailable

        first = enumerate_once()
        before = {}
        for pid in first:
            observed = process_identity(pid)
            if not _valid_group_identity(observed, pid, pgid):
                raise unavailable
            before[pid] = observed
        if enumerate_once() != first:
            raise unavailable
        after = []
        for pid in first:
            observed = process_identity(pid)
            if not _valid_group_identity(observed, pid, pgid) or observed != before[pid]:
                raise unavailable
            after.append(observed)
        return tuple(after)
    except (OSError, AttributeError, TypeError, ValueError, OverflowError,
            RuntimeError, StopIteration) as error:
        raise unavailable from error


def boot_identity():
    """Read the fixed NUL-terminated kernel bootsessionuuid (kern_sysctl.c)."""
    unavailable = RuntimeError('boot_identity_unavailable')
    if sys.platform != 'darwin':
        raise unavailable
    try:
        call = ctypes.CDLL(None, use_errno=True).sysctlbyname
        call.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
                         ctypes.c_size_t]
        call.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(37)
        size = ctypes.c_size_t(37)
        ctypes.set_errno(0)
        result = call(b'kern.bootsessionuuid', buffer, ctypes.byref(size), None, 0)
        error = ctypes.get_errno()
        raw = bytes(buffer)
        if (type(result) is not int or result != 0 or error or size.value != 37 or
                raw[36] != 0 or b'\0' in raw[:36]):
            raise unavailable
        value = raw[:36].decode('ascii')
        if (not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value)
                or int(value.replace('-', ''), 16) == 0):
            raise unavailable
        return value
    except (OSError, AttributeError, TypeError, ValueError, UnicodeError,
            RuntimeError) as error:
        raise unavailable from error


def peer_uid(connection):
    if sys.platform == 'darwin':
        call = ctypes.CDLL(None, use_errno=True).getpeereid
        call.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                         ctypes.POINTER(ctypes.c_uint)]
        call.restype = ctypes.c_int
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if call(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
            raise RuntimeError('peer_identity_unavailable')
        return uid.value
    if hasattr(socket, 'SO_PEERCRED'):
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                    struct.calcsize('3i'))
        return struct.unpack('3i', raw)[1]
    raise RuntimeError('peer_identity_unavailable')
