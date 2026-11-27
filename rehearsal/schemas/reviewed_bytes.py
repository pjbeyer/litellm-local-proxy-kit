"""Retain exact reviewed bytes from a descriptor admitted by the storage boundary."""
from collections.abc import Callable
import os
import stat

from .configuration_check import InputError, is_digest, require_digest


def _absolute_parts(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
        raise InputError("input_invalid")
    parts = tuple(path[1:].split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise InputError("input_invalid")
    return parts


def relative_parts(path: str, *, given_root: str, canonical_root: str) -> tuple[str, ...]:
    """Select a literal suffix beneath one of two already admitted root spellings.

    This is lexical only. The caller must resolve the returned components
    relative to the held root object using a qualified storage boundary.
    """
    parts = _absolute_parts(path)
    roots = {_absolute_parts(given_root), _absolute_parts(canonical_root)}
    matches = [parts[len(root):] for root in roots if parts[:len(root)] == root]
    if len(matches) != 1 or not matches[0]:
        raise InputError("input_invalid")
    return matches[0]


def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid,
            info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_verified(fd: int, *, limit: int, expected: str,
                  require_admitted: Callable[[int], None]) -> bytes:
    """Snapshot a regular file. The caller retains ownership of the descriptor.

    The caller must safely acquire the descriptor without activating a mount.
    Its admission check must bind local storage and containment to this exact
    descriptor, raising on uncertainty. This helper does not resolve paths.
    """
    if type(limit) is not int or limit < 0 or not is_digest(expected):
        raise InputError("input_invalid")
    try:
        require_admitted(fd)
        before = os.fstat(fd)
        if not (stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and 0 <= before.st_size <= limit):
            raise InputError("input_invalid")
        data = bytearray()
        while len(data) <= before.st_size:
            chunk = os.pread(fd, min(65536, before.st_size + 1 - len(data)), len(data))
            if not chunk:
                break
            data.extend(chunk)
        require_admitted(fd)
        if len(data) != before.st_size or _identity(before) != _identity(os.fstat(fd)):
            raise InputError("input_invalid")
    except OSError:
        raise InputError("input_invalid") from None
    raw = bytes(data)
    require_digest(raw, expected)
    return raw
