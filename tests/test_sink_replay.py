import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

from test_lifecycle_guard import guard, MODULE_PATH


CANON = {
    'ts': '2000-01-01T00:00:00+0000',
    'category': 'VETO',
    'code': 'LGV-001',
    'tool': 'terminal',
}
LINE = (json.dumps(CANON, separators=(',', ':')) + '\n').encode('ascii')
CHILD = r'''
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
state, cap = sys.argv[2:]
os.environ['XDG_STATE_HOME'] = state
spec = importlib.util.spec_from_file_location('_sink_replay_fresh', path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
if cap:
    module.SINK_MAX_BYTES = int(cap)
accepted = module.sink_write('VETO', 'LGV-001', 'terminal')
print(json.dumps({
    'accepted': accepted,
    'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
}, separators=(',', ':')))
'''


def _child(state, cap=None):
    result = subprocess.run(
        [sys.executable, '-B', '-c', CHILD, str(MODULE_PATH), str(state),
         '' if cap is None else str(cap)],
        capture_output=True, text=True, timeout=5, check=True,
    )
    payload = json.loads(result.stdout)
    assert set(payload) == {'accepted', 'source_sha256'}
    assert type(payload['accepted']) is bool
    assert payload['source_sha256'] == hashlib.sha256(
        Path(MODULE_PATH).read_bytes()
    ).hexdigest()
    return payload['accepted']


def _fixture(tmp_path, monkeypatch, cap=None):
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setenv('XDG_STATE_HOME', str(state))
    assert _child(state, cap) is True
    active = state / 'hermes' / 'lifecycle-guard' / 'decisions.jsonl'
    lock = active.with_name('decisions.jsonl.lock')
    assert active.is_file() and lock.is_file()
    active.write_bytes(LINE)
    active.chmod(0o600)
    return state, active, lock


def _snapshot(path):
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _identity(info):
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode),
            stat.S_IMODE(info.st_mode), info.st_nlink)


def _records(path):
    raw = path.read_bytes()
    assert raw and raw.endswith(b'\n')
    records = []
    for line in raw[:-1].split(b'\n'):
        pairs = json.loads(line.decode('utf-8'), object_pairs_hook=lambda p: p)
        assert type(pairs) is list and len(pairs) == 4
        keys = [key for key, _ in pairs]
        assert set(keys) == set(CANON) and len(set(keys)) == 4
        record = dict(pairs)
        assert all(type(value) is str for value in record.values())
        records.append(record)
    return records


def test_fresh_process_rejects_extra_schema_key(tmp_path, monkeypatch):
    state, active, lock = _fixture(tmp_path, monkeypatch)
    assert lock.is_file()
    malformed = dict(CANON, extra='x')
    active.write_bytes(
        (json.dumps(malformed, separators=(',', ':')) + '\n').encode('ascii')
    )
    active.chmod(0o600)
    before = _snapshot(active)
    assert _child(state) is False
    assert _snapshot(active) == before


