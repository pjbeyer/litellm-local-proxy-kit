"""Read-only process/peer identities (Apple public proc_bsdinfo ABI).

ABI reference: apple-oss-distributions/xnu, bsd/sys/proc_info.h.
No ps environment reads, process control or second-resolution birth guesses.
"""
import ctypes
import ctypes.util
import os
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
