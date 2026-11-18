"""T019 startup orchestration: fake platform adapter + real worker fixtures.

Offline only: launchctl is faked with an explicit adapter that records
exact argv and simulates the KeepAlive lifecycle by running the REAL
canary-worker entrypoint as double-forked retained children (kernel
PID-1 parentage, distinct births). No live launchd call is made;
responses are labeled fake by construction.
"""

import json
import os
import plistlib
import sys
import time
from pathlib import Path

import pytest

# Canary-lane tests spawn a real worker subprocess that reads the darwin kernel
# boot identity; it cannot inherit the parent-stub on Linux.
DARWIN_ONLY = pytest.mark.skipif(sys.platform != "darwin",
                                 reason="canary lane binds the darwin kernel boot identity")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'core'))
sys.path.insert(0, str(ROOT/'tests'))
from restart_control.engine import Fault
from restart_control import canary
from restart_control.shutdown import Shutdown
from restart_control.startup import run_startup_canary, _Lane
from restart_lifecycle_adapter import FixtureLifecycleAdapter
from test_restart_runtime import runtime, private_root  # noqa: F401


class FakePlatform:
    """Explicit fake launchd adapter: fixed argv, real worker children.

    ``bootstrap`` double-forks the real canary-worker entrypoint so the
    retained child is genuinely reparented to PID 1 (launchd parentage),
    simulating runs=1. When that child exits (the worker consumed the
    exit marker), KeepAlive semantics spawn a second distinct child
    (runs=2) that stays until ``bootout`` sends SIGTERM and waits for
    the cooperative exit. ``print`` returns a fake launchctl print
    payload with parity fields.
    """

    def __init__(self, runtime_fixture, policy_path, state_dir):
        self.calls = []
        self.jobs = {}
        self.runtime = runtime_fixture
        self.policy_path = policy_path
        self.state_dir = state_dir
        self.entrypoint = ROOT/'core'/'restart_controller.py'
        self._children = []  # (pid, runs) in lifecycle order
        self._label_runs = {}

    # -- child simulation ---------------------------------------------------

    def _spawn_worker(self, label, runs):
        """Double-fork the real canary-worker entrypoint; return identity."""
        nonce = label.rsplit('.', 1)[1]
        argv = [sys.executable, '-I', str(self.entrypoint), '--policy',
                str(self.policy_path), 'canary-worker', nonce]
        env = {'HOME': str(self.state_dir), 'PATH': os.defpath,
               'TMPDIR': os.environ['TMPDIR']}
        # Double fork: the intermediate exits immediately, the worker is
        # reparented to PID 1 — the kernel-observed launchd parentage.
        pid = os.fork()
        if pid == 0:
            try:
                inner = os.fork()
                if inner == 0:
                    os.execve(sys.executable, argv, env)
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.waitpid(pid, 0)
        # The worker writes incarnation-N.json itself; wait for it and
        # read the true kernel identity from the receipt.
        scope = Path(self.state_dir) / 'canary' / nonce
        deadline = time.monotonic() + 20
        receipt = None
        while time.monotonic() < deadline:
            path = scope / f'incarnation-{runs}.json'
            if path.exists():
                try:
                    receipt = json.loads(path.read_text())
                    break
                except Exception:
                    pass
            time.sleep(0.05)
        if receipt is None:
            raise RuntimeError('fake worker never recorded its identity')
        identity = receipt['identity']
        self._children.append((identity['pid'], runs))
        self._label_runs[label] = runs
        return identity

    # -- fake launchctl ---------------------------------------------------

    def execute(self, argv, timeout):
        self.calls.append(list(argv))
        verb = argv[1]
        if verb == 'bootstrap':
            job = plistlib.loads(Path(argv[3]).read_bytes())
            if job['Label'] in self.jobs:
                return 5, b'bootstrap: service already loaded'
            self.jobs[job['Label']] = job
            if job.get('RunAtLoad'):
                self._spawn_worker(job['Label'], 1)
            return 0, b''
        if verb == 'bootout':
            label = argv[2].split('/', 2)[2]
            if label not in self.jobs:
                return 3, b'bootout: unknown service'
            # SIGTERM the current live worker (cooperative exit) and
            # prove its disappearance before reporting success.
            import signal
            for pid, runs in list(self._children):
                if runs == self._label_runs.get(label):
                    try:
                        os.kill(pid, signal.SIGTERM)
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            try:
                                os.kill(pid, 0)
                                time.sleep(0.05)
                            except OSError:
                                break
                    except OSError:
                        pass
            del self.jobs[label]
            return 0, b''
        if verb == 'print':
            label = argv[2].split('/', 2)[2]
            runs = self._label_runs.get(label)
            if label not in self.jobs or runs is None:
                # launchctl's real not-found signature: exit 113 with the
                # fixed "Could not find service" text — the only nonzero
                # observation the runtime classifies as proven absence.
                return 113, b'Could not find service'
            job = self.jobs[label]
            live = self._live_pid(label)
            env = '\n'.join('\t\t' + k + ' => ' + v for k, v in
                           job['EnvironmentVariables'].items())
            args = '\n'.join('\t\t' + a for a in job['ProgramArguments'])
            payload = (f'gui/{os.getuid()}/{label} = {{\n'
                       f'\tpath = {self._plist_path(label)}\n'
                       f'\tprogram = {job["ProgramArguments"][0]}\n'
                       f'\tworking directory = {job["WorkingDirectory"]}\n'
                       f'\targuments = {{\n{args}\n\t}}\n'
                       f'\tenvironment = {{\n{env}\n\t}}\n'
                       f'\tminimum runtime = {job["ThrottleInterval"]}\n'
                       f'\texit timeout = {job["ExitTimeOut"]}\n'
                       f'\tpid = {live}\n'
                       f'\truns = {runs}\n}}\n')
            return 0, payload.encode()
        return 1, b'fake: unsupported'

    def _live_pid(self, label):
        """The PID of the currently live worker for the label."""
        runs = self._label_runs.get(label)
        for pid, r in reversed(self._children):
            if r == runs:
                try:
                    os.kill(pid, 0)
                    return pid
                except OSError:
                    continue
        raise RuntimeError('fake: no live worker for label')

    def worker_exited(self, runs):
        """Advance KeepAlive: the runs=1 child exited; spawn runs=2."""
        label = next(iter(self.jobs))
        self._spawn_worker(label, 2)

    def _plist_path(self, label):
        return str(Path(self.state_dir) / 'canary' /
                   label.rsplit('.', 1)[1] / (label + '.plist'))


