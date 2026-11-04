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
import subprocess
import time
import threading

from .engine import Fault, atomic_json, canonical_digest, private_dir, _read_json
from .process_identity import process_identity
from .runtime_identity import (CONTROLLER_INPUTS, PROXY_INPUTS, file_fact, source_facts)

POLICY_FIELDS = {'version', 'kit_root', 'deployment_root', 'plist', 'auto_plist',
                 'state_dir', 'scratch_dir', 'candidate_port', 'models',
                 'controller_root', 'controller_label'}


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


def _regular(path):
    try:
        return file_fact(path)
    except Exception:
        raise Fault('runtime_invalid') from None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class Runtime:
    fixture = False

    def __init__(self, policy_path):
        self.policy_path = Path(policy_path).absolute()
        try:
            policy = _read_json(self.policy_path)
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
        except Fault:
            raise
        except Exception:
            raise Fault('runtime_invalid') from None

    def release_credentials(self):
        self._key = None
        self._operation_started = None

    def _group_exists(self, pgid):
        result = subprocess.run(['/bin/ps', '-axo', 'pgid='], env=self.env,
                                capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise Fault('candidate_cleanup_failed')
        return str(pgid) in result.stdout.split()

    def _owned_stop(self, child, birth, grace=20):
        try:
            if child.poll() is not None:
                if self._group_exists(child.pid):
                    raise Fault('candidate_cleanup_failed')
                child.wait()
                return
            if birth is None or _identity(child.pid) != birth or os.getpgid(child.pid) != child.pid:
                raise Fault('candidate_cleanup_failed')
            # Only a retained owned child/group; NEVER the primary listener.
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=grace)
            if self._group_exists(child.pid):
                raise Fault('candidate_cleanup_failed')
        except Exception:
            # The child can exit between poll and kernel identity inspection.
            # Reap that ordinary failure, but never signal an unknown identity.
            with contextlib.suppress(Exception):
                if child.poll() is not None and not self._group_exists(child.pid):
                    child.wait()
                    return
            self._cleanup_blocked = True
            raise Fault('candidate_cleanup_failed') from None

    def _capture(self, argv, timeout=90, env=None, control=False):
        if self._candidate is not None or self._cleanup_blocked:
            raise Fault('candidate_cleanup_failed')
        if self._operation_started is None:
            self._operation_started = time.monotonic()
        remaining = 900 - (time.monotonic() - self._operation_started)
        if remaining <= 0:
            raise Fault('service_control_timeout' if control else 'gate_failed')
        deadline = time.monotonic() + min(timeout, remaining)
        child = subprocess.Popen(argv, env=self.env if env is None else env, cwd=self.config,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        birth = None
        try:
            try:
                birth = _identity(child.pid)
            except Fault:
                if child.poll() is None:
                    raise
            stream = child.stdout
            assert stream is not None  # Popen was explicitly given stdout=PIPE
            out = bytearray()
            with selectors.DefaultSelector() as reader:
                reader.register(stream, selectors.EVENT_READ)
                while True:
                    available = deadline - time.monotonic()
                    if available <= 0 or not reader.select(available):
                        raise subprocess.TimeoutExpired(argv, timeout)
                    part = os.read(stream.fileno(), min(65536, HELPER_OUTPUT_LIMIT + 1 - len(out)))
                    if not part:
                        break
                    out.extend(part)
                    if len(out) > HELPER_OUTPUT_LIMIT:
                        raise Fault('gate_failed')
            child.wait(timeout=max(.001, deadline-time.monotonic()))
            if child.returncode:
                raise Fault('service_control_denied' if control else 'gate_failed')
            return bytes(out)
        except subprocess.TimeoutExpired:
            raise Fault('service_control_timeout' if control else 'gate_failed') from None
        finally:
            try:
                self._owned_stop(child, birth)
            finally:
                if child.stdout is not None:
                    child.stdout.close()

    def _listener(self, port):
        try:
            r = subprocess.run(['/usr/sbin/lsof', '-t', '-nP', '-iTCP:'+str(port), '-sTCP:LISTEN'],
                               env=self.env, capture_output=True, timeout=5)
            values = r.stdout.decode().split()
            if r.returncode == 1 and not values and not r.stderr.strip():
                return None
            if r.returncode or not values or any(not v.isdigit() for v in values):
                raise ValueError('unknown_listener')
            pids = set(map(int, values))
            if len(pids) != 1:
                raise ValueError('ambiguous_listener')
            return _identity(pids.pop())
        except Exception:
            raise Fault('unmanaged_listener') from None

    def _job_state(self, label=None):
        label = label or self.label
        try:
            r = subprocess.run(['/bin/launchctl', 'print', f'gui/{os.getuid()}/'+label],
                               env=self.env, capture_output=True, text=True, timeout=10)
            if r.returncode:
                return None
            text = r.stdout
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
            parity = (field('path') == str(self.auto_plist) and args == self.job['ProgramArguments']
                      and all(actual_env.get(k) == v for k, v in self.job['EnvironmentVariables'].items())
                      and field('minimum runtime') == str(self.supervision['ThrottleInterval']))
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
            proxy = {name: _regular(self.config/name) for name in
                     (*PROXY_INPUTS, self.label+'.plist', self.label+'.rotate.plist')}
            inputs: dict[str, object] = {'deploy.conf.sh': _regular(self.deployment/'deploy.conf.sh'),
                      'auto_plist': _regular(self.auto_plist)}
            for name in ('overlay.yaml', 'credentials.toml', 'pin.lock'):
                inputs['deployment/'+name] = _regular(self.deployment/'deployments'/self.name/name)
            exemptions = self.deployment/'drift-exemptions.yaml'
            inputs['drift-exemptions.yaml'] = _regular(exemptions) if exemptions.exists() else None
            proof = json.loads(self._capture([self.python, str(self.kit/'core/restart_control/runtime_identity.py'),
                                             str(self.config/'pin.lock')], timeout=90))
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
        self._capture(['/bin/bash', str(self.kit/'core/installer.sh'), str(self.deployment), '--verify'], 90)
        self._capture([self.python, str(self.kit/'core/drift_check.py'), '--deployment-root', str(self.deployment)], 90)
        self._capture([*self.job['ProgramArguments'], '--preflight'], 180)
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
                key = self._capture([op, 'read', row['op_ref']], 30, env=env).decode().strip()
                del sa, env
            elif row.get('source') == 'cli-token':
                tool = shutil.which('cli-token', path=self.env['PATH'])
                if not tool:
                    raise ValueError('resolver_unavailable')
                key = self._capture([tool, 'auth', 'print-token'], 30).decode().strip()
            else:
                raise ValueError('master_source_invalid')
            if not key or key.startswith('op://') or any(c.isspace() for c in key):
                raise ValueError('master_unresolved')
            self._key = key
        except Exception:
            raise Fault('gate_failed') from None

    def _request(self, port, path, key=None, payload=None):
        if port not in (self.port, self.policy['candidate_port']) or path not in (
                '/health/liveliness', '/health/readiness', '/v1/models', '/v1/chat/completions'):
            raise Fault('postcheck_failed')
        headers = {'Content-Type': 'application/json'}
        if key is not None:
            headers['Authorization'] = 'Bearer '+key
        if self._operation_started is None:
            self._operation_started = time.monotonic()
        budget = min(REQUEST_TIMEOUT, 900 - (time.monotonic() - self._operation_started))
        if budget <= 0:
            raise Fault('postcheck_failed')
        # http.client has no ambient-proxy or redirect machinery. The fixed
        # literal host, approved port and closed path define the only URL.
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=budget)
        expired, owned_socket = threading.Event(), []
        def expire():
            expired.set()
            if owned_socket:
                with contextlib.suppress(OSError):
                    owned_socket[0].shutdown(socket.SHUT_RDWR)
        timer = threading.Timer(budget, expire)
        timer.daemon = True
        timer.start()
        try:
            connection.connect()
            owned_socket.append(connection.sock)
            if expired.is_set():
                raise Fault('postcheck_failed')
            connection.request('POST' if payload is not None else 'GET', path,
                 body=json.dumps(payload).encode() if payload is not None else None, headers=headers)
            with connection.getresponse() as result:
                if result.status != 200:
                    return result.status, {}
                body = result.read(1024 * 1024 + 1)
                if expired.is_set() or len(body) > 1024 * 1024:
                    raise Fault('postcheck_failed')
                return result.status, json.loads(body)
        except Exception:
            raise Fault('postcheck_failed') from None
        finally:
            timer.cancel()
            connection.close()

    def _accept(self, port, guard, deadline, startup=False):
        while True:
            identity = guard()
            try:
                ready = identity is not None and self._request(port, '/health/readiness')[0] == 200
            except Fault:
                ready = False
            guard()
            if ready:
                break
            if not startup or time.monotonic() >= deadline:
                raise Fault('postcheck_failed')
            time.sleep(.5)
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
        port = self.policy['candidate_port']
        if self._candidate is not None or self._listener(port) is not None:
            raise Fault('candidate_failed')
        argv = [*self.job['ProgramArguments'][:2], str(port)]
        child = subprocess.Popen(argv, env=self.env, cwd=self.config, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
        self._candidate = child
        birth = None
        def guard():
            if child.poll() is not None or _identity(child.pid) != birth:
                raise Fault('candidate_failed')
            listener = self._listener(port)
            if listener is not None and listener != birth:
                raise Fault('candidate_failed')
            return listener
        try:
            birth = _identity(child.pid)
            return self._accept(port, guard, time.monotonic()+180, startup=True)
        except Fault:
            raise Fault('candidate_failed') from None
        finally:
            self._owned_stop(child, birth)
            if self._listener(port) is not None:
                raise Fault('candidate_cleanup_failed')
            self._candidate = None

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
            atomic_json(path/'manifest.json', manifest)
            return {k: self._last_frozen[k] for k in ('asset_digest', 'runtime_digest')}
        except Exception:
            raise Fault('lkg_invalid') from None

    def validate_lkg(self, path, binding):
        try:
            manifest = _read_json(path/'manifest.json')
            names = set(PROXY_INPUTS) | {self.label+'.plist', self.label+'.rotate.plist'}
            if (set(manifest) != names or manifest != self._last_proxy
                    or self._last_frozen is None or binding != {
                        k: self._last_frozen[k] for k in ('asset_digest', 'runtime_digest')}):
                raise ValueError('snapshot_invalid')
            if any(_regular(path/name) != manifest[name] for name in names):
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
            temporary = self.config/('.'+name+'.rollback')
            if temporary.exists() or temporary.is_symlink():
                raise Fault('lkg_invalid')
            shutil.copy2(path/name, temporary)
            if _regular(temporary) != fact:
                raise Fault('lkg_invalid')
            os.replace(temporary, self.config/name)
        if self.freeze() != proof:
            raise Fault('lkg_invalid')

    def qualify(self, plan):
        try:
            record = _read_json(self.state/'qualification.json')
        except FileNotFoundError:
            raise Fault('qualification_missing') from None
        try:
            own = process_identity(os.getpid())
            job = self._job_state(self.policy['controller_label'])
            fields = {'version', 'operator_uid', 'controller_label', 'controller_identity',
                      'controller_digest', 'policy_digest', 'plist_digest', 'controller_epoch',
                      'issued_at', 'expires_at', 'issued_monotonic', 'expires_monotonic', 'canary_evidence_digest'}
            if (set(record) != fields or type(record['version']) is not int or record['version'] != 1
                    or type(record['operator_uid']) is not int):
                raise ValueError('qualification_invalid')
            identity = {k: own[k] for k in ('pid', 'uid', 'birth')}
            if (own['ppid'] != 1 or record['operator_uid'] != os.getuid() or not job or job['identity'] != identity
                    or record['controller_identity'] != identity
                    or record['controller_label'] != self.policy['controller_label']
                    or record['controller_digest'] != plan['controller_digest']
                    or record['policy_digest'] != plan['policy_digest']
                    or record['plist_digest'] != _regular(self.plist)['sha256']
                    or record['controller_epoch'] != plan['controller_epoch']
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
