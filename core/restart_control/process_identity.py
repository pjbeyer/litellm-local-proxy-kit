"""Read-only process/peer identities (Apple public proc_bsdinfo ABI).

ABI reference: apple-oss-distributions/xnu, bsd/sys/proc_info.h.
No ps environment reads, process control or second-resolution birth guesses.

T010: the kernel identity ABI primitive now lives in runtime_identity.py so
the flat standalone checker owns it without controller-package imports.
This module re-exports it (with compatibility aliases) and keeps only the
group-census and peer-identity surfaces; there is no duplicate ABI.
"""
import ctypes
import ctypes.util
import os
import socket
import struct
import sys

from .runtime_identity import kernel_process_identity, kernel_boot_identity

# Compatibility aliases: the historical names keep working; the single ABI
# implementation is kernel_process_identity/kernel_boot_identity.
process_identity = kernel_process_identity
boot_identity = kernel_boot_identity


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
