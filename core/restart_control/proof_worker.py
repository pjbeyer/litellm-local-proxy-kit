#!/usr/bin/env python3
"""Fresh isolated proof worker; all observations execute in the parent.

Invoked by the restart controller as a fixed script with -I -B:
    <pinned-python> -I -B proof_worker.py <pin.lock> <fd> <nonce>

The worker imports no controller package: it compiles the exact adjacent
runtime_identity source bytes (never a timestamp-valid .pyc) and performs
the existing shared proof, including the final fresh LiteLLM import. Every
read-only observation (Git/nm) is executed by the parent over one inherited
anonymous socketpair: closed JSON requests with monotonic sequence numbers,
status/length responses, one absolute deadline. The worker never spawns an
observer itself and accepts no executable, shell, environment, cwd or flags
over this channel. Stdout carries only the bounded proof result; refusal,
disconnection or any failure is a failed proof, never a success digest.
"""
from __future__ import annotations
import hashlib
import json
import os
import socket
import stat
import struct
import sys
import types
from pathlib import Path

sys.dont_write_bytecode = True  # before compiling the helper source

WORKER_VERSION = 1
REQUEST_LIMIT_BYTES = 4096
DATA_LIMIT = 4 * 1024 * 1024
BIND_TIMEOUT = 30


def _helper():
    """Compile the exact adjacent verified source; never a cached .pyc."""
    path = Path(__file__).resolve().parent / 'runtime_identity.py'
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or not 1 <= info.st_size <= 2 * 1024 * 1024):
        raise ValueError('runtime_helper_missing')
    raw = path.read_bytes()
    module = types.ModuleType('pinned_runtime_proof')
    module.__file__ = str(path)
    exec(compile(raw, str(path), 'exec', dont_inherit=True), module.__dict__)
    return module, raw


class _ParentChannel:
    """Context-local tool callback; the parent owns every observer child."""

    def __init__(self, connection, nonce, pin_path):
        self._connection = connection
        self._nonce = nonce
        self._pin_path = Path(pin_path)
        self._seq = 0

    def _op(self, argv):
        """Map one exact observation argv to the closed channel schema."""
        if type(argv) is not list or any(type(item) is not str for item in argv):
            raise ValueError('observer_request_invalid')
        if argv[:1] == ['/usr/bin/nm']:
            if len(argv) != 3 or argv[1] != '-gU':
                raise ValueError('observer_request_invalid')
            return 'native_exports', {}
        if len(argv) < 4 or argv[0] != 'git' or argv[1] != '-C':
            raise ValueError('observer_request_invalid')
        if argv[3:] == ['rev-parse', '--show-toplevel']:
            return 'git_root', {'path': argv[2]}
        if argv[3:] == ['rev-parse', 'HEAD']:
            return 'git_head', {}
        if len(argv) >= 7 and argv[3:7] == ['ls-tree', '-rz', 'HEAD', '--']:
            relatives = argv[7:]
            if relatives == ['litellm-rust', '.cargo']:
                return 'native_sources', {}
            if len(relatives) == 1 and relatives[0]:
                return 'git_entries', {'relative': relatives[0]}
        if argv[3:] == ['rev-parse', 'HEAD:litellm-rust']:
            return 'native_tree', {}
        raise ValueError('observer_request_invalid')

    def _request(self, op, fields, timeout):
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise ValueError('observer_request_invalid')
        self._seq += 1
        request = {'version': WORKER_VERSION, 'seq': self._seq, 'op': op, **fields}
        raw = json.dumps(request).encode()
        if len(raw) > REQUEST_LIMIT_BYTES:
            raise ValueError('observer_request_invalid')
        self._connection.settimeout(timeout)
        self._connection.sendall(raw + b'\n')
        return self._receive(timeout)

    def _receive(self, timeout):
        header = self._receive_exact(5, timeout)
        status, length = struct.unpack('>BI', header)
        if status not in (0, 1) or length > DATA_LIMIT:
            raise ValueError('observer_response_invalid')
        data = self._receive_exact(length, timeout) if length else b''
        if status == 1:
            raise ValueError('native_observation_invalid')
        return data

    def _receive_exact(self, count, timeout):
        chunks = bytearray()
        while len(chunks) < count:
            self._connection.settimeout(timeout)
            part = self._connection.recv(count - len(chunks))
            if not part:
                raise ValueError('observer_channel_closed')
            chunks.extend(part)
        return bytes(chunks)

    def bind(self, source_sha256):
        """First request: version/nonce/worker identity and digest bindings."""
        pin_raw = self._pin_path.read_bytes()
        self._request('bind', {'nonce': self._nonce, 'pid': os.getpid(),
                               'source_sha256': source_sha256,
                               'pin_sha256': hashlib.sha256(pin_raw).hexdigest()},
                      BIND_TIMEOUT)

    def observe(self, argv, timeout, limit=4*1024*1024, env=None):
        """One bounded observation; env and execution are parent-owned."""
        op, fields = self._op(argv)
        return self._request(op, fields, timeout)


def main():
    try:
        if len(sys.argv) != 4:
            raise ValueError('invalid_input')
        pin_path, fd_raw, nonce = sys.argv[1], int(sys.argv[2]), sys.argv[3]
        module, raw = _helper()
        connection = socket.socket(fileno=fd_raw)
        try:
            channel = _ParentChannel(connection, nonce, pin_path)
            channel.bind(hashlib.sha256(raw).hexdigest())
            module._CHANNEL_STATE['tool'] = channel
            print(json.dumps(module.prove_runtime(pin_path)))
        finally:
            connection.close()
        return 0
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