class _FakeController:
    """Minimal controller shim: epoch + fixture journal-backed recovery."""

    def __init__(self):
        self.epoch = 'e' * 32
        self.recovery = FixtureLifecycleAdapter(namespace='restart_control')


def _arm(runtime_fixture):
    facts = runtime_fixture.binding_facts()
    return canary.arm_canary(runtime_fixture.state, facts)


def _make_lane(runtime_fixture, fake, arm, shutdown=None):
    """Build a _Lane with the runtime's helpers routed to the fake.

    Callers mirror the serve order themselves: the arm is one-time
    consumed into its private scope BEFORE the lane runs (the real
    worker validates the consumed scope — its own ``intent.json`` and
    the derived canary plist — before recording anything).
    """
    shutdown = shutdown or Shutdown()
    scope = canary.canary_scope(runtime_fixture.state, arm['nonce'])
    controller = _FakeController()
    lane = _Lane(runtime_fixture, controller, shutdown, arm, scope)

    def fake_capture(argv, timeout=90, env=None, control=False, purpose=None):
        status, _ = fake.execute(argv, timeout)
        if status != 0:
            raise Fault('service_control_denied' if control else 'gate_failed')

    def fake_observe(argv, timeout, env=None, control=False):
        return fake.execute(argv, timeout)

    def fake_job_state(label, expected=None, expected_path=None, timeout=10):
        status, raw = fake.execute(['/bin/launchctl', 'print',
                                    f'gui/{os.getuid()}/' + label], 10)
        if status:
            return None
        import re
        text = raw.decode('utf-8', 'replace')

        def field(key):
            match = re.search(r'(?m)^\s*' + re.escape(key) + r' = (.+)$', text)
            return match.group(1).strip() if match else None
        pid, runs = field('pid'), field('runs')
        from restart_control.process_identity import process_identity
        identity = None
        if pid and pid.isdigit():
            try:
                full = process_identity(int(pid))
                identity = {k: full[k] for k in ('pid', 'uid', 'birth')}
            except Exception:
                identity = None
        argmatch = re.search(r'(?ms)^\targuments = \{\n(.*?)^\t\}', text)
        args = ([s.strip() for s in argmatch.group(1).splitlines() if s.strip()]
                if argmatch else None)
        envmatch = re.search(r'(?ms)^\tenvironment = \{\n(.*?)^\t\}', text)
        actual_env = {}
        if envmatch:
            for line in envmatch.group(1).splitlines():
                if ' => ' in line:
                    key, value = line.strip().split(' => ', 1)
                    actual_env[key] = value
        parity = (args == expected['ProgramArguments']
                  and all(actual_env.get(k) == v for k, v in
                          expected['EnvironmentVariables'].items())
                  and field('minimum runtime') ==
                      str(expected['ThrottleInterval']))
        return {'identity': identity,
                'runs': int(runs) if runs and runs.isdigit() else None,
                'parity': parity}

    # Route the tracked control/observe calls to the fake platform. The
    # real path journals through _capture; the offline fake is not a
    # child, so the lane's journal surface is the adapter's operation.
    runtime_fixture._capture = fake_capture
    runtime_fixture._observe = fake_observe
    runtime_fixture._job_state = fake_job_state
    # The fixture runtime enters with a placeholder active operation; the
    # lane must own the exact active context for its own attempt.
    runtime_fixture._active_operation = None
    return lane


