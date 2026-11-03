"""Shared shutdown latch/deadline primitive.

One monotonic shutdown state shared by signal handling, wire admission,
engine actions and runtime helpers. The first ``request()`` fixes a single
deadline ``SHUTDOWN_CONTRACT["shutdown_deadline_seconds"]`` later; later,
reentrant or concurrent requests can never extend it. The deadline is one
shared budget covering drain, cleanup, evidence writes and socket cleanup —
never a fresh budget per worker or phase.

No dependency on engine/runtime; no credentials, environment knobs or I/O.
Production constants are fixed at import time; there are no environment
overrides and no configurable production budgets. Tests may inject a
monotonic clock. ``commit(purpose)`` admits one action; ``claim(permit)``
consumes that permit once, including when submission happens after the latch.

Time semantics: the injected ``clock`` (default ``time.monotonic``) owns
deadline/checkpoint/remaining arithmetic. ``wait`` bounds its real sleep
with ``time.monotonic`` so a simulated frozen clock cannot hang an interval
wait; the shared deadline itself is always evaluated on ``clock``.
"""

from __future__ import annotations

from dataclasses import dataclass
import sys
import threading
import time
import weakref

__all__ = ["SHUTDOWN_CONTRACT", "ShutdownRequested", "Shutdown"]


class _FrozenDict(dict):
    """Plain-data mapping that blocks ordinary mutation APIs.

    Inherits ``dict`` so ``json.dumps`` and ``Mapping`` consumers treat it as
    plain data (no MappingProxyType serialization footgun), while item/
    update mutation raises ``TypeError``. Nested containers are frozen too.
    Deliberate ``dict.__setitem__`` or ``dict.update`` calls bypass this
    subclass; this prevents accidents, not hostile same-user base-class edits.
    """

    def __setitem__(self, key, value):
        raise TypeError("contract is immutable")

    def __delitem__(self, key):
        raise TypeError("contract is immutable")

    def pop(self, key, *default):
        raise TypeError("contract is immutable")

    def popitem(self):
        raise TypeError("contract is immutable")

    def clear(self):
        raise TypeError("contract is immutable")

    def setdefault(self, key, default=None):
        raise TypeError("contract is immutable")

    def update(self, *args, **kwargs):
        raise TypeError("contract is immutable")

    def __ior__(self, other):
        raise TypeError("contract is immutable")

    def __copy__(self):
        return self.snapshot()

    def __deepcopy__(self, memo):
        return self.snapshot()

    def snapshot(self):
        """Return an independently immutable plain-data copy."""
        return _deep_frozen(self)


