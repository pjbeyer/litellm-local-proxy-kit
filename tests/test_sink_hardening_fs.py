"""Filesystem/record hardening tests for the decision sink (source repair).

Six behavioral test functions, including mode variants, pinning contract
D7/D8/D10/D11 against the synchronous ``sink_write`` primitive (the async
``log_decision`` lifecycle is deliberately out of scope here). All state is
disposable under pytest's tmp_path via the ``state_home`` fixture — the real
sink is never touched. No subprocess, signal, thread, FIFO or real-PID use
anywhere in this file. Expectations are pinned from the contract, not
derived from the implementation.
"""
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from test_lifecycle_guard import MODULE_PATH, guard

SINK_PARTS = ("hermes", "lifecycle-guard", "decisions.jsonl")
TS = "2000-01-01T00:00:00+0000"
CANON = {"ts": TS, "category": "VETO", "code": "LGV-001", "tool": "terminal"}
LINE = json.dumps(CANON, separators=(",", ":")) + "\n"


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    """Redirect the sink to a disposable state home (never the real one)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path


def sink_file(state_home):
    return state_home.joinpath(*SINK_PARTS)


def sink_dir(state_home):
    return sink_file(state_home).parent


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def make_valid_dir(state_home):
    d = sink_dir(state_home)
    d.mkdir(parents=True)
    os.chmod(d, 0o700)
    return d


@pytest.mark.parametrize("initial_mode", (0o600, 0o644))
def test_active_symlink_target_preserved(state_home, initial_mode):
    """D8: a linked active file is rejected; its target stays untouched."""
    make_valid_dir(state_home)
    victim = state_home / "victim.txt"
    victim.write_bytes(LINE.encode("ascii"))
    os.chmod(victim, initial_mode)
    before = victim.read_bytes()
    os.symlink(victim, sink_file(state_home))

    guard.sink_write("VETO", "LGV-001", "terminal")

    assert sink_file(state_home).is_symlink(), "linked active was replaced"
    assert os.readlink(sink_file(state_home)) == str(victim)
    assert victim.read_bytes() == before, "target content changed"
    assert mode_of(victim) == initial_mode, "target mode was tightened"


@pytest.mark.parametrize("initial_mode", (0o600, 0o644))
def test_active_hardlink_target_preserved(state_home, initial_mode):
    """D8: an existing nlink>1 active file is rejected unchanged."""
    make_valid_dir(state_home)
    victim = state_home / "victim.txt"
    victim.write_bytes(LINE.encode("ascii"))
    os.chmod(victim, initial_mode)
    os.link(victim, sink_file(state_home))  # nlink == 2

    guard.sink_write("VETO", "LGV-001", "terminal")

    assert victim.read_bytes() == LINE.encode("ascii"), "target content changed"
    assert sink_file(state_home).read_bytes() == LINE.encode("ascii")
    assert mode_of(victim) == initial_mode, "target mode was tightened"
    assert os.stat(victim).st_nlink == 2, "hardlink count changed"


@pytest.mark.parametrize("initial_mode", (0o700, 0o755))
def test_managed_directory_symlink_target_untouched(state_home, initial_mode):
    """D7: unsafe managed dir rejected; no chmod/file outside the sink."""
    decoy = state_home / "decoy"
    decoy.mkdir()
    os.chmod(decoy, initial_mode)
    sentinel = decoy / "keep.txt"
    sentinel.write_bytes(b"keep\n")
    parent = state_home / "hermes"
    parent.mkdir()
    os.symlink(decoy, parent / "lifecycle-guard")

    guard.sink_write("VETO", "LGV-001", "terminal")

    assert mode_of(decoy) == initial_mode, "decoy directory was chmodded"
    assert sentinel.read_bytes() == b"keep\n", "decoy contents changed"
    assert list(decoy.rglob("decisions.jsonl")) == [], "file created in decoy"
    assert sorted(p.name for p in decoy.iterdir()) == ["keep.txt"]
    assert (parent / "lifecycle-guard").is_symlink()
    assert os.readlink(parent / "lifecycle-guard") == str(decoy)


def test_existing_0644_active_tightened_before_content_write(
        state_home, monkeypatch):
    """D8: mode is 0600 on the owned descriptor AT the content write."""
    make_valid_dir(state_home)
    active = sink_file(state_home)
    active.write_bytes(b"")  # valid empty existing file
    os.chmod(active, 0o644)
    assert mode_of(active) == 0o644  # pinned pre-write state
    fixture_stat = os.stat(active)
    fixture_identity = (fixture_stat.st_dev, fixture_stat.st_ino)

    seen = []
    real_write = os.write

    def observe(fd):
        try:
            st = os.fstat(fd)
        except OSError:
            st = None
        if st is not None and (st.st_dev, st.st_ino) == fixture_identity:
            seen.append(stat.S_IMODE(st.st_mode))

    def recording_write(fd, data):
        observe(fd)
        return real_write(fd, data)

    # Observe both the old buffered-open writer and the new descriptor writer.
    real_open = open

    class RecordingFile:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def write(self, data):
            observe(self.wrapped.fileno())
            return self.wrapped.write(data)

    def recording_open(*args, **kwargs):
        return RecordingFile(real_open(*args, **kwargs))

    monkeypatch.setattr(os, "write", recording_write)
    monkeypatch.setattr(guard, "open", recording_open, raising=False)
    guard.sink_write("VETO", "LGV-001", "terminal")

    assert seen, "content write on the fixture descriptor was not observed"
    assert set(seen) == {0o600}, "descriptor not 0600 at content-write time"
    assert mode_of(active) == 0o600
    lines = active.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert set(rec) == {"ts", "category", "code", "tool"}
    assert (rec["category"], rec["code"], rec["tool"]) == (
        "VETO", "LGV-001", "terminal")


@pytest.mark.parametrize("initial_mode", (0o600, 0o644))
def test_overflow_with_occupied_rotation_destination_drops_append(state_home, initial_mode):
    """D10: unsafe rotation symlink is rejected, not atomically replaced."""
    cap = getattr(guard, "SINK_MAX_BYTES", None)
    assert cap == 256 * 1024, "production cap must match the contract"
    make_valid_dir(state_home)
    active = sink_file(state_home)
    # Valid full-schema JSONL just under the cap: the next record overflows,
    # so any rejection is a size/rotation decision, not a malformed-state
    # rejection masking the branch under test.
    count = cap // len(LINE)
    active.write_bytes(LINE.encode() * count)
    os.chmod(active, 0o600)
    assert active.stat().st_size <= cap
    before = active.read_bytes()
    assert len(before) + len(LINE.encode("ascii")) > cap
    rotated = Path(str(active) + ".1")
    target = state_home / "rotation-target.jsonl"
    target.write_bytes(LINE.encode("ascii"))
    os.chmod(target, initial_mode)
    rotated.symlink_to(target)

    guard.sink_write("VETO", "LGV-001", "terminal")

    assert active.read_bytes() == before, "oversized append was performed"
    assert active.stat().st_size <= cap
    assert rotated.is_symlink(), "unsafe rotation destination was replaced"
    assert os.readlink(rotated) == str(target)
    assert target.read_bytes() == LINE.encode("ascii")
    assert mode_of(target) == initial_mode


def test_schema_invalid_prior_jsonl_rejected_across_fresh_module(state_home):
    """D11: syntactically valid but schema-invalid state rejected unchanged,
    both on the loaded module and on a fresh module import."""
    make_valid_dir(state_home)
    active = sink_file(state_home)
    bad = json.dumps({**CANON, "extra": "x"}, separators=(",", ":")) + "\n"
    active.write_bytes(bad.encode())
    os.chmod(active, 0o600)
    before = active.read_bytes()

    guard.sink_write("VETO", "LGV-001", "terminal")
    assert active.read_bytes() == before, "schema-invalid state was modified"

    # Fresh module import (same process; a fresh PROCESS is the parent's
    # probe) — rejection must not depend on first-import state.
    spec = importlib.util.spec_from_file_location(
        "lifecycle_guard_fs_replay", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load fresh lifecycle guard module")
    fresh = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fresh
    spec.loader.exec_module(fresh)
    fresh.sink_write("VETO", "LGV-001", "terminal")
    assert active.read_bytes() == before, "replay mutated rejected state"