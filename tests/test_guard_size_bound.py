"""lpkit-5j5: execute_code/terminal size-bound boundary gate.

Documents the ENFORCED threshold empirically established in V&V
(2026-11-13): the effective limit is MAX_TEXT_CHARS (65536 chars,
the chunk-splitter cap), NOT the config input_text_bytes bound
(262144); a larger remainder is an unprovable shape (FR-007 row c).
The BOUND_SIZE refusal message states the actual bound so the
operator can predict what passes (acceptance criterion 2).

Boundary pins: over 65536 chars the input takes the FR-007 bound
path (head-scan rows a/b/c) — either ALLOW (intent-free,
parse-provable head), or BOUND_SIZE/LGV-010 (unprovable remainder),
never a lifecycle VETO for benign filler. The original 5j5 repro
class (few-KB bd-SQL probe) must pass.
Offline: payloads NEVER executed (benign syntax-clean filler)."""
import importlib.util
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from test_lifecycle_guard import (FIXTURE_PID, MODULE_PATH, write_policy)


@pytest.fixture()
def ex_scope(tmp_path, monkeypatch):
    parent = Path(os.environ.get('TMPDIR')
                  or tempfile.gettempdir()).resolve()
    owned = Path(tempfile.mkdtemp(prefix='guard-size-', dir=parent))
    monkeypatch.setenv('XDG_STATE_HOME', str(owned / 'state'))
    policy = write_policy(tmp_path)
    spec = importlib.util.spec_from_file_location(module_key := f'guard_size_{uuid.uuid4().hex}', MODULE_PATH)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = guard
    spec.loader.exec_module(guard)

    def forbidden(*args, **kwargs):
        raise AssertionError('native operation reached')

    monkeypatch.setattr(guard.subprocess, 'run', forbidden)
    monkeypatch.setattr(guard.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(guard.os, 'kill', forbidden)
    monkeypatch.setattr(guard.os, 'killpg', forbidden)
    snapshot = guard.Snapshot(
        pids=frozenset({FIXTURE_PID}), pgids=frozenset({FIXTURE_PID}),
        comms=frozenset({'litellm'}), args=('litellm --port 4000',))

    def discover(port):
        return snapshot

    def evaluate(tool, args):
        return guard.evaluate(tool, args, policy, discover=discover)

    yield evaluate
    if hasattr(guard, 'sink_shutdown'):
        try:
            guard.sink_shutdown()
        except Exception:
            pass


def _benign_script(n_chars):
    """Syntax-clean, intent-free Python of exactly n_chars."""
    header = "x = 1\n"
    tail = "\ny = x + 1\n"
    pad_len = n_chars - len(header) - len(tail)
    assert pad_len > 0
    pad = "# " + "x" * pad_len + "\n"
    return header + pad + tail


def test_over_bound_benign_is_bound_path_not_veto(ex_scope):
    # 65538 > MAX_TEXT_CHARS (65536): over-bound input takes the
    # FR-007 bound path — ALLOW (intent-free, provable head) or
    # BOUND_SIZE, never a lifecycle VETO for benign filler
    decision = ex_scope("execute_code",
                        {"code": _benign_script(65538)})
    assert decision is None or (
        decision["category"] == "BOUND_SIZE"
        and decision["log_code"] == "LGV-010")


def test_bound_size_message_states_bound(ex_scope):
    # if the over-bound benign shape refuses, the message states
    # the numeric bound (5j5 acceptance criterion 2)
    decision = ex_scope("execute_code",
                        {"code": _benign_script(65538)})
    if decision is not None:
        assert "65536" in decision["message"]


def test_under_bound_allow(ex_scope):
    # just under the cap passes full analysis and allows
    assert ex_scope("execute_code",
                    {"code": _benign_script(60000)}) is None


def test_small_payload_allow(ex_scope):
    # the original 5j5 repro class: a few-KB bd-SQL probe must pass
    code = ("import subprocess\n"
            "r = subprocess.run(['bd', 'sql', 'select count(*) "
            "from tasks'], capture_output=True, text=True)\n"
            "print(r.stdout)\n")
    assert ex_scope("execute_code", {"code": code}) is None


def test_config_bound_documented(tmp_path):
    # input_text_bytes remains the outer documented policy bound
    pol = write_policy(tmp_path)
    import json
    data = json.loads(Path(pol).read_text())
    assert data["bounds"]["input_text_bytes"] == 262144