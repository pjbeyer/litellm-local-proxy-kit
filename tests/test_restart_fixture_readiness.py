"""Unit tests for the fixture socket-readiness probe (no real sockets used).

The parent proved the race mechanically: FixtureRig's constructor returned once
the socket *file* existed while direct connects still got ECONNREFUSED,
because wire.serve binds the path before it calls listen(). _wait_for_unix_listener
replaced the file-existence poll with an actual AF_UNIX connect probe. These
tests pin that contract with fully mocked socket/thread/clock objects -- no
processes, no real sockets, no credentials, no service activity -- and assert
in every case that the outcome came from connect(), never from file existence.
They also pin the probe's time contract (each attempt's settimeout is capped
by the remaining deadline, with no per-attempt floor that could exceed the
total budget) and the constructor's wiring (the real FixtureRig.__init__ must
start the server thread and then block on the readiness probe) with every
module collaborator mocked: no disk, no thread, no process, no socket.
"""

import errno
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_restart_lifecycle_fixtures as fixture_module
from test_restart_lifecycle_fixtures import _wait_for_unix_listener

_FIXTURE_MODULE = "test_restart_lifecycle_fixtures"


class _FakeClock:
    """Monotonic fake: advances a fixed step per call."""

    def __init__(self, start=0.0, step=0.01):
        self.now = start
        self.step = step
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.now += self.step
        return self.now


def _refused(path):
    return OSError(errno.ECONNREFUSED, "connection refused", str(path))


def _missing(path):
    return OSError(errno.ENOENT, "no such file or directory", str(path))


class _ThreadProxy:
    """Forward-only shim exposing just the attributes the helper reads."""

    def __init__(self, alive=True):
        self._alive = alive
        self.is_alive_calls = 0

    def is_alive(self):
        self.is_alive_calls += 1
        return self._alive


class WaitForUnixListenerTest(unittest.TestCase):
    """Mocked-clock, mocked-socket unit tests for the readiness probe."""

    def setUp(self):
        # A socket path that plausibly "exists" the whole time in every test:
        # readiness must be proven by connect(), never by file existence.
        self.path = Path("fixture-probe.sock")
        self.patchers = []

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()

    def _patch_time(self, clock):
        mono = mock.patch("%s.time.monotonic" % _FIXTURE_MODULE,
                          side_effect=clock)
        sleep = mock.patch("%s.time.sleep" % _FIXTURE_MODULE,
                           side_effect=lambda _s: None)
        self.patchers += [mono, sleep]
        sleep_mock = sleep.start()
        mono.start()
        return sleep_mock

    def _patch_socket(self, sock):
        factory = mock.patch("%s.socket.socket" % _FIXTURE_MODULE,
                             side_effect=lambda *a, **k: sock)
        self.patchers.append(factory)
        factory.start()
        return sock

    def _make_sock(self, outcomes):
        """outcomes: list of None (success) or OSError instances to raise."""
        sock = mock.MagicMock(spec=socket.socket)
        sock.connect = mock.MagicMock(side_effect=list(outcomes))
        sock.__enter__ = mock.MagicMock(return_value=sock)
        sock.__exit__ = mock.MagicMock(return_value=False)
        return sock

    # -- 1. both pre-listen errnos retried, "file" existing the whole time -
    def test_refusal_then_success_despite_file_existing(self):
        """ENOENT then ECONNREFUSED are retried; readiness lands on connect()."""
        outcomes = [_missing(self.path), _refused(self.path), None]
        sock = self._patch_socket(self._make_sock(outcomes))
        clock = _FakeClock()
        sleep = self._patch_time(clock)
        thread = _ThreadProxy(alive=True)
        _wait_for_unix_listener(self.path, thread, timeout=10.0)
        self.assertEqual(sock.connect.call_count, 3)
        self.assertEqual(sock.connect.call_args[0][0], str(self.path))
        self.assertEqual(sleep.call_count, 2)
        self.assertGreaterEqual(thread.is_alive_calls, 1)

    # -- 2. EPERM propagates immediately, no retry --------------------------
    def test_eperm_propagates_immediately_without_retry(self):
        sock = self._patch_socket(self._make_sock([_refused(self.path), None]))
        clock = _FakeClock()
        sleep = self._patch_time(clock)
        thread = _ThreadProxy(alive=True)
        denied = OSError(errno.EPERM, "operation not permitted", str(self.path))
        sock.connect.side_effect = [_refused(self.path), denied]
        with self.assertRaises(OSError) as raised:
            _wait_for_unix_listener(self.path, thread, timeout=10.0)
        self.assertEqual(raised.exception.errno, errno.EPERM)
        self.assertEqual(sock.connect.call_count, 2)
        sleep.assert_called_once()

    # -- 3. EACCES propagates immediately, no retry, no sleep ---------------
    def test_eacces_propagates_immediately_without_retry(self):
        sock = self._patch_socket(
            self._make_sock([OSError(errno.EACCES, "permission denied",
                                      str(self.path))]))
        clock = _FakeClock()
        sleep = self._patch_time(clock)
        thread = _ThreadProxy(alive=True)
        with self.assertRaises(OSError) as raised:
            _wait_for_unix_listener(self.path, thread, timeout=10.0)
        self.assertEqual(raised.exception.errno, errno.EACCES)
        self.assertEqual(sock.connect.call_count, 1)
        sleep.assert_not_called()

    # -- 4. dead server thread fails loud, no endless polling -------------
    def test_dead_thread_fails_loud(self):
        # Intentionally zero connects: a dead thread must be detected before
        # any probe attempt, so the socket factory is never reached.
        sock = self._patch_socket(self._make_sock([_refused(self.path)]))
        clock = _FakeClock()
        self._patch_time(clock)
        thread = _ThreadProxy(alive=False)
        with self.assertRaises(AssertionError):
            _wait_for_unix_listener(self.path, thread, timeout=10.0)
        self.assertEqual(sock.connect.call_count, 0)

    # -- 5. bounded deadline: eternal refusal exhausts the timeout ---------
    def test_eternal_refusal_is_bounded_by_deadline(self):
        sock = self._patch_socket(
            self._make_sock([OSError(errno.ECONNREFUSED, "refused")] * 50))
        clock = _FakeClock(step=4.0)
        sleep = self._patch_time(clock)
        thread = _ThreadProxy(alive=True)
        with self.assertRaises(AssertionError) as raised:
            _wait_for_unix_listener(self.path, thread, timeout=10.0)
        self.assertIn("never accepted", str(raised.exception))
        # Deadline is monotonic-bounded: the fake clock advances 4s per call,
        # so the loop must exhaust 10s well before the 50-outcome pool.
        self.assertLessEqual(clock.now, 22.0)
        self.assertLess(sock.connect.call_count, 50)
        self.assertGreaterEqual(thread.is_alive_calls, 1)
        # Every refused connect was followed by exactly one bounded sleep.
        self.assertEqual(sleep.call_count, sock.connect.call_count)

    # -- 6. already-listening socket: single immediate successful connect -
    def test_already_listening_succeeds_on_first_connect(self):
        sock = self._patch_socket(self._make_sock([None]))
        clock = _FakeClock()
        sleep = self._patch_time(clock)
        thread = _ThreadProxy(alive=True)
        _wait_for_unix_listener(self.path, thread, timeout=10.0)
        self.assertEqual(sock.connect.call_count, 1)
        self.assertEqual(sock.connect.call_args[0][0], str(self.path))
        self.assertEqual(sock.settimeout.call_count, 1)
        sleep.assert_not_called()

    # -- 7. per-attempt settimeout never exceeds the remaining budget -----
    def test_probe_timeout_never_exceeds_remaining_budget(self):
        """settimeout is capped by remaining time, with no per-attempt floor.

        A 10ms total budget with the clock advancing 4ms per call leaves 6ms
        at the first probe, so every settimeout must be in (0, 6ms]. The old
        max(0.05, ...) floor spent 50ms per attempt against a 10ms budget.
        """
        sock = self._patch_socket(self._make_sock([_refused(self.path)] * 5))
        clock = _FakeClock(step=0.004)
        self._patch_time(clock)
        thread = _ThreadProxy(alive=True)
        with self.assertRaises(AssertionError):
            _wait_for_unix_listener(self.path, thread, timeout=0.01)
        values = [call.args[0] for call in sock.settimeout.call_args_list]
        self.assertTrue(values)
        for value in values:
            self.assertGreater(value, 0)
            self.assertLessEqual(value, 0.006 + 1e-12)


