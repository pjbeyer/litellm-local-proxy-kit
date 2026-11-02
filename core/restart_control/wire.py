"""Private, bounded Unix JSON-line transport for the restart controller."""

import contextlib
import ctypes
import json
import os
import socket
import stat
import struct
import threading
import time
from pathlib import Path

from .engine import Fault, private_dir, strict_json


LIMIT = 65536
READ_TIMEOUT = 2.0


def _error(code):
    return {"status": "blocked", "code": code}


def _peer_uid(connection):
    if hasattr(connection, "getpeereid"):
        return connection.getpeereid()[0]
    if os.uname().sysname == "Darwin":
        uid = ctypes.c_uint()
        gid = ctypes.c_uint()
        if ctypes.CDLL(None, use_errno=True).getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise Fault("foreign_peer")
        return uid.value
    if hasattr(socket, "SO_PEERCRED"):
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)[1]
    raise Fault("foreign_peer")


def _readline(connection):
    chunks = bytearray()
    deadline = time.monotonic() + (connection.gettimeout() or READ_TIMEOUT)
    while len(chunks) <= LIMIT:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Fault("invalid_request")
        connection.settimeout(remaining)
        part = connection.recv(min(4096, LIMIT + 1 - len(chunks)))
        if not part:
            break
        chunks.extend(part)
        if b"\n" in part:
            break
    if len(chunks) > LIMIT or not chunks.endswith(b"\n") or chunks.count(b"\n") != 1:
        raise Fault("invalid_request")
    return bytes(chunks[:-1])


def _serve_one(connection, controller):
    with connection:
        connection.settimeout(READ_TIMEOUT)
        try:
            if _peer_uid(connection) != controller.uid:
                result = _error("foreign_peer")
            else:
                request = strict_json(_readline(connection))
                result = controller.handle(request) if isinstance(request, dict) else _error("invalid_request")
            raw = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
            if len(raw) > LIMIT:
                raw = b'{"status":"blocked","code":"internal_error"}\n'
            connection.sendall(raw)
        except Fault as exc:
            with contextlib.suppress(OSError):
                connection.sendall(json.dumps(_error(exc.code)).encode() + b"\n")
        except ValueError:
            with contextlib.suppress(OSError):
                connection.sendall(b'{"status":"blocked","code":"invalid_request"}\n')
        except (OSError, TimeoutError):
            pass


def serve(socket_path, controller, stop_event=None):
    """Serve until stop_event is set; never replace an existing endpoint."""
    path = Path(socket_path)
    private_dir(path.parent)
    if path.exists() or path.is_symlink():
        raise Fault("socket_exists")
    stop_event = stop_event or threading.Event()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    inode = None
    workers = []
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise Fault("state_io")
        inode = info.st_ino
        listener.listen(32)
        listener.settimeout(.2)
        while not stop_event.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            workers = [worker for worker in workers if worker.is_alive()]
            if len(workers) >= 16:
                connection.close()
                continue
            worker = threading.Thread(target=_serve_one, args=(connection, controller), daemon=True)
            workers.append(worker)
            worker.start()
    except OSError:
        raise Fault("state_io") from None
    finally:
        listener.close()
        deadline = time.monotonic() + 1000
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        if inode is not None:
            with contextlib.suppress(OSError):
                if path.lstat().st_ino == inode:
                    path.unlink()


def client(socket_path, request, timeout=900):
    try:
        raw = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(raw) > LIMIT:
            raise Fault("invalid_request")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(socket_path))
            connection.sendall(raw)
            response = strict_json(_readline(connection))
            if not isinstance(response, dict):
                raise Fault("controller_unavailable")
            return response
    except Fault:
        raise
    except (OSError, TimeoutError, ValueError, TypeError):
        raise Fault("controller_unavailable") from None
