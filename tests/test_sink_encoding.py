import json
import stat

import pytest

from test_lifecycle_guard import guard


def _write():
    return guard.sink_write('VETO', 'LGV-001', 'terminal')


def _seeded_active(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    monkeypatch.setenv('XDG_STATE_HOME', str(state))
    assert _write() is True
    candidates = [
        path for path in state.rglob('*')
        if path.is_file() and b'"category"' in path.read_bytes()
    ]
    assert len(candidates) == 1
    active = candidates[0]
    assert stat.S_IMODE(active.stat().st_mode) == 0o600
    directory = active.parent
    while True:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        if directory == state:
            break
        assert state in directory.parents
        directory = directory.parent
    original = active.read_bytes()
    assert original.endswith(b'\n')
    record = json.loads(original[:-1].decode('utf-8'))
    assert set(record) == {'ts', 'category', 'code', 'tool'}
    assert record['category'] == 'VETO'
    assert record['code'] == 'LGV-001'
    assert record['tool'] == 'terminal'
    canonical = json.dumps(record, sort_keys=True,
                           separators=(',', ':')).encode('utf-8')
    return active, canonical


@pytest.mark.parametrize('encoding', ('utf8_bom', 'utf16le_bom',
                                      'utf16be_bom'))
def test_seeded_non_utf8_line_is_rejected_without_mutation(
        tmp_path, monkeypatch, encoding):
    active, canonical = _seeded_active(tmp_path, monkeypatch)
    if encoding == 'utf8_bom':
        seed = b'\xef\xbb\xbf' + canonical + b'\n'
    elif encoding == 'utf16le_bom':
        seed = b'\xff\xfe' + canonical.decode('utf-8').encode('utf-16-le') + b'\n'
    else:
        seed = b'\xfe\xff' + canonical.decode('utf-8').encode('utf-16-be') + b'\n'
    assert len(seed) <= guard._SINK_RECORD_MAX
    active.write_bytes(seed)
    mode = stat.S_IMODE(active.stat().st_mode)
    assert mode == 0o600
    assert _write() is False
    assert active.read_bytes() == seed
    assert stat.S_IMODE(active.stat().st_mode) == mode


def test_seeded_valid_utf8_line_allows_append(tmp_path, monkeypatch):
    active, canonical = _seeded_active(tmp_path, monkeypatch)
    seed = canonical + b'\n'
    active.write_bytes(seed)
    assert _write() is True
    lines = active.read_bytes().splitlines(keepends=True)
    assert len(lines) == 2
    assert lines[0] == seed
    assert set(json.loads(lines[1].decode('utf-8'))) == {
        'ts', 'category', 'code', 'tool'}