def test_no_arm_means_zero_platform_calls(runtime, private_root):
    """No arm: the startup canary performs zero service-control calls."""
    fake = FakePlatform(runtime, runtime.policy_path, runtime.state)
    result = run_startup_canary(runtime, _FakeController(), Shutdown())
    assert result is None
    assert fake.calls == []


def test_arm_binding_mismatch_leaves_diagnostic_and_no_calls(runtime, private_root):
    """An arm bound to a different generation blocks before any call."""
    fake = FakePlatform(runtime, runtime.policy_path, runtime.state)
    wrong = canary.arm_canary(runtime.state,
                              {'policy_digest': 'b' * 64,
                               'controller_digest': 'b' * 64,
                               'controller_plist_digest': 'b' * 64})
    result = run_startup_canary(runtime, _FakeController(), Shutdown())
    assert result['outcome'] == 'canary_blocked'
    assert result['code'] == 'canary_binding_mismatch'
    assert fake.calls == []
    diagnostic = json.loads((runtime.state/'canary'/'diagnostic.json').read_text())
    assert diagnostic['nonce'] == wrong['nonce']
    # The arm itself is untouched: never silently deleted.
    assert (runtime.state/'canary'/'intent.json').exists()


@DARWIN_ONLY
def test_full_pass_lane_with_real_worker_children(runtime, private_root):
    """The full lane passes: bootstrap → load → exit → KeepAlive → bootout.

    Real double-forked canary-worker children (kernel PID-1 parentage,
    distinct births), fake launchctl with exact derived argv/label
    assertions, and no retry anywhere.
    """
    arm = _arm(runtime)
    fake = FakePlatform(runtime, runtime.policy_path, runtime.state)
    # Mirror the serve order: consume the arm into its private scope
    # before the lane runs (the real worker validates the consumed
    # scope before recording its identity).
    canary.consume_arm(runtime.state, operator_uid=os.getuid())
    lane = _make_lane(runtime, fake, arm)
    # KeepAlive: when the runs=1 worker has exited (marker consumed),
    # the fake spawns the second distinct incarnation before the lane
    # observes runs=2.
    original_keepalive = lane._keepalive

    def keepalive_step():
        fake.worker_exited(1)
        return original_keepalive()
    lane._keepalive = keepalive_step
    result = lane.run()
    assert result['outcome'] == 'canary_passed', result
    # Exact derived label and argv: the canary job is never caller-set.
    label = 'local.restart-canary.' + arm['nonce']
    assert result['label'] == label
    argv0 = fake.calls[0]
    assert argv0[:3] == ['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}']
    assert argv0[3].endswith(label + '.plist')
    bootouts = [c for c in fake.calls if c[1] == 'bootout']
    assert len(bootouts) == 1
    assert bootouts[0] == ['/bin/launchctl', 'bootout',
                           f'gui/{os.getuid()}/' + label]
    assert len([c for c in fake.calls if c[1] == 'bootstrap']) == 1
    # The journal carried the attempt: pending → running → passed.
    assert lane.controller.recovery.transitions == [
        'begin', 'canary_running', 'canary_passed']
    # Both incarnations were REAL children with distinct births.
    scope = canary.canary_scope(runtime.state, arm['nonce'])
    first = json.loads((scope/'incarnation-1.json').read_text())
    second = json.loads((scope/'incarnation-2.json').read_text())
    assert first['identity']['birth'] != second['identity']['birth']
    assert first['identity']['uid'] == os.getuid()
    # Evidence + detail written under the fixed namespace, 0600.
    evidence_dir = runtime.state/'canary'/'evidence'
    detail_dir = runtime.state/'canary'/'detail'
    assert list(evidence_dir.iterdir()) and list(detail_dir.iterdir())
    for path in (*evidence_dir.iterdir(), *detail_dir.iterdir()):
        assert (path.stat().st_mode & 0o777) == 0o600
    # The evidence envelope validates against the frozen schema.
    evidence = result['evidence']
    assert evidence['scope'] == 'intended-controller'
    assert evidence['nonce'] == arm['nonce']
    assert evidence['incarnations'][0]['runs'] == 1
    assert evidence['incarnations'][1]['runs'] == 2
    assert all(evidence['checks'].values())
    # The first worker consumed its exit marker (cooperative, one-time)
    # and the second worker's SIGTERM latch receipt is durable.
    assert not (scope/'exit.marker').exists()
    assert (scope/'latch.json').exists()


