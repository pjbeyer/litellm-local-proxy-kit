"""Fixed macOS backend. No caller-provided commands, primary signals or logs."""
from __future__ import annotations
import contextlib
import http.client
import json
import math
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import selectors
import socket
import stat
import struct
import subprocess
import time
import threading
import uuid

from .engine import Fault, atomic_json, canonical_digest, private_dir, _read_json
from .process_identity import process_identity, process_group_identities
from .runtime_identity import (CONTROLLER_INPUTS, PROXY_INPUTS, file_fact, source_facts,
                               _owned_observer_stop)

from .runtime_identity import PROXY_OPTIONAL_INPUTS, native_receipt_fact, read_pin

POLICY_FIELDS = {'version', 'kit_root', 'deployment_root', 'plist', 'auto_plist',
                 'state_dir', 'scratch_dir', 'candidate_port', 'models',
                 'controller_root', 'controller_label'}


def controller_job(policy, policy_path, python):
    """The single generated controller definition used by install and qualify."""
    destination = Path(policy['controller_root'])
    return {'Label': policy['controller_label'],
        'ProgramArguments': [python, '-I', str(destination/'restart_controller.py'),
                             '--policy', str(policy_path), 'serve'],
        'WorkingDirectory': str(destination), 'RunAtLoad': True, 'KeepAlive': True,
        'ThrottleInterval': 30, 'ExitTimeOut': 1000,
        'EnvironmentVariables': {'HOME': str(Path.home()),
            'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
        'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}


def controller_supervision(job):
    return {key: job[key] for key in ('KeepAlive', 'ThrottleInterval', 'RunAtLoad', 'ExitTimeOut')}


REQUEST_TIMEOUT = 15.0
HELPER_OUTPUT_LIMIT = 4 * 1024 * 1024


def _identity(pid):
    try:
        full = process_identity(pid)
        if full['uid'] != os.getuid():
            raise ValueError('foreign_process')
        return {k: full[k] for k in ('pid', 'uid', 'birth')}
    except Exception:
        raise Fault('primary_changed') from None


def _journal_identity(pid):
    """Four-key journal identity; never fabricate a missing pgid or birth."""
    full = process_identity(pid)
    if full['uid'] != os.getuid():
        raise ValueError('foreign_process')
    return {'pid': full['pid'], 'uid': full['uid'],
            'pgid': full['pgid'], 'birth': full['birth']}


def _regular(path):
    try:
        return file_fact(path)
    except Exception:
        raise Fault('runtime_invalid') from None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _native_receipt(pin_path, name):
    if name not in PROXY_OPTIONAL_INPUTS:
        raise ValueError('native_receipt_invalid')
    pin_path = Path(pin_path)
    expects = 'native_artifact' in read_pin(pin_path)
    present = os.path.lexists(pin_path.parent/name)
    fact = native_receipt_fact(pin_path)
    if (fact is not None) != present or present != expects:
        raise ValueError('native_receipt_unpaired')
    return fact


class Runtime:
    fixture = False

    def __init__(self, policy_path, *, shutdown=None, test_lifecycle=None):
        from .shutdown import Shutdown, ShutdownRequested
        if test_lifecycle is not None:
            from .engine import FixtureLifecyclePort
            if not isinstance(test_lifecycle, FixtureLifecyclePort):
                raise Fault('invalid_request')
            if shutdown is not None and shutdown is not test_lifecycle.shutdown:
                raise Fault('invalid_request')
            shutdown = test_lifecycle.shutdown
        elif type(shutdown) is not Shutdown:
            raise Fault('invalid_request')
        self._expected_shutdown = shutdown
        self.shutdown = shutdown
        shutdown.checkpoint()
        self.policy_path = Path(policy_path).absolute()
        try:
            policy = _read_json(self.policy_path)
            shutdown.checkpoint()
            if (not isinstance(policy, dict) or set(policy) != POLICY_FIELDS or
                    type(policy['version']) is not int or policy['version'] != 1):
                raise ValueError('policy_invalid')
            for name in POLICY_FIELDS - {'version', 'candidate_port', 'models', 'controller_label'}:
                if not isinstance(policy[name], str) or not Path(policy[name]).is_absolute():
                    raise ValueError('policy_invalid')
            if (type(policy['candidate_port']) is not int or not 1024 <= policy['candidate_port'] <= 65535
                    or not isinstance(policy['models'], list) or len(policy['models']) != 2
                    or len(set(policy['models'])) != 2
                    or any(not isinstance(m, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,128}', m)
                           for m in policy['models'])
                    or not isinstance(policy['controller_label'], str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', policy['controller_label'])):
                raise ValueError('policy_invalid')
            self.policy = policy
            self.kit = Path(policy['kit_root']).resolve(strict=True)
            self.deployment = Path(policy['deployment_root']).resolve(strict=True)
            self.plist = Path(policy['plist']).resolve(strict=True)
            self.auto_plist = Path(policy['auto_plist']).resolve(strict=True)
            self.controller_root = Path(policy['controller_root']).resolve(strict=True)
            self.state = private_dir(Path(policy['state_dir']))
            self.scratch = private_dir(Path(policy['scratch_dir']))
            self.job = plistlib.loads(self.plist.read_bytes())
            self.label = self.job['Label']
            if not isinstance(self.label, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', self.label):
                raise ValueError('label_invalid')
            argv = self.job['ProgramArguments']
            env = self.job['EnvironmentVariables']
            self.config = Path(self.job['WorkingDirectory']).resolve(strict=True)
            if (len(argv) != 3 or argv[0] != '/bin/bash' or
                    Path(argv[1]).resolve() != self.config/'run-proxy-wrapper.sh' or
                    not re.fullmatch(r'[1-9][0-9]{0,4}', argv[2])):
                raise ValueError('argv_invalid')
            self.port = int(argv[2])
            if self.port == policy['candidate_port'] or not 1 <= self.port <= 65535:
                raise ValueError('port_invalid')
            if (env['LITELLM_PROXY_LABEL'] != self.label or
                    Path(env['LITELLM_PROXY_CONFIG']).resolve() != self.config/'config.rendered.yaml' or
                    Path(env['CREDENTIALS_MANIFEST']).resolve() != self.config/'credentials.toml' or
                    self.plist.read_bytes() != self.auto_plist.read_bytes()):
                raise ValueError('plist_invalid')
            self.python = env['LITELLM_PROXY_PYTHON']
            self.name = env['LITELLM_DEPLOYMENT']
            if not re.fullmatch(r'[A-Za-z0-9_-]+', self.name):
                raise ValueError('deployment_invalid')
            self.supervision = {k: self.job[k] for k in ('KeepAlive', 'ThrottleInterval', 'RunAtLoad')}
            if type(self.supervision['ThrottleInterval']) is not int or self.supervision['ThrottleInterval'] < 30:
                raise ValueError('supervision_invalid')
            from importlib.util import spec_from_file_location, module_from_spec
            spec = spec_from_file_location('restart_manifest_parser', self.kit/'core/parse_credentials.py')
            if spec is None or spec.loader is None:
                raise ValueError('parser_unavailable')
            parser = module_from_spec(spec)
            spec.loader.exec_module(parser)
            self.rows = parser.parse((self.config/'credentials.toml').read_text())
            denied = {'OP_SERVICE_ACCOUNT_TOKEN', 'PYTHONHOME', 'PYTHONPATH'} | {
                row['env_var'] for row in self.rows}
            if denied.intersection(env) or any(not isinstance(v, str) for v in env.values()):
                raise ValueError('inherited_credentials')
            self.env = {'HOME': str(Path.home()), 'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
                        'TMPDIR': str(self.scratch), **env}
            self.policy_digest = canonical_digest(policy)
            self._key = None
            self._operation_started = None
            self._last_runtime = None
            self._last_frozen = None
            self._candidate = None
            self._cleanup_blocked = False
            self._job_blob = self.plist.read_bytes()
            self._manifest_blob = (self.config/'credentials.toml').read_bytes()
            self._loaded_sources = source_facts(self.kit)
            self._last_proxy = None
            self._proof_state = {'root': None}
            shutdown.checkpoint()
            if test_lifecycle is not None:
                self.recovery = test_lifecycle
                self._active_operation = object()
                self._lifecycle_bound = True
        except ShutdownRequested:
            raise
        except Fault:
            raise
        except Exception:
            raise Fault('runtime_invalid') from None

    def release_credentials(self):
        self._key = None
        self._operation_started = None
        active = getattr(self, '_active_operation', None)
        self._active_operation = None
        if active is not None:
            try:
                self.recovery.end_operation(active)
            except Exception:
                raise Fault('recovery_invalid') from None

    def bind_lifecycle(self, shutdown, recovery):
        from .recovery import RecoveryStore
        from .shutdown import Shutdown
        if (getattr(self, '_lifecycle_bound', False)
                or type(shutdown) is not Shutdown or type(recovery) is not RecoveryStore
                or shutdown is not self._expected_shutdown
                or recovery.state_dir.resolve() != self.state.resolve()):
            raise Fault('recovery_invalid')
        self.shutdown = shutdown
        self.recovery = recovery
        self._active_operation = None
        self._lifecycle_bound = True

    def begin_operation(self, handle):
        from .shutdown import ShutdownRequested
        if (handle is None or not getattr(self, '_lifecycle_bound', False)
                or self._active_operation is not None):
            raise Fault('recovery_invalid')
        try:
            self.shutdown.checkpoint()
            self.recovery.require_clear(handle)
        except ShutdownRequested:
            raise Fault('controller_stopping') from None
        except Exception:
            raise Fault('recovery_invalid') from None
        self._active_operation = handle

    def _admit_helper(self):
        from .shutdown import ShutdownRequested
        if not getattr(self, '_lifecycle_bound', False) or self._active_operation is None:
            raise Fault('recovery_invalid')
        try:
            self.shutdown.checkpoint()
            self.recovery.require_clear(self._active_operation)
        except ShutdownRequested:
            raise Fault('controller_stopping') from None
        except Exception:
            raise Fault('recovery_invalid') from None

    def _group_exists(self, pgid):
        return bool(process_group_identities(pgid))

    def _owned_stop(self, child, birth, grace=20):
        if type(grace) not in (int, float) or grace <= 0:
            self._cleanup_blocked = True
            raise Fault("candidate_cleanup_failed")
        deadline = time.monotonic() + grace
        if not deadline < float("inf"):
            self._cleanup_blocked = True
            raise Fault("candidate_cleanup_failed")

        def remaining():
            local = max(0.0, deadline - time.monotonic())
            value = self.shutdown.remaining(local, cleanup=True)
            if value <= 0:
                raise RuntimeError()
            return value

        try:
            def valid_birth(value):
                return (
                    type(value) is list
                    and len(value) == 2
                    and type(value[0]) is int
                    and value[0] > 0
                    and type(value[1]) is int
                    and 0 <= value[1] < 1_000_000
                )

            def valid_full(identity):
                return (
                    type(identity) is dict
                    and set(identity) == {"pid", "uid", "ppid", "pgid", "birth"}
                    and type(identity["pid"]) is int
                    and identity["pid"] > 0
                    and type(identity["uid"]) is int
                    and identity["uid"] >= 0
                    and type(identity["ppid"]) is int
                    and identity["ppid"] >= 0
                    and type(identity["pgid"]) is int
                    and identity["pgid"] > 0
                    and valid_birth(identity["birth"])
                )

            def current_leader():
                identity = process_identity(child.pid)
                if not valid_full(identity):
                    raise RuntimeError()
                if (
                    identity["pid"] != child.pid
                    or identity["uid"] != os.getuid()
                    or identity["pgid"] != child.pid
                    or identity["birth"] != captured_birth
                ):
                    raise RuntimeError()
                if captured_pgid is not None and identity["pgid"] != captured_pgid:
                    raise RuntimeError()
                return identity

            def empty_group():
                remaining()
                first = process_group_identities(child.pid)
                if type(first) is not tuple or first:
                    raise RuntimeError()
                remaining()
                second = process_group_identities(child.pid)
                if type(second) is not tuple or second:
                    raise RuntimeError()
                remaining()
                child.wait(timeout=0)

            def validate_census(leader):
                remaining()
                census = process_group_identities(child.pid)
                if type(census) is not tuple or not census:
                    raise RuntimeError()
                seen = set()
                found_leader = False
                for member in census:
                    if not valid_full(member):
                        raise RuntimeError()
                    pid = member["pid"]
                    if (
                        pid in seen
                        or member["uid"] != os.getuid()
                        or member["pgid"] != child.pid
                        or member["birth"] < captured_birth
                    ):
                        raise RuntimeError()
                    seen.add(pid)
                    if pid == child.pid:
                        if member != leader:
                            raise RuntimeError()
                        found_leader = True
                if not found_leader:
                    raise RuntimeError()
                remaining()

            if type(child.pid) is not int or child.pid <= 0:
                raise RuntimeError()
            remaining()
            if birth is None:
                if child.poll() is None:
                    raise RuntimeError()
                empty_group()
                return None
            if type(birth) is not dict or set(birth) not in (
                {"pid", "uid", "birth"},
                {"pid", "uid", "pgid", "birth"},
            ):
                raise RuntimeError()
            if type(birth["pid"]) is not int or birth["pid"] != child.pid:
                raise RuntimeError()
            if type(birth["uid"]) is not int or birth["uid"] != os.getuid():
                raise RuntimeError()
            if "pgid" in birth:
                if type(birth["pgid"]) is not int or birth["pgid"] != child.pid:
                    raise RuntimeError()
                captured_pgid = birth["pgid"]
            else:
                captured_pgid = None
            captured_birth = birth["birth"]
            if not valid_birth(captured_birth):
                raise RuntimeError()

            remaining()
            if child.poll() is not None:
                empty_group()
                return None
            leader = current_leader()
            validate_census(leader)

            if child.poll() is not None:
                empty_group()
                return None
            current_leader()
            remaining()
            os.killpg(child.pid, signal.SIGTERM)

            while True:
                remaining()
                if child.poll() is not None:
                    empty_group()
                    return None
                leader = current_leader()
                validate_census(leader)
                timeout = min(0.05, remaining())
                self.shutdown.wait(timeout, cleanup=True)
        except Exception:
            # The child can exit between a poll and a kernel identity
            # inspection — including immediately after our own SIGTERM.
            # That is an ordinary, expected exit, not a blocked state:
            # bounded-wait for the death to land, reap it, and prove the
            # group empty with two scans. Fail closed only when the child
            # stays alive with unreadable identity, or members remain.
            # Never signal an unknown identity from this path.
            try:
                end = time.monotonic() + min(2.0, max(0.0, deadline - time.monotonic()))
                while child.poll() is None and time.monotonic() < end:
                    time.sleep(0.01)
                if child.poll() is not None:
                    # poll() reaped the child; prove emptiness with two scans
                    # before any wait() call. No signal is ever issued here.
                    first = process_group_identities(child.pid)
                    second = process_group_identities(child.pid)
                    if (type(first) is tuple and not first
                            and type(second) is tuple and not second):
                        return None
            except Exception:
                pass
            self._cleanup_blocked = True
            raise Fault("candidate_cleanup_failed") from None

    _PROOF_OPS = frozenset({'bind', 'git_root', 'git_head', 'git_entries',
                            'native_sources', 'native_tree', 'native_exports'})
    _PROOF_REQUEST_LIMIT_BYTES = 4096
    _PROOF_DATA_LIMIT = 4 * 1024 * 1024
    _PROOF_MAX_REQUESTS = 64

    def _serve_proof_channel(self, connection, deadline, state):
        """Serve one worker's observations; each executes through the action gate.

        Single-threaded: the parent blocks in recv while the worker runs;
        every observation is a journalled, tracked helper spawn. No observer
        commits after the shutdown latch; disconnection, unknown observer
        state or cleanup failure is sticky recovery uncertainty, never a
        success digest. One absolute deadline bounds the whole channel.
        """
        requests = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Fault('service_control_timeout')
            request = self._proof_read_request(connection, deadline)
            if request is None:  # worker closed the channel: proof finished
                return
            requests += 1
            if requests > self._PROOF_MAX_REQUESTS:
                raise Fault('invalid_request')
            self._dispatch_proof_observation(connection, request, state, deadline)

    def _proof_read_request(self, connection, deadline):
        """Read one newline-terminated JSON request, exactly; None on EOF.

        Silence while the worker verifies sources is normal: recv retries
        until the absolute deadline, never a per-request timeout.
        """
        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Fault('service_control_timeout')
            connection.settimeout(min(remaining, 0.5))
            try:
                part = connection.recv(4096)
            except TimeoutError:
                continue
            except (BlockingIOError, InterruptedError):
                continue
            if not part:
                if chunks:
                    raise Fault('invalid_request')
                return None
            chunks.extend(part)
            if len(chunks) > self._PROOF_REQUEST_LIMIT_BYTES:
                raise Fault('invalid_request')
            if b'\n' in chunks:
                line, _, tail = bytes(chunks).partition(b'\n')
                # No pipelining: bytes after the first newline refuse.
                if tail:
                    raise Fault('invalid_request')
                if not line:
                    raise Fault('invalid_request')
                return line

    def _dispatch_proof_observation(self, connection, request, state, deadline):
        """Validate one closed observation request and execute it journalled."""
        try:
            decoded = json.loads(request)
        except Exception:
            raise Fault('invalid_request') from None
        if (type(decoded) is not dict or decoded.get('version') != 1
                or type(decoded.get('seq')) is not int or decoded['seq'] < 1
                or type(decoded.get('op')) is not str):
            raise Fault('invalid_request')
        op = decoded['op']
        if op not in self._PROOF_OPS:
            raise Fault('invalid_request')
        if op == 'bind':
            if (decoded['seq'] != 1 or set(decoded) != {
                    'version', 'seq', 'op', 'nonce', 'pid', 'source_sha256',
                    'pin_sha256'}):
                raise Fault('invalid_request')
        else:
            if decoded['seq'] != state['next_seq']:
                raise Fault('invalid_request')
            if op == 'git_root':
                if set(decoded) != {'version', 'seq', 'op', 'path'}:
                    raise Fault('invalid_request')
            elif op == 'git_entries':
                if set(decoded) != {'version', 'seq', 'op', 'relative'}:
                    raise Fault('invalid_request')
                if (type(decoded['relative']) is not str or not decoded['relative']
                        or decoded['relative'].startswith('/')):
                    raise Fault('invalid_request')
            elif set(decoded) != {'version', 'seq', 'op'}:
                raise Fault('invalid_request')
            state['next_seq'] = decoded['seq'] + 1
        status, data = self._execute_proof_observation(op, decoded, state, deadline)
        self._proof_respond(connection, status, data)

    def _execute_proof_observation(self, op, request, state, deadline):
        """Run one observation through the journal-attached action gate.

        Returns (0, data) on success or (1, b'') refusal. Protocol forgery
        (bind mismatch) tears the channel down; admission refusals answer
        the worker with a refusal byte and fail its proof.
        """
        if op == 'bind':
            fact = self._loaded_sources['core/restart_control/runtime_identity.py']
            if (request['nonce'] != state['nonce']
                    or request['source_sha256'] != fact['sha256']
                    or request['pid'] != state['pid']
                    or request['pin_sha256'] != state['pin_sha256']):
                raise Fault('invalid_request')
            return 0, b''
        if op == 'bind-shape':  # unreachable; keeps linters honest
            raise Fault('invalid_request')
        if op == 'git_root':
            if state['root'] is not None:
                return 1, b''
            try:
                nominated = Path(request['path']).resolve(strict=True)
            except Exception:
                return 1, b''
            if nominated.name != 'litellm' or not nominated.is_dir():
                return 1, b''
            root_raw = self._capture(
                ['git', '-C', str(nominated), 'rev-parse', '--show-toplevel'],
                timeout=60, purpose='runtime-proof')
            root = Path(root_raw.decode().strip()).resolve()
            head = self._capture(
                ['git', '-C', str(root), 'rev-parse', 'HEAD'],
                timeout=60, purpose='runtime-proof').decode().strip()
            pin = read_pin(self.config/'pin.lock')
            if not head.startswith(pin['patch_tip']):
                return 1, b''
            state['root'] = root
            return 0, str(root).encode()
        root = state['root']
        if root is None:
            return 1, b''
        if op == 'git_head':
            return 0, self._capture(
                ['git', '-C', str(root), 'rev-parse', 'HEAD'],
                timeout=60, purpose='runtime-proof')
        if op == 'git_entries':
            relative = request['relative']
            try:
                (root/relative).resolve(strict=True).relative_to(root)
            except Exception:
                return 1, b''
            return 0, self._capture(
                ['git', '-C', str(root), 'ls-tree', '-rz', 'HEAD', '--', relative],
                timeout=60, purpose='runtime-proof')
        if op == 'native_sources':
            return 0, self._capture(
                ['git', '-C', str(root), 'ls-tree', '-rz', 'HEAD', '--',
                 'litellm-rust', '.cargo'],
                timeout=60, purpose='runtime-proof')
        if op == 'native_tree':
            return 0, self._capture(
                ['git', '-C', str(root), 'rev-parse', 'HEAD:litellm-rust'],
                timeout=60, purpose='runtime-proof')
        if op == 'native_exports':
            from .runtime_identity import NATIVE_PATH
            return 0, self._capture(
                ['/usr/bin/nm', '-gU', str(root/NATIVE_PATH)],
                timeout=60, purpose='runtime-proof')
        raise Fault('invalid_request')

    def _proof_respond(self, connection, status, data):
        """Fixed status/length framing; refusal is one byte, no payload."""
        if data is None:
            data = b''
        if len(data) > self._PROOF_DATA_LIMIT:
            raise Fault('invalid_request')
        connection.settimeout(2.0)
        connection.sendall(struct.pack('>BI', status, len(data)))
        if data:
            connection.sendall(data)

    _PROOF_WORKER_SOURCE = 'core/restart_control/proof_worker.py'

    def run_proof_worker(self):
        """Fresh isolated pinned-interpreter proof worker for every proof.

        The worker is our own single helper child: fixed script, -I -B, no
        controller-package imports, one inherited anonymous socketpair. The
        parent owns its Popen/PID/birth/group and serves every observation
        through the journal-attached action gate; worker stdout carries only
        the existing bounded proof result. Cleanup follows _capture's exact
        contract: single SIGTERM, bounded reap, group census, sticky
        uncertainty on any doubt.
        """
        import socket as socket_module
        from .shutdown import ShutdownRequested
        if (self._proof_state.get('root') is not None or
                self._candidate is not None or self._cleanup_blocked):
            raise Fault('recovery_invalid')
        worker_source = self.controller_root/'restart_control'/'proof_worker.py'
        fact = self._loaded_sources[self._PROOF_WORKER_SOURCE]
        if file_fact(worker_source) != fact:
            raise Fault('runtime_invalid')
        nonce = uuid.uuid4().hex
        spawn_id = uuid.uuid4().hex
        parent_end, child_end = socket_module.socketpair()
        child = None
        journal = None
        birth = None
        handle = None
        out = None
        try:
            self._admit_helper()
            handle = self.recovery.spawn_intent(self._active_operation,
                                                spawn_id, 'runtime-proof')
            try:
                permit = self.shutdown.commit('runtime-proof')
                self.shutdown.claim(permit)
            except ShutdownRequested:
                with contextlib.suppress(Exception):
                    self.recovery.mark_uncertain(handle)
                raise Fault('controller_stopping') from None
            deadline = time.monotonic() + 120
            argv = [self.python, '-I', '-B', str(worker_source),
                    str(self.config/'pin.lock'), str(child_end.fileno()), nonce]
            child = subprocess.Popen(
                argv, env=self.env, cwd=self.config, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True, pass_fds=(child_end.fileno(),))
            try:
                journal = _journal_identity(child.pid)
                birth = {k: journal[k] for k in ('pid', 'uid', 'birth')}
            except Fault:
                if child.poll() is None:
                    raise
                journal = None
                birth = None
            if journal is not None:
                self.recovery.spawn_owned(handle, journal)
            state = {'next_seq': 2, 'nonce': nonce, 'pid': child.pid,
                     'pin_sha256': file_fact(self.config/'pin.lock')['sha256'],
                     'root': None}
            child_end.close()
            try:
                self._serve_proof_channel(parent_end, deadline, state)
                out = self._read_worker_stdout(child, deadline)
            finally:
                with contextlib.suppress(Exception):
                    parent_end.close()
                with contextlib.suppress(Exception):
                    if child.poll() is not None:
                        child.wait(timeout=1)
                if child.stdout is not None:
                    child.stdout.close()
        except Fault:
            self._cleanup_blocked = self._cleanup_blocked or (journal is None)
            if handle is not None:
                with contextlib.suppress(Exception):
                    self.recovery.mark_uncertain(handle)
            raise
        except Exception:
            # Journal write or identity loss: never invent ownership/closure.
            self._cleanup_blocked = True
            if handle is not None:
                with contextlib.suppress(Exception):
                    self.recovery.mark_uncertain(handle)
            raise Fault('recovery_invalid') from None
        else:
            if journal is not None:
                try:
                    self.recovery.spawn_closed(handle, journal, cleanup_proven=True)
                except Exception:
                    self._cleanup_blocked = True
                    raise Fault('recovery_invalid') from None
            self._proof_state['root'] = None
            return out
        finally:
            # Worker cleanup is never an unowned escalation: our own child,
            # identity-proven stop through _owned_stop, bounded reap.
            with contextlib.suppress(Exception):
                child_end.close()
            if child is not None:
                try:
                    self._owned_stop(child, birth)
                finally:
                    if child.stdout is not None:
                        child.stdout.close()
                    if child.stderr is not None:
                        child.stderr.close()

    def _read_worker_stdout(self, child, deadline):
        """Bounded proof-result read during pipe reads, not post-buffering."""
        stream = child.stdout
        assert stream is not None
        out = bytearray()
        with selectors.DefaultSelector() as reader:
            reader.register(stream, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Fault('service_control_timeout')
                if not reader.select(min(remaining, 0.1)):
                    continue
                part = os.read(stream.fileno(),
                               min(65536, HELPER_OUTPUT_LIMIT + 1 - len(out)))
                if not part:
                    break
                out.extend(part)
                if len(out) > HELPER_OUTPUT_LIMIT:
                    raise Fault('gate_failed')
        child.wait(timeout=max(.001, deadline-time.monotonic()))
        if child.returncode:
            raise Fault('gate_failed')
        return bytes(out)

    def _observe(self, argv, timeout, env=None, control=False):
        """Run one read-only observation child (lsof, launchctl print).

        Latch-checked before the child is created, bounded pipe reads
        against one absolute deadline, and SIGTERM-only cleanup through
        the shared primitive — the last two implicit-kill subprocess.run
        sites are gone. A surviving observer after its grace window is
        sticky cleanup uncertainty. Like the pre-existing observation
        one-shots, these short-lived read-only children are outside the
        journaled tracked-spawn family (per-poll journaling would
        exhaust the frozen 64-spawn recovery cap during acceptance
        polling); the latch check before each child and the bounded
        no-escalation cleanup are the SC-003/SC-004 obligations here.
        Unlike _capture a nonzero exit is a RESULT, not a failure: lsof
        returns 1 for no listener and launchctl print is nonzero for a
        missing service, so callers inspect the status.
        """
        from .shutdown import ShutdownRequested
        try:
            self.shutdown.checkpoint()
        except ShutdownRequested:
            raise Fault('controller_stopping') from None
        if self._cleanup_blocked:
            raise Fault('candidate_cleanup_failed')
        if self._operation_started is None:
            self._operation_started = time.monotonic()
        remaining = 900 - (time.monotonic() - self._operation_started)
        if remaining <= 0:
            raise Fault('service_control_timeout' if control else 'gate_failed')
        child = None
        try:
            deadline = time.monotonic() + min(timeout, remaining)
            child = subprocess.Popen(argv, env=self.env if env is None else env,
                                     cwd=self.config, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True)
            stream, errors = child.stdout, child.stderr
            assert stream is not None and errors is not None
            out, err = bytearray(), bytearray()
            with selectors.DefaultSelector() as reader:
                reader.register(stream, selectors.EVENT_READ)
                reader.register(errors, selectors.EVENT_READ)
                while reader.get_map():
                    available = deadline - time.monotonic()
                    if available <= 0 or not reader.select(available):
                        raise subprocess.TimeoutExpired(argv, timeout)
                    for key, _ in reader.select(0):
                        target = out if key.fd == stream.fileno() else err
                        data = os.read(key.fd,
                                       min(65536, HELPER_OUTPUT_LIMIT + 1 - len(target)))
                        if not data:
                            reader.unregister(key.fileobj)
                            continue
                        target.extend(data)
                    if len(out) > HELPER_OUTPUT_LIMIT or len(err) > HELPER_OUTPUT_LIMIT:
                        raise ValueError('observation_overflow')
            child.wait(timeout=max(.001, deadline-time.monotonic()))
            return child.returncode, bytes(out)
        except subprocess.TimeoutExpired:
            raise Fault('service_control_timeout' if control else 'gate_failed') from None
        except ShutdownRequested:
            raise Fault('controller_stopping') from None
        except Exception:
            raise Fault('unmanaged_listener') from None
        finally:
            # Single SIGTERM, bounded reap, never SIGKILL or killpg. A
            # surviving observer is sticky; the business Fault still
            # propagates.
            if child is not None:
                try:
                    if child.poll() is None:
                        _owned_observer_stop(child)
                except Exception:
                    self._cleanup_blocked = True
                finally:
                    if child.stdout is not None:
                        child.stdout.close()
                    if child.stderr is not None:
                        child.stderr.close()

    def _capture(self, argv, timeout=90, env=None, control=False, purpose=None):
        from .shutdown import ShutdownRequested
        spawn_id = uuid.uuid4().hex
        if purpose is None:
            purpose = 'diagnostic'
        self._admit_helper()
        if self._candidate is not None or self._cleanup_blocked:
            raise Fault('candidate_cleanup_failed')
        if self._operation_started is None:
            self._operation_started = time.monotonic()
        remaining = 900 - (time.monotonic() - self._operation_started)
        if remaining <= 0:
            raise Fault('service_control_timeout' if control else 'gate_failed')
        handle = self.recovery.spawn_intent(self._active_operation, spawn_id, purpose)
        try:
            permit = self.shutdown.commit(purpose)
            self.shutdown.claim(permit)
        except ShutdownRequested:
            with contextlib.suppress(Exception):
                self.recovery.mark_uncertain(handle)
            raise Fault('controller_stopping') from None
        child = None
        journal = None
        birth = None
        result = None
        try:
            try:
                deadline = time.monotonic() + min(timeout, remaining)
                child = subprocess.Popen(argv, env=self.env if env is None else env,
                                         cwd=self.config, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         start_new_session=True)
                try:
                    journal = _journal_identity(child.pid)
                    birth = {k: journal[k] for k in ('pid', 'uid', 'birth')}
                except Fault:
                    if child.poll() is None:
                        raise
                if journal is not None:
                    self.recovery.spawn_owned(handle, journal)
                stream = child.stdout
                assert stream is not None  # Popen was explicitly given stdout=PIPE
                out = bytearray()
                with selectors.DefaultSelector() as reader:
                    reader.register(stream, selectors.EVENT_READ)
                    while True:
                        available = deadline - time.monotonic()
                        if available <= 0 or not reader.select(available):
                            raise subprocess.TimeoutExpired(argv, timeout)
                        part = os.read(stream.fileno(),
                                       min(65536, HELPER_OUTPUT_LIMIT + 1 - len(out)))
                        if not part:
                            break
                        out.extend(part)
                        if len(out) > HELPER_OUTPUT_LIMIT:
                            raise Fault('gate_failed')
                child.wait(timeout=max(.001, deadline-time.monotonic()))
                if child.returncode:
                    raise Fault('service_control_denied' if control else 'gate_failed')
                result = bytes(out)
            finally:
                if child is not None:
                    try:
                        self._owned_stop(child, birth)
                    finally:
                        if child.stdout is not None:
                            child.stdout.close()
                        if child.stderr is not None:
                            child.stderr.close()
        except Fault:
            # Deliberate conservatism: a helper that failed its business work
            # (e.g. nonzero exit -> gate_failed) is journaled 'uncertain' even
            # when cleanup was fully proven. The child is dead and reaped, so
            # the label is semantically conservative, not exact — but an
            # uncertain row only costs a cleared-ticket reconciliation before
            # the next admission, and never fabricates closure for a helper
            # whose work failed.
            self._cleanup_blocked = self._cleanup_blocked or (journal is None)
            with contextlib.suppress(Exception):
                self.recovery.mark_uncertain(handle)
            raise
        except subprocess.TimeoutExpired:
            with contextlib.suppress(Exception):
                self.recovery.mark_uncertain(handle)
            raise Fault('service_control_timeout' if control else 'gate_failed') from None
        except Exception:
            # Journal write or identity loss: never invent ownership/closure,
            # stay sticky and refuse next work with a closed Fault.
            with contextlib.suppress(Exception):
                self.recovery.mark_uncertain(handle)
            self._cleanup_blocked = True
            raise Fault('recovery_invalid') from None
        else:
            if journal is not None:
                try:
                    self.recovery.spawn_closed(handle, journal, cleanup_proven=True)
                except Exception:
                    # Close-write failure after fully proven cleanup: state is
                    # preserved and we fail loud; never return success while
                    # the durable close is unproven.
                    self._cleanup_blocked = True
                    raise Fault('recovery_invalid') from None
            return result

    def _listener(self, port):
        if type(port) is not int or port not in (self.port, self.policy['candidate_port']):
            raise Fault('unmanaged_listener')
        status, out = self._observe(['/usr/sbin/lsof', '-t', '-nP', '-iTCP:'+str(port), '-sTCP:LISTEN'],
                                    timeout=10)
        # lsof exit 1 with empty output and empty stderr is its documented
        # "nothing matches" result; any other shape is an unknown state.
        if status == 1 and not out.strip():
            return None
        values = out.decode().split()
        if status or not values or any(not v.isdigit() for v in values):
            raise Fault('unmanaged_listener')
        pids = set(map(int, values))
        if len(pids) != 1:
            raise Fault('ambiguous_listener')
        return _identity(pids.pop())

    def _job_state(self, label=None, expected=None, expected_path=None):
        label = label or self.label
        controller_check = expected is not None
        expected = self.job if expected is None else expected
        expected_path = self.auto_plist if expected_path is None else expected_path
        try:
            status, raw = self._observe(['/bin/launchctl', 'print', f'gui/{os.getuid()}/'+label],
                                        timeout=10)
            if status:
                return None
            text = raw.decode('utf-8', 'replace')
            def field(key):
                match = re.search(r'(?m)^\s*'+re.escape(key)+r' = (.+)$', text)
                return match.group(1).strip() if match else None
            pid, runs = field('pid'), field('runs')
            identity = _identity(int(pid)) if pid and pid.isdigit() else None
            argmatch = re.search(r'(?ms)^\targuments = \{\n(.*?)^\t\}', text)
            args = [s.strip() for s in argmatch.group(1).splitlines() if s.strip()] if argmatch else None
            envmatch = re.search(r'(?ms)^\tenvironment = \{\n(.*?)^\t\}', text)
            actual_env = {}
            if envmatch:
                for line in envmatch.group(1).splitlines():
                    if ' => ' in line:
                        key, value = line.strip().split(' => ', 1)
                        actual_env[key] = value
            parity = (field('path') == str(expected_path) and args == expected['ProgramArguments']
                      and all(actual_env.get(k) == v for k, v in expected['EnvironmentVariables'].items())
                      and field('minimum runtime') == str(expected['ThrottleInterval']))
            if controller_check:
                # launchd may add only its matching service-name metadata.
                # Read the unique top-level observation; nested or duplicate
                # fields cannot attest the controller's shutdown grace period.
                exit_timeouts = re.findall(r'(?m)^\texit timeout = ([^\r\n]+)$', text)
                exit_parity = (len(exit_timeouts) == 1 and
                               exit_timeouts[0].strip() == str(expected['ExitTimeOut']))
                explicit = dict(actual_env)
                if explicit.get('XPC_SERVICE_NAME') == label:
                    explicit.pop('XPC_SERVICE_NAME')
                parity = (parity and exit_parity and field('program') == expected['ProgramArguments'][0]
                          and field('working directory') == expected['WorkingDirectory']
                          and explicit == expected['EnvironmentVariables'])
            return {'identity': identity, 'runs': int(runs) if runs and runs.isdigit() else None,
                    'parity': parity}
        except Exception:
            return None

    def _bootstrap_fact(self, identity):
        return {'version': 1, 'uid': os.getuid(), 'label': self.label,
                'identity': identity, 'plist_digest': _regular(self.plist)['sha256'],
                'supervision': self.supervision}

    def inspect(self):
        identity = self._listener(self.port)
        if identity is None:
            raise Fault('unmanaged_listener')
        job = self._job_state()
        managed = bool(job and job['identity'] == identity and job['parity'])
        if managed:
            try:
                managed = _read_json(self.state/'bootstrap.json') == self._bootstrap_fact(identity)
            except Exception:
                managed = False
        return {'identity': identity, 'managed': managed}

    def freeze(self):
        try:
            if canonical_digest(_read_json(self.policy_path)) != self.policy_digest:
                raise ValueError('policy_changed')
            facts = source_facts(self.kit)
            if (facts != self._loaded_sources or self.plist.read_bytes() != self._job_blob
                    or self.auto_plist.read_bytes() != self._job_blob
                    or (self.config/'credentials.toml').read_bytes() != self._manifest_blob):
                raise ValueError('controller_generation_changed')
            controller = {}
            for name in CONTROLLER_INPUTS:
                copy = self.controller_root/name.removeprefix('core/')
                fact = _regular(copy)
                if fact != facts[name]:
                    raise ValueError('controller_copy_changed')
                controller[name] = fact
            proxy: dict[str, object] = {name: _regular(self.config/name) for name in
                     (*PROXY_INPUTS, self.label+'.plist', self.label+'.rotate.plist')}
            for name in PROXY_OPTIONAL_INPUTS:
                proxy['optional/'+name] = _native_receipt(self.config/'pin.lock', name)
            inputs: dict[str, object] = {'deploy.conf.sh': _regular(self.deployment/'deploy.conf.sh'),
                      'auto_plist': _regular(self.auto_plist)}
            deployment_pin = self.deployment/'deployments'/self.name/'pin.lock'
            for name in ('overlay.yaml', 'credentials.toml', 'pin.lock'):
                inputs['deployment/'+name] = _regular(self.deployment/'deployments'/self.name/name)
            for name in PROXY_OPTIONAL_INPUTS:
                inputs['deployment/'+name] = _native_receipt(deployment_pin, name)
            exemptions = self.deployment/'drift-exemptions.yaml'
            inputs['drift-exemptions.yaml'] = _regular(exemptions) if exemptions.exists() else None
            proof = json.loads(self.run_proof_worker())
            runtime_digest = proof['runtime_digest']
            full = {'kit': facts, 'proxy': proxy, 'inputs': inputs,
                    'policy_digest': self.policy_digest, 'runtime_digest': runtime_digest,
                    'controller': controller}
            frozen = {'asset_digest': canonical_digest(full), 'runtime_digest': runtime_digest,
                      'policy_digest': self.policy_digest, 'controller_digest': canonical_digest(controller)}
            self._last_runtime, self._last_frozen, self._last_proxy = runtime_digest, frozen, proxy
            return frozen
        except Exception:
            raise Fault('runtime_invalid') from None

    def check_gates(self):
        from .shutdown import ShutdownRequested
        checkpoints = []
        def admit():
            try:
                self.shutdown.checkpoint()
            except ShutdownRequested:
                raise Fault('controller_stopping') from None
            checkpoints.append(1)
        admit()
        self._capture(['/bin/bash', str(self.kit/'core/installer.sh'), str(self.deployment), '--verify'], 90)
        admit()
        self._capture([self.python, str(self.kit/'core/drift_check.py'), '--deployment-root', str(self.deployment)], 90)
        admit()
        self._capture([*self.job['ProgramArguments'], '--preflight'], 180)
        admit()
        try:
            import yaml
            config = yaml.safe_load((self.config/'config.rendered.yaml').read_text())
            reference = config['general_settings']['master_key']
            if not isinstance(reference, str) or not re.fullmatch(r'os\.environ/[A-Z][A-Z_0-9]*', reference):
                raise ValueError('master_reference_invalid')
            name = reference.split('/', 1)[1]
            rows = [row for row in self.rows if row.get('env_var') == name]
            if len(rows) != 1 or rows[0].get('required') is not True:
                raise ValueError('master_row_invalid')
            row = rows[0]
            admit()
            if row.get('source', 'op') == 'op':
                sa = self._capture(['/usr/bin/security', 'find-generic-password', '-s',
                                    self.env['OP_KEYCHAIN_SERVICE'], '-a', self.env['OP_KEYCHAIN_ACCOUNT'], '-w'], 15).decode().strip()
                if not sa or sa.startswith('op://') or any(c.isspace() for c in sa):
                    raise ValueError('resolver_unavailable')
                env = {**self.env, 'OP_LOAD_DESKTOP_APP_SETTINGS': 'false', 'OP_CACHE': 'false',
                       'OP_SERVICE_ACCOUNT_TOKEN': sa}
                op = shutil.which('op', path=self.env['PATH'])
                if not op:
                    raise ValueError('resolver_unavailable')
                admit()
                key = self._capture([op, 'read', row['op_ref']], 30, env=env).decode().strip()
                del sa, env
            elif row.get('source') == 'flexai-token':
                tool = shutil.which('flexai', path=self.env['PATH'])
                if not tool:
                    raise ValueError('resolver_unavailable')
                admit()
                key = self._capture([tool, 'auth', 'print-token'], 30).decode().strip()
            else:
                raise ValueError('master_source_invalid')
            if not key or key.startswith('op://') or any(c.isspace() for c in key):
                raise ValueError('master_unresolved')
            self._key = key
        except ShutdownRequested:
            raise Fault('controller_stopping') from None
        except Exception:
            raise Fault('gate_failed') from None

    def _request(self, port, path, key=None, payload=None):
        from .shutdown import ShutdownRequested
        if port not in (self.port, self.policy['candidate_port']) or path not in (
                '/health/liveliness', '/health/readiness', '/v1/models', '/v1/chat/completions'):
            raise Fault('postcheck_failed')
        headers = {'Content-Type': 'application/json'}
        if key is not None:
            headers['Authorization'] = 'Bearer '+key
        try:
            self.shutdown.checkpoint()
        except ShutdownRequested:
            raise Fault('controller_stopping') from None
        if self._operation_started is None:
            self._operation_started = time.monotonic()
        budget = min(REQUEST_TIMEOUT, 900 - (time.monotonic() - self._operation_started))
        if budget <= 0:
            raise Fault('postcheck_failed')
        # http.client has no ambient-proxy or redirect machinery. The fixed
        # literal host, approved port and closed path define the only URL.
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=budget)
        # One absolute deadline bounds the whole request. The socket's own
        # timeout is the only mechanism that reliably interrupts a blocked
        # recv on macOS (neither close() nor SHUT_RDWR from another thread
        # does), so the deadline IS the socket timeout; SC-004's
        # shared-deadline observability comes from the budget cap at each
        # admission and the latch checked between every request in
        # _accept's wake-polled loop.
        # Absolute deadline: the socket timeout is per-operation and never
        # trips against a slow trickle (one byte inside every window), so a
        # timer force-closes the connection at the budget. Between recv
        # calls the closed fd surfaces immediately; a fully blocked recv
        # is covered by the socket timeout.
        closing = threading.Event()
        def interrupt():
            if not closing.is_set():
                closing.set()
                # SHUT_RDWR surfaces EOF to the next recv — the only
                # reliable interrupt when bytes keep trickling in inside
                # every timeout window. close() alone is a refcount
                # decrement while the response file exists, so the fd
                # would stay open and the buffered read would continue.
                with contextlib.suppress(Exception):
                    connection.sock.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(Exception):
                    connection.close()
        timer = threading.Timer(budget, interrupt)
        timer.daemon = True
        timer.start()
        connect_deadline = time.monotonic() + budget
        try:
            connection.connect()
            if time.monotonic() >= connect_deadline:
                raise TimeoutError('request_budget')
            connection.request('POST' if payload is not None else 'GET', path,
                 body=json.dumps(payload).encode() if payload is not None else None, headers=headers)
            with connection.getresponse() as result:
                if result.status != 200:
                    return result.status, {}
                body = result.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise Fault('postcheck_failed')
                # A trickle can satisfy read() inside every timeout window
                # yet outlive the budget before EOF; refuse results that
                # landed after the absolute deadline.
                if closing.is_set():
                    raise TimeoutError('request_budget')
                return result.status, json.loads(body)
        except Exception:
            raise Fault('postcheck_failed') from None
        finally:
            timer.cancel()
            with contextlib.suppress(Exception):
                connection.close()

    def _accept(self, port, guard, deadline, startup=False):
        from .shutdown import ShutdownRequested
        while True:
            identity = guard()
            try:
                ready = identity is not None and self._request(port, '/health/readiness')[0] == 200
            except Fault:
                ready = False
            except ShutdownRequested:
                raise
            guard()
            if ready:
                break
            if not startup or time.monotonic() >= deadline:
                raise Fault('postcheck_failed')
            # Wake-poll: sleep is bounded and observes the shared latch, so
            # a shutdown request interrupts the wait immediately.
            if self.shutdown.wait(0.5):
                raise Fault('controller_stopping')
        results = {}
        for path in ('/health/liveliness', '/health/readiness'):
            guard()
            status, _ = self._request(port, path)
            guard()
            if status != 200:
                raise Fault('postcheck_failed')
            results[path.rsplit('/', 1)[-1]] = status
        guard()
        status, _ = self._request(port, '/v1/models', 'deliberately-invalid-acceptance-key')
        guard()
        if status != 401:
            raise Fault('postcheck_failed')
        results['invalid_key'] = status
        if self._key is None:
            raise Fault('gate_failed')
        guard()
        status, body = self._request(port, '/v1/models', self._key)
        guard()
        if status != 200 or not isinstance(body.get('data'), list) or not body['data']:
            raise Fault('postcheck_failed')
        results['models'] = {'http': status, 'count': len(body['data'])}
        results['completions'] = []
        for model in self.policy['models']:
            guard()
            status, body = self._request(port, '/v1/chat/completions', self._key,
                 {'model': model, 'messages': [{'role': 'user', 'content': 'Reply OK only.'}], 'max_tokens': 24})
            guard()
            if status != 200 or not isinstance(body.get('choices'), list) or not body['choices']:
                raise Fault('postcheck_failed')
            results['completions'].append({'http': status, 'choices_present': True})
        results['mcp_parity'] = 'not_established'
        return results

    def accept_primary(self, identity):
        def guard():
            current = self.inspect()['identity']
            if current != identity:
                raise Fault('primary_changed')
            return current
        return self._accept(self.port, guard, time.monotonic(), startup=False)

    def rehearse(self):
        from .shutdown import ShutdownRequested
        self._admit_helper()
        port = self.policy['candidate_port']
        if self._candidate is not None or self._cleanup_blocked:
            raise Fault('candidate_cleanup_failed')
        if self._listener(port) is not None:
            raise Fault('candidate_failed')
        argv = [*self.job['ProgramArguments'][:2], str(port)]
        spawn_id = uuid.uuid4().hex
        handle = self.recovery.spawn_intent(self._active_operation, spawn_id, 'candidate')
        try:
            permit = self.shutdown.commit('candidate')
            self.shutdown.claim(permit)
        except ShutdownRequested:
            with contextlib.suppress(Exception):
                self.recovery.mark_uncertain(handle)
            raise Fault('controller_stopping') from None
        child = None
        journal = None
        birth = None
        interrupted = False
        try:
            child = subprocess.Popen(argv, env=self.env, cwd=self.config,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                journal = _journal_identity(child.pid)
                birth = {k: journal[k] for k in ('pid', 'uid', 'birth')}
            except Fault:
                if child.poll() is None:
                    raise
            if journal is not None:
                self.recovery.spawn_owned(handle, journal)
            self._candidate = child
            def guard():
                nonlocal interrupted
                try:
                    self.shutdown.checkpoint()
                except ShutdownRequested:
                    interrupted = True
                    raise Fault('controller_stopping') from None
                if child.poll() is not None or _identity(child.pid) != birth:
                    raise Fault('candidate_failed')
                listener = self._listener(port)
                if listener is not None and listener != birth:
                    raise Fault('candidate_failed')
                return listener
            return self._accept(port, guard, time.monotonic()+180, startup=True)
        except Fault:
            # A latched interruption is never disguised as a candidate
            # business failure: it surfaces as the closed stopping code.
            raise Fault('controller_stopping' if interrupted else 'candidate_failed') from None
        except ShutdownRequested:
            raise Fault('controller_stopping') from None
        except Exception:
            # Journal write or identity loss: never invent ownership/closure.
            self._cleanup_blocked = True
            with contextlib.suppress(Exception):
                self.recovery.mark_uncertain(handle)
            raise Fault('recovery_invalid') from None
        finally:
            if child is not None:
                try:
                    self._owned_stop(child, birth)
                finally:
                    self._candidate = None
            try:
                if self._listener(port) is not None:
                    self._cleanup_blocked = True
                    raise Fault('candidate_cleanup_failed')
            except Fault:
                if not interrupted:
                    raise
            if journal is not None:
                if self._cleanup_blocked:
                    with contextlib.suppress(Exception):
                        self.recovery.mark_uncertain(handle)
                else:
                    try:
                        self.recovery.spawn_closed(handle, journal, cleanup_proven=True)
                    except Exception:
                        self._cleanup_blocked = True
                        raise Fault('recovery_invalid') from None

    def _optional_names(self):
        return set(PROXY_OPTIONAL_INPUTS)

    def snapshot_lkg(self, path):
        if self._last_frozen is None:
            raise Fault('lkg_invalid')
        manifest = {}
        try:
            for name in (*PROXY_INPUTS, self.label+'.plist', self.label+'.rotate.plist'):
                fact = _regular(self.config/name)
                shutil.copy2(self.config/name, path/name)
                if _regular(path/name) != fact:
                    raise ValueError('snapshot_changed')
                manifest[name] = fact
            # Optional native receipt: frozen presence/absence is preserved
            # exactly — copied only when present, recorded absence otherwise.
            # A receipt that is present now but was absent when frozen (or the
            # reverse) is a presence-drift refusal, never a silent transition.
            for name in PROXY_OPTIONAL_INPUTS:
                fact = _native_receipt(self.config/'pin.lock', name)
                frozen = self._last_proxy['optional/'+name] if self._last_proxy else None
                if (fact is None) != (frozen is None):
                    raise ValueError('optional_presence_changed')
                if fact is not None:
                    if fact != frozen:
                        raise ValueError('optional_receipt_changed')
                    shutil.copy2(self.config/name, path/name)
                    if _regular(path/name) != fact:
                        raise ValueError('snapshot_changed')
                manifest[name] = fact
            atomic_json(path/'manifest.json', manifest)
            return {k: self._last_frozen[k] for k in ('asset_digest', 'runtime_digest')}
        except Exception:
            raise Fault('lkg_invalid') from None

    def validate_lkg(self, path, binding):
        try:
            manifest = _read_json(path/'manifest.json')
            names = set(PROXY_INPUTS) | {self.label+'.plist', self.label+'.rotate.plist'}
            optional = self._optional_names()
            frozen = self._last_proxy or {}
            expected = {**{n: frozen[n] for n in names},
                        **{n: frozen.get('optional/'+n) for n in optional}}
            if (set(manifest) != names | optional or manifest != expected
                    or self._last_frozen is None or binding != {
                    k: self._last_frozen[k] for k in ('asset_digest', 'runtime_digest')}):
                raise ValueError('snapshot_invalid')
            if any(_regular(path/name) != manifest[name] for name in names):
                raise ValueError('snapshot_invalid')
            # Explicit absence: a receipt recorded absent in the manifest must
            # be exactly absent on disk; a recorded fact must match bytes.
            for name in optional:
                recorded = manifest[name]
                copy = path/name
                if recorded is None:
                    if copy.exists() or copy.is_symlink():
                        raise ValueError('snapshot_invalid')
                elif _regular(copy) != recorded:
                    raise ValueError('snapshot_invalid')
        except Exception:
            raise Fault('lkg_invalid') from None

    def restore_lkg(self, path, binding):
        proof = self.freeze()
        if proof['runtime_digest'] != binding['runtime_digest'] or proof['asset_digest'] != binding['asset_digest']:
            raise Fault('lkg_invalid')
        self.validate_lkg(path, binding)
        manifest = _read_json(path/'manifest.json')
        for name, fact in manifest.items():
            if fact is None:
                continue
            temporary = self.config/('.'+name+'.rollback')
            if temporary.exists() or temporary.is_symlink():
                raise Fault('lkg_invalid')
            shutil.copy2(path/name, temporary)
            if _regular(temporary) != fact:
                raise Fault('lkg_invalid')
            os.replace(temporary, self.config/name)
        # Exact presence/absence restore: a receipt recorded absent must
        # already be exactly absent live; a stale receipt is never silently
        # deleted to force success during legacy transition.
        for name in self._optional_names():
            live = self.config/name
            if manifest[name] is None and (live.exists() or live.is_symlink()):
                raise Fault('lkg_invalid')
        if self.freeze() != proof:
            raise Fault('lkg_invalid')

    def qualify(self, plan):
        try:
            record = _read_json(self.state/'qualification.json')
        except FileNotFoundError:
            raise Fault('qualification_missing') from None
        try:
            own = process_identity(os.getpid())
            expected = controller_job(self.policy, self.policy_path, self.python)
            supervisor = Path.home()/'Library/LaunchAgents'/(self.policy['controller_label']+'.plist')
            supervisor_fact = _regular(supervisor)
            if (supervisor_fact['mode'] != 0o600 or supervisor.stat().st_uid != os.getuid()
                    or supervisor.read_bytes() != plistlib.dumps(expected, sort_keys=True)):
                raise ValueError('controller_supervision_invalid')
            job = self._job_state(self.policy['controller_label'], expected=expected, expected_path=supervisor)
            fields = {'version', 'operator_uid', 'controller_label', 'controller_identity',
                      'controller_digest', 'policy_digest', 'plist_digest', 'controller_epoch',
                      'issued_at', 'expires_at', 'issued_monotonic', 'expires_monotonic', 'canary_evidence_digest',
                      'controller_plist_digest', 'controller_supervision'}
            if (set(record) != fields or type(record['version']) is not int or record['version'] != 1
                    or type(record['operator_uid']) is not int):
                raise ValueError('qualification_invalid')
            identity = {k: own[k] for k in ('pid', 'uid', 'birth')}
            if (own['ppid'] != 1 or record['operator_uid'] != os.getuid() or not job or not job.get('parity') or job['identity'] != identity
                    or canonical_digest(record['controller_identity']) != canonical_digest(identity)
                    or record['controller_label'] != self.policy['controller_label']
                    or record['controller_digest'] != plan['controller_digest']
                    or record['policy_digest'] != plan['policy_digest']
                    or record['plist_digest'] != _regular(self.plist)['sha256']
                    or record['controller_epoch'] != plan['controller_epoch']
                    or record['controller_plist_digest'] != supervisor_fact['sha256']
                    or canonical_digest(record['controller_supervision']) != canonical_digest(controller_supervision(expected))
                    or not re.fullmatch('[0-9a-f]{64}', record['canary_evidence_digest'])):
                raise ValueError('qualification_invalid')
            for first, last, now in (('issued_at', 'expires_at', time.time()),
                                     ('issued_monotonic', 'expires_monotonic', time.monotonic())):
                if not _finite(record[first]) or not _finite(record[last]) or not (
                        record[first] <= now < record[last] and 0 < record[last]-record[first] <= 24*60*60):
                    raise ValueError('qualification_expired')
        except Exception:
            raise Fault('qualification_invalid') from None

    def stop(self):
        self._capture(['/bin/launchctl', 'bootout', f'gui/{os.getuid()}/'+self.label], 30, control=True)
        deadline = time.monotonic()+20
        while self._listener(self.port) is not None:
            if time.monotonic() >= deadline:
                raise Fault('service_control_timeout')
            time.sleep(.5)
        time.sleep(6)

    def start(self):
        self._bootstrap_digest = _regular(self.plist)
        self._capture(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(self.auto_plist)], 30, control=True)
        self._started_at, self._started_identity = time.monotonic(), None

    def accept_started(self):
        def guard():
            if _regular(self.plist) != self._bootstrap_digest:
                raise Fault('postcheck_failed')
            job = self._job_state()
            if not job or not job['parity']:
                raise Fault('postcheck_failed')
            if job['runs'] == 0 and job['identity'] is None and self._started_identity is None:
                return None
            if job['runs'] != 1 or job['identity'] is None:
                raise Fault('postcheck_failed')
            if self._started_identity is None:
                self._started_identity = job['identity']
            if job['identity'] != self._started_identity:
                raise Fault('postcheck_failed')
            listener = self._listener(self.port)
            if listener is not None and listener != job['identity']:
                raise Fault('postcheck_failed')
            return listener
        checks = self._accept(self.port, guard, self._started_at+180, startup=True)
        identity = guard()
        if identity is None:
            raise Fault('postcheck_failed')
        atomic_json(self.state/'bootstrap.json', self._bootstrap_fact(identity))
        return {'identity': identity, 'checks': checks}