def test_failed_truncate_leaves_durable_malformed_tail(tmp_path, monkeypatch):
    state, active, _ = _fixture(tmp_path, monkeypatch)
    before = _snapshot(active)
    info = active.stat()
    target = (info.st_dev, info.st_ino)
    real_fstat = guard.os.fstat
    real_write = guard.os.write
    real_truncate = guard.os.ftruncate
    faults = {'write': 0, 'truncate': 0}

    def owned(fd):
        try:
            current = real_fstat(fd)
        except OSError:
            return False
        return (current.st_dev, current.st_ino) == target

    def short_write(fd, data):
        if owned(fd):
            faults['write'] += 1
            if faults['write'] == 1:
                return real_write(fd, data[:7])
            return 0
        return real_write(fd, data)

    def failed_truncate(fd, length):
        if owned(fd):
            faults['truncate'] += 1
            raise OSError('injected truncate failure')
        return real_truncate(fd, length)

    with monkeypatch.context() as patch:
        patch.setattr(guard.os, 'write', short_write)
        patch.setattr(guard.os, 'ftruncate', failed_truncate)
        assert guard.sink_write('VETO', 'LGV-001', 'terminal') is False

    assert faults['write'] >= 1 and faults['truncate'] >= 1
    damaged = _snapshot(active)
    assert damaged[1] == before[1]
    assert damaged[0].startswith(before[0])
    tail = damaged[0][len(before[0]):]
    assert tail and len(tail) <= 7 and not tail.endswith(b'\n')
    assert _child(state) is False
    assert _snapshot(active) == damaged


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo'])
def test_unsafe_lock_entry_is_preserved(tmp_path, monkeypatch, kind):
    _, active, lock = _fixture(tmp_path, monkeypatch)
    active_before = _snapshot(active)
    lock.unlink()
    victim = tmp_path / 'victim.jsonl'
    victim.write_bytes(LINE)
    victim.chmod(0o600)
    if kind == 'symlink':
        os.symlink(victim, lock)
    elif kind == 'hardlink':
        os.link(victim, lock)
    else:
        os.mkfifo(lock, 0o600)
        lock.chmod(0o600)
    lock_before = _identity(lock.lstat())
    victim_before = (_snapshot(victim), _identity(victim.stat()))

    started = time.monotonic()
    assert guard.sink_write('VETO', 'LGV-001', 'terminal') is False
    elapsed = time.monotonic() - started
    if kind == 'fifo':
        assert elapsed < 1
        assert stat.S_ISFIFO(lock.lstat().st_mode)
    elif kind == 'symlink':
        assert stat.S_ISLNK(lock.lstat().st_mode)
        assert os.readlink(lock) == str(victim)
    else:
        assert stat.S_ISREG(lock.lstat().st_mode)
        assert lock.stat().st_ino == victim.stat().st_ino
    assert _identity(lock.lstat()) == lock_before
    assert (_snapshot(victim), _identity(victim.stat())) == victim_before
    assert _snapshot(active) == active_before


def test_injected_active_uid_mismatch(tmp_path, monkeypatch):
    '''Injected UID mismatch, not a genuine foreign-owned file.'''
    _, active, _ = _fixture(tmp_path, monkeypatch)
    before = _snapshot(active)
    info = active.stat()
    target = (info.st_dev, info.st_ino)
    real_fstat = guard.os.fstat
    observed = []

    def foreign_fstat(fd):
        current = real_fstat(fd)
        if (current.st_dev, current.st_ino) == target:
            observed.append(fd)
            fields = list(current)
            fields[stat.ST_UID] = os.getuid() + 1
            return os.stat_result(fields)
        return current

    with monkeypatch.context() as patch:
        patch.setattr(guard.os, 'fstat', foreign_fstat)
        assert guard.sink_write('VETO', 'LGV-001', 'terminal') is False
    assert observed
    assert _snapshot(active) == before


def test_cross_process_lock_is_nonblocking(tmp_path, monkeypatch):
    state, active, lock = _fixture(tmp_path, monkeypatch)
    before = _snapshot(active)
    assert _records(active) == [CANON]
    with lock.open('rb+') as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert _child(state) is False
            assert _snapshot(active) == before
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    assert _child(state) is True
    assert len(_records(active)) == 2
    assert active.read_bytes().startswith(LINE)


def test_two_fresh_processes_rotate_once_at_cap(tmp_path, monkeypatch):
    cap = 512
    state, active, lock = _fixture(tmp_path, monkeypatch, cap)
    seed_count = cap // len(LINE)
    assert seed_count > 0 and (seed_count + 1) * len(LINE) > cap
    assert 2 * len(LINE) <= cap
    seed = LINE * seed_count
    active.write_bytes(seed)
    active.chmod(0o600)
    assert _records(active) == [CANON] * seed_count

    assert _child(state, cap) is True
    rotated = active.with_name('decisions.jsonl.1')
    assert rotated.read_bytes() == seed
    rotated_first = (_snapshot(rotated), _identity(rotated.stat()))
    assert _child(state, cap) is True

    assert (_snapshot(rotated), _identity(rotated.stat())) == rotated_first
    assert {path.name for path in active.parent.iterdir()} == {
        'decisions.jsonl', 'decisions.jsonl.1', 'decisions.jsonl.lock'
    }
    assert len(active.read_bytes()) <= cap
    assert len(rotated.read_bytes()) <= cap
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600
               for path in (active, rotated, lock))
    records = _records(rotated) + _records(active)
    assert len(records) == seed_count + 2
    assert records[:seed_count] == [CANON] * seed_count
    assert all((item['category'], item['code'], item['tool']) ==
               ('VETO', 'LGV-001', 'terminal') for item in records)