def test_refused_bootstrap_blocks_without_retry(runtime, private_root):
    """A refused bootstrap leaves canary_blocked, one call, no retry."""
    arm = _arm(runtime)
    fake = FakePlatform(runtime, runtime.policy_path, runtime.state)

    def refuse(argv, timeout):
        fake.calls.append(list(argv))
        return 5, b'bootstrap: service already loaded'
    fake.execute = refuse
    lane = _make_lane(runtime, fake, arm)
    result = lane.run()
    assert result['outcome'] == 'canary_blocked'
    assert result['code'] == 'service_control_denied'
    assert len(fake.calls) == 1
    assert lane.controller.recovery.transitions == [
        'begin', 'canary_running', 'canary_blocked']
    scope = canary.canary_scope(runtime.state, arm['nonce'])
    diagnostic = json.loads((scope/'diagnostic.json').read_text())
    assert diagnostic['code'] == 'service_control_denied'


def test_shutdown_latch_blocks_lane(runtime, private_root):
    """A latched shutdown during the lane blocks, never escalates."""
    arm = _arm(runtime)
    fake = FakePlatform(runtime, runtime.policy_path, runtime.state)
    lane = _make_lane(runtime, fake, arm)
    lane.shutdown.request()
    result = lane.run()
    assert result['outcome'] == 'canary_blocked'
    assert result['code'] == 'controller_stopping'
    assert fake.calls == []
    # The latch fired before the journal begin: no attempt was created.
    assert lane.controller.recovery.transitions == []
    scope = canary.canary_scope(runtime.state, arm['nonce'])
    diagnostic = json.loads((scope/'diagnostic.json').read_text())
    assert diagnostic['code'] == 'controller_stopping'


