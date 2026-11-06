"""Offline quotation regressions; never execute the command strings."""
import importlib.util
import os
import shlex
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from test_lifecycle_guard import (FIXTURE_PID, FIXTURE_PORT, MODULE_PATH,
                                  guard, write_policy)


@pytest.fixture
def scope(tmp_path, monkeypatch):
    parent = Path(os.environ.get('TMPDIR') or tempfile.gettempdir()).resolve()
    owned = Path(tempfile.mkdtemp(prefix='guard-quoted-', dir=parent))
    module_key = f'guard_quoted_{uuid.uuid4().hex}'
    fresh_guard = None
    captured_worker = None
    attempted = []
    try:
        monkeypatch.setenv('XDG_STATE_HOME', str(owned / 'state'))
        policy = write_policy(tmp_path)
        spec = importlib.util.spec_from_file_location(module_key, MODULE_PATH)
        assert spec is not None and spec.loader is not None
        fresh_guard = importlib.util.module_from_spec(spec)
        sys.modules[module_key] = fresh_guard
        spec.loader.exec_module(fresh_guard)

        def forbidden(*args, **kwargs):
            attempted.append(True)
            raise AssertionError('native operation reached in pure matcher test')

        monkeypatch.setattr(fresh_guard.subprocess, 'run', forbidden)
        monkeypatch.setattr(fresh_guard.subprocess, 'Popen', forbidden)
        monkeypatch.setattr(fresh_guard.os, 'kill', forbidden)
        monkeypatch.setattr(fresh_guard.os, 'killpg', forbidden)
        snapshot = fresh_guard.Snapshot(
            pids=frozenset({FIXTURE_PID}), pgids=frozenset({FIXTURE_PID}),
            comms=frozenset({'litellm'}), args=('litellm --port 4000',))
        seen = []
        original = fresh_guard._analyze_code_text

        def observe(code, policy, get_snapshot):
            seen.append(code)
            return original(code, policy, get_snapshot)

        monkeypatch.setattr(fresh_guard, '_analyze_code_text', observe)
        discovered = []

        def discover(port):
            discovered.append(port)
            return snapshot

        assert fresh_guard.sink_start()
        captured_worker = fresh_guard._sink_worker
        assert captured_worker is not None

        def evaluate(command):
            return fresh_guard.evaluate('terminal', {'command': command},
                                        policy, discover=discover)

        yield evaluate, seen, discovered
    finally:
        cleanup_errors = []
        before_shutdown = (getattr(fresh_guard, '_sink_worker', None)
                           if fresh_guard is not None else None)
        had_worker = captured_worker is not None or before_shutdown is not None
        shutdown_ok = True
        if fresh_guard is not None:
            if hasattr(fresh_guard, 'sink_flush'):
                try:
                    assert fresh_guard.sink_flush(1.0) is True
                except Exception as exc:
                    cleanup_errors.append(f'flush: {exc!r}')
            if hasattr(fresh_guard, 'sink_shutdown'):
                try:
                    result = fresh_guard.sink_shutdown(5.0)
                    if had_worker:
                        shutdown_ok = bool(result)
                except Exception as exc:
                    shutdown_ok = False
                    cleanup_errors.append(f'shutdown: {exc!r}')
        current_worker = (getattr(fresh_guard, '_sink_worker', None)
                          if fresh_guard is not None else None)
        dead = all(worker is None or not worker.is_alive()
                   for worker in (captured_worker, before_shutdown,
                                  current_worker))
        if dead:
            sys.modules.pop(module_key, None)
            shutil.rmtree(owned)
        assert attempted == [], 'a swallowed native attempt is still a failure'
        assert dead, f'worker still alive; fixture retained at {owned}'
        assert shutdown_ok, 'worker shutdown did not complete'
        assert cleanup_errors == [], cleanup_errors


@pytest.mark.parametrize("program", [
    "print(1)",
    "print(1 if True else 0)",
    "print('then', 'do', 'else', 'done', 'fi', 'esac', ';;')",
    "print([x if x else 0 for x in [1, 0]])",
    'print("don\'t alter else")',
    'print(f"done {1 if True else 0}")',
])
@pytest.mark.parametrize("wrapper_depth", [0, 1, 2])
def test_quoted_inline_python_is_inspected_byte_identically(scope, program,
                                                           wrapper_depth):
    evaluate, seen, _ = scope
    command = "python3 -c " + shlex.quote(program)
    for _ in range(wrapper_depth):
        command = "bash -c " + shlex.quote(command)
    assert evaluate(command) is None
    assert seen == [program], "analysis must not change quoted program bytes"


@pytest.mark.parametrize("command", [
    f"if true; then kill -TERM {FIXTURE_PID}; fi",
    f"while true; do kill -TERM {FIXTURE_PID}; done",
    f"case x in x) kill -TERM {FIXTURE_PID};; esac",
    "bash -c " + shlex.quote(f"if true; then kill -TERM {FIXTURE_PID}; fi"),
    'printf "%s" "$(' + f"if true; then kill -TERM {FIXTURE_PID}; fi" + ')"',
])
def test_real_wrapped_signal_intent_remains_vetoed(scope, command):
    evaluate, _, discovered = scope
    decision = evaluate(command)
    assert decision is not None
    assert decision["action"] == "block"
    assert decision["category"] == "VETO"
    assert decision["log_code"] == "LGV-001"
    assert discovered == [FIXTURE_PORT], "veto must reach protected-target analysis"


@pytest.mark.parametrize("command", [
    '''python3 -c "print('$(printf '\"')' if True else 0)"''',
    '''echo "$(printf '\"') else"''',
    '''echo "`printf '\"'` else"''',
    '''echo "$(if true; then printf '%s' x; fi) else"''',
])
def test_nested_substitution_quotes_are_preserved(scope, command):
    # This is a lexical preservation invariant, not permission to execute a
    # program containing dynamic substitutions or an assertion of shlex support.
    assert guard._normalize_control_flow(command) == command


@pytest.mark.parametrize("command", [
    "echo ''else",
    "echo x\\ else",
    "echo $(printf x)else",
    "echo `printf x`else",
])
def test_word_prefix_keeps_keyword_suffix_as_data(scope, command):
    assert guard._normalize_control_flow(command) == command


def _escaped_shell_word(text):
    """Encode one argument without quoting; never invoke a shell."""
    return "".join(char if char.isalnum() or char == "_" else "\\" + char
                   for char in text)


@pytest.mark.parametrize("program", [
    'print("else")',
    'print("then")',
    'print("done")',
    'print("do")',
    'print("fi")',
    'print("esac")',
    'print(1 if True else 0)',
])
def test_escaped_inline_python_is_inspected_byte_identically(scope, program):
    evaluate, seen, _ = scope
    assert evaluate("python3 -c " + _escaped_shell_word(program)) is None
    assert seen == [program]


def test_escaped_inline_signal_code_is_still_inspected(scope):
    evaluate, seen, _ = scope
    program = f"import os; os.kill({FIXTURE_PID}, 15)"
    decision = evaluate("python3 -c " + _escaped_shell_word(program))
    assert seen == [program], "escaping must not hide code from inspection"
    assert decision is not None
    assert decision["action"] == "block"
    assert decision["category"] == "VETO"
    assert decision["log_code"] == "LGV-001"