def _deep_frozen(value):
    if isinstance(value, dict):
        return _FrozenDict({key: _deep_frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_frozen(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_frozen(item) for item in value)
    return value


def _frozen(value):
    """Freeze a contract value, refusing non-plain data fail-closed."""
    if type(value) is not dict:
        raise ValueError("contract_invalid")
    return _FrozenDict({key: _deep_frozen(item) for key, item in value.items()})


def _finite(value, name):
    if type(value) not in (int, float) \
            or value != value or value in (float("inf"), float("-inf")):
        raise ValueError(name)


SHUTDOWN_CONTRACT = _frozen({
    "version": 1,
    "supervisor_exit_seconds": 60,
    "shutdown_deadline_seconds": 50,
    "logging_environment": {"OSLogRateLimit": "64"},
})

_SHUTDOWN_DEADLINE_SECONDS = SHUTDOWN_CONTRACT["shutdown_deadline_seconds"]
_POLL_INTERVAL = 0.05  # bounded fallback polling behind wakeups


class ShutdownRequested(RuntimeError):
    """Closed refusal for normal-action admission after the latch."""


@dataclass(frozen=True, eq=False)
class _ActionPermit:
    """Immutable metadata only; it cannot execute or submit an action."""

    __slots__ = ("purpose", "_issuer", "__weakref__")

    purpose: str
    _issuer: Shutdown


class _StickyWakeup:
    """Event-like wakeup safe for same-thread signal reentry in wait/set."""

    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._set = False

    def set(self):
        with self._condition:
            self._set = True
            self._condition.notify_all()

    def wait(self, timeout):
        with self._condition:
            if not self._set:
                self._condition.wait(timeout)
            return self._set


class Shutdown:
    """Monotonic shutdown latch with one fixed, unextendable deadline."""

    def __init__(self, clock=time.monotonic):
        if sys.implementation.name != "cpython":
            raise ValueError("runtime_unsupported")
        self._clock = clock
        if not callable(self._clock):
            raise ValueError("clock_invalid")
        initial = self._clock()
        _finite(initial, "clock_invalid")
        self._lock = threading.RLock()
        self._wakeup = _StickyWakeup()
        self._request_state = {}
        self._last_now = initial
        self._clock_failed = False
        self._submission_gate = threading.Lock()
        self._permits = weakref.WeakKeyDictionary()

    @property
    def requested(self):
        with self._lock:
            return bool(self._request_state)

    @property
    def deadline(self):
        with self._lock:
            return self._request_state.get("deadline")

    def _now(self):
        with self._lock:
            if self._clock_failed:
                raise ValueError("clock_invalid")
            value = self._clock()
            try:
                _finite(value, "clock_invalid")
                if value < self._last_now:
                    raise ValueError("clock_invalid")
            except ValueError:
                self._clock_failed = True
                raise
            self._last_now = value
            return value

    def request(self):
        """Latch shutdown; the first call fixes the deadline for good.

        Never waits on operation/submission locks, never extends the first
        deadline. CPython's built-in dict.setdefault with a literal string
        key and float value publishes the first deadline in one nonoverwriting
        operation, even if a signal reenters between surrounding bytecodes.
        The wakeup uses a reentrant condition for signals arriving in wait.
        """
        with self._lock:
            if self._request_state:
                return  # already latched: the deadline can never move later
            now = self._now()
            if self._request_state:  # a clock callback may have latched reentrantly
                return
            deadline = now + _SHUTDOWN_DEADLINE_SECONDS
            try:
                _finite(deadline, "clock_invalid")
            except ValueError:
                self._clock_failed = True
                raise
            self._request_state.setdefault("deadline", deadline)
        self._wakeup.set()

    def checkpoint(self):
        """Admit normal work before shutdown; refuse it after the latch."""
        with self._lock:
            if self._request_state:
                raise ShutdownRequested("controller_stopping")
            self._now()
            if self._request_state:
                raise ShutdownRequested("controller_stopping")

    def commit(self, purpose):
        """Admit one named action at the final shutdown-open read.

        The short submission gate orders commitments. The permit carries no
        execution capability, and OS submission may happen after the latch.
        Purpose is a bounded metadata tag; RecoveryStore owns the purpose
        enum. ``claim`` must be called once for the particular submission.
        """
        if (type(purpose) is not str or not 1 <= len(purpose) <= 64
                or not "a" <= purpose[0] <= "z"
                or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                       for char in purpose)):
            raise ValueError("purpose_invalid")
        with self._submission_gate:
            with self._lock:
                if self._request_state:
                    raise ShutdownRequested("controller_stopping")
                self._now()
                if self._request_state:  # linearization: one action admitted
                    raise ShutdownRequested("controller_stopping")
                permit = _ActionPermit(purpose, self)
                self._permits[permit] = False
                return permit

    def claim(self, permit):
        """Consume this instance's committed permit once, without readmission.

        Returns its purpose metadata. The caller owns the one corresponding
        action; this method does not run it or grant another commitment.
        """
        with self._lock:
            if (type(permit) is not _ActionPermit or permit._issuer is not self
                    or permit not in self._permits):
                raise ValueError("permit_invalid")
            if self._permits[permit]:
                raise ValueError("permit_consumed")
            self._permits[permit] = True
            return permit.purpose

    def remaining(self, limit, cleanup=False):
        """Remaining budget, capped at ``limit`` seconds.

        Before the latch the normal budget is unconstrained by shutdown and
        ``limit`` is returned. After the latch, normal action admission
        (``cleanup=False``) fails closed with ``ShutdownRequested``.
        ``cleanup=True`` may spend the remaining shared budget until the
        deadline; past the deadline it refuses with ``0.0`` even for cleanup.
        """
        _finite(limit, "limit_invalid")
        if limit < 0:
            raise ValueError("limit_invalid")
        if type(cleanup) is not bool:
            raise ValueError("cleanup_invalid")
        with self._lock:
            if not self._request_state:
                self._now()
            if not self._request_state:
                return limit
            if not cleanup:
                raise ShutdownRequested("controller_stopping")
            deadline = self._request_state["deadline"]
        remaining = deadline - self._now()
        if remaining <= 0:
            return 0.0
        return min(remaining, limit)

    def wait(self, interval, cleanup=False):
        """Bounded wait observing the shared shutdown state.

        Returns ``True`` when shutdown says stop, ``False`` when the interval
        elapsed without shutdown. Before the latch this is a normal-operation
        wait: it sleeps at most ``interval`` (bounded real time) but wakes
        immediately when the latch fires. After the latch, ``cleanup=False``
        is closed normal admission and raises ``ShutdownRequested``;
        ``cleanup=True`` sleeps at most the interval and shared budget,
        returning ``True`` only at the shared deadline. Uses sticky
        wakeups before the latch and never holds the state lock while waiting.
        """
        _finite(interval, "interval_invalid")
        if interval <= 0:
            raise ValueError("interval_invalid")
        if type(cleanup) is not bool:
            raise ValueError("cleanup_invalid")
        with self._lock:
            if self._request_state and not cleanup:
                raise ShutdownRequested("controller_stopping")
        end = time.monotonic() + interval
        while True:
            with self._lock:
                requested = bool(self._request_state)
                deadline = self._request_state.get("deadline")
                if requested and not cleanup:
                    return True
                now = self._now()
            if requested and deadline - now <= 0:
                return True
            real_remaining = end - time.monotonic()
            if real_remaining <= 0:
                return False
            delay = min(real_remaining, _POLL_INTERVAL)
            if requested:
                # The sticky wakeup remains set forever; sleeping on it spins.
                time.sleep(min(delay, deadline - now))
            else:
                self._wakeup.wait(delay)