class FixtureRigConstructorWiringTest(unittest.TestCase):
    """The real FixtureRig.__init__ must start the thread, then wait on it.

    Every collaborator the constructor touches is patched on the fixture
    module, so construction performs no disk, thread, process, or socket
    operation. The readiness helper's side effect proves it runs strictly
    after the server thread was started exactly once.
    """

    def test_constructor_waits_for_listener(self):
        root = mock.MagicMock(name="fixture-root")
        children = {}

        def _divide(item):
            if item not in children:
                children[item] = mock.MagicMock(name="root/%s" % item)
            return children[item]

        root.__truediv__.side_effect = _divide

        backend = mock.MagicMock(name="backend")
        backend.children = []
        mockthread = mock.MagicMock(name="server-thread")
        fake_threading = mock.MagicMock(name="threading")
        fake_threading.Thread.return_value = mockthread

        helper = mock.MagicMock(name="_wait_for_unix_listener")

        def _ready(sock, thread):
            mockthread.start.assert_called_once_with()

        helper.side_effect = _ready

        fake_path = mock.MagicMock(name="Path")
        fake_path.return_value = root

        with mock.patch.multiple(
                fixture_module,
                Path=fake_path,
                tempfile=mock.MagicMock(name="tempfile"),
                _fixture_base=mock.MagicMock(name="_fixture_base"),
                FakeLaunchctlBackend=mock.MagicMock(
                    name="FakeLaunchctlBackend", return_value=backend),
                Controller=mock.MagicMock(name="Controller"),
                FixtureLifecycleAdapter=mock.MagicMock(
                    name="FixtureLifecycleAdapter"),
                _spawn=mock.MagicMock(name="_spawn"),
                threading=fake_threading,
                _wait_for_unix_listener=helper):
            rig = fixture_module.FixtureRig()

        mockthread.start.assert_called_once_with()
        helper.assert_called_once_with(rig.sock, rig.thread)


if __name__ == "__main__":
    unittest.main()