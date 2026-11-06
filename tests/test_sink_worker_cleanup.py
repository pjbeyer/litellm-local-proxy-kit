import sys
import threading
import types
import uuid

import pytest

import test_sink_worker as worker_tests


class FakeThread:
    def __init__(self, alive):
        self.alive = alive
        self.join_calls = []

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)
        if not self.alive:
            raise AssertionError("joined a dead or unstarted fake thread")


def make_fixture(tmp_path, captured, after_shutdown):
    owned = tmp_path / ("owned_" + uuid.uuid4().hex)
    owned.mkdir()
    key = "test_sink_worker_cleanup_" + uuid.uuid4().hex
    guard = types.SimpleNamespace(_sink_worker=captured)

    def shutdown(timeout):
        guard._sink_worker = after_shutdown
        return True

    guard.sink_shutdown = shutdown
    fixture = worker_tests.SinkWorkerTest(methodName="runTest")
    fixture._tracked_dirs = [str(owned)]
    fixture._worker_stopped = False
    fixture._fresh_module_keys = [key]
    fixture._persist_ownership_errors = []
    fixture._worker_refs = {id(captured): captured}
    fixture.mod = guard
    fixture.stall = threading.Event()
    sys.modules[key] = types.ModuleType(key)
    return fixture, owned, key


def assert_retained(fixture, owned, key):
    assert fixture._worker_stopped is False
    fixture._cleanup_tracked_dirs()
    fixture._cleanup_fresh_modules()
    assert owned.exists()
    assert key in sys.modules


def test_captured_live_worker_survives_cleared_current(tmp_path):
    captured = FakeThread(alive=True)
    fixture, owned, key = make_fixture(tmp_path, captured, None)
    try:
        with pytest.raises(AssertionError, match="sink worker still alive"):
            fixture._bounded_teardown()
        assert len(captured.join_calls) == 1
        assert_retained(fixture, owned, key)
    finally:
        sys.modules.pop(key, None)


def test_replacement_live_worker_is_checked(tmp_path):
    captured = FakeThread(alive=False)
    current = FakeThread(alive=True)
    fixture, owned, key = make_fixture(tmp_path, captured, current)
    try:
        with pytest.raises(AssertionError, match="sink worker still alive"):
            fixture._bounded_teardown()
        assert captured.join_calls == []
        assert len(current.join_calls) == 1
        assert_retained(fixture, owned, key)
    finally:
        sys.modules.pop(key, None)


def test_dead_captured_and_current_allow_cleanup(tmp_path):
    captured = FakeThread(alive=False)
    current = FakeThread(alive=False)
    fixture, owned, key = make_fixture(tmp_path, captured, current)
    try:
        fixture._bounded_teardown()
        assert fixture._worker_stopped is True
        assert captured.join_calls == []
        assert current.join_calls == []
        fixture._cleanup_tracked_dirs()
        fixture._cleanup_fresh_modules()
        assert not owned.exists()
        assert key not in sys.modules
    finally:
        sys.modules.pop(key, None)


@pytest.mark.parametrize("clear_current", [False, True],
                         ids=["failed-start", "never-started"])
def test_dead_unstarted_fake_is_not_joined(tmp_path, clear_current):
    fake = FakeThread(alive=False)
    after_shutdown = None if clear_current else fake
    fixture, owned, key = make_fixture(tmp_path, fake, after_shutdown)
    try:
        fixture._bounded_teardown()
        assert fixture._worker_stopped is True
        assert fake.join_calls == []
        fixture._cleanup_tracked_dirs()
        fixture._cleanup_fresh_modules()
        assert not owned.exists()
        assert key not in sys.modules
    finally:
        sys.modules.pop(key, None)