@DARWIN_ONLY
def test_registration_latency_polls_not_blocks(runtime, private_root):
    """Transient print absences before first sighting poll, not block.

    Live gate-4 finding: launchctl bootstrap returns before launchd
    makes the job queryable, so the first print after bootstrap can
    carry the real not-found signature (exit 113) while the worker is
    already starting. The lane must poll to the step deadline instead
    of recording canary_job_absent against a job that is coming up.
    Absence stays PROOF in _cleanup (the job must be gone there).
    """
    arm = _arm(runtime)
    fake = FakePlatform(runtime, runtime.policy_path, runtime.state)
    canary.consume_arm(runtime.state, operator_uid=os.getuid())
    lane = _make_lane(runtime, fake, arm)

    # The real _job_state raise path (fixture's fake_job_state returns
    # None instead) with a transient-absence window over the first
    # sighting: 113 signature only until the first successful print.
    import re as _re
    from restart_control.process_identity import process_identity
    real_job_state = _Lane._job_state
    printed = {'n': 0}

    def flaky_job_state(label, expected=None, expected_path=None, timeout=10):
        status, raw = fake.execute(['/bin/launchctl', 'print',
                                    f'gui/{os.getuid()}/' + label], 10)
        if status:
            # Mirror the runtime's real classification: the 113
            # not-found signature raises the explicit absence fault
            # (so _cleanup can prove absence); anything else refuses.
            if status == 113 and b'Could not find service' in raw:
                raise Fault('canary_job_absent')
            raise Fault('unmanaged_listener')
        printed['n'] += 1
        if printed['n'] <= 2:
            # Registration latency: pretend launchd has not exposed the
            # job yet (same shape the live 113 produced through the
            # runtime's real classification).
            raise Fault('canary_job_absent')
        text = raw.decode('utf-8', 'replace')

        def field(key):
            match = _re.search(r'(?m)^\s*' + _re.escape(key) + r' = (.+)$',
                               text)
            return match.group(1).strip() if match else None
        pid, runs = field('pid'), field('runs')
        identity = None
        if pid and pid.isdigit():
            try:
                full = process_identity(int(pid))
                identity = {k: full[k] for k in ('pid', 'uid', 'birth')}
            except Exception:
                identity = None
        argmatch = _re.search(
            r'(?ms)^\targuments = \{\n(.*?)^\t\}', text)
        args = ([s.strip() for s in argmatch.group(1).splitlines()
                 if s.strip()] if argmatch else None)
        envmatch = _re.search(r'(?ms)^\tenvironment = \{\n(.*?)^\t\}', text)
        actual_env = {}
        for line in envmatch.group(1).splitlines():
            if ' => ' in line:
                key, value = line.strip().split(' => ', 1)
                actual_env[key] = value
        parity = (args == expected['ProgramArguments']
                  and all(actual_env.get(k) == v
                          for k, v in expected['EnvironmentVariables'].items())
                  and field('minimum runtime') ==
                      str(expected['ThrottleInterval']))
        return {'identity': identity,
                'runs': int(runs) if runs and runs.isdigit() else None,
                'parity': parity}

    lane._job_state = lambda job: flaky_job_state(
        lane.label, expected=lane.canary_job(),
        expected_path=lane._plist_path)
    original_keepalive = lane._keepalive

    def keepalive_step():
        fake.worker_exited(1)
        return original_keepalive()
    lane._keepalive = keepalive_step
    result = lane.run()
    assert result['outcome'] == 'canary_passed', result
    assert printed['n'] >= 3
    assert lane.controller.recovery.transitions == [
        'begin', 'canary_running', 'canary_passed']