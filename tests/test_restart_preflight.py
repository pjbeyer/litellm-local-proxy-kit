"""Credential parity and non-disruptive installed-wrapper preflight regressions."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

CORE = Path(__file__).resolve().parents[1] / 'core'


def manifest(tmp_path, extra=''):
    p = tmp_path / 'credentials.toml'
    p.write_text('[[credential]]\nenv_var = "LOCAL_KEY"\n'
                 'op_ref = "op://fixture/item/credential"\nrequired = true\n'
                 '[[credential]]\nenv_var = "RELAY_KEY"\n'
                 'source = "cli-token"\nrequired = true\n' + extra)
    return p


def test_parser_accepts_closed_cli_source(tmp_path):
    r = subprocess.run([sys.executable, str(CORE / 'parse_credentials.py'), str(manifest(tmp_path))], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert 'RELAY_KEY\t-\t1\tcli-token' in r.stdout


def test_parser_rejects_cli_op_ref(tmp_path):
    r = subprocess.run([sys.executable, str(CORE / 'parse_credentials.py'), str(manifest(tmp_path, 'op_ref = "op://fixture/other/credential"\n'))], capture_output=True, text=True)
    assert r.returncode != 0


@pytest.mark.parametrize('port_args', [[], ['44001']])
@pytest.mark.parametrize('command,expected', [
    ('printf "fixture-relay-value\\n"', 0),
    ('exit 1', 78),
    ('exit 0', 78),
    ('printf "op://fixture/unresolved\\n"', 78),
    ('printf "line1\\nline2\\n"', 78),
])
def test_preflight_cli_value_evidence(tmp_path, command, expected, port_args):
    # This fake launcher records ANY launch, so --preflight must never reach it.
    launch = tmp_path / 'launched'
    python = tmp_path / 'python'
    python.write_text('#!/bin/sh\nexit 0\n')
    python.chmod(0o755)
    litellm = tmp_path / 'litellm'
    litellm.write_text(f'#!/bin/sh\ntouch "{launch}"\n')
    litellm.chmod(0o755)
    op = tmp_path / 'op'
    op.write_text('#!/bin/sh\nprintf "fixture-master-value\\n"\n')
    op.chmod(0o755)
    cli = tmp_path / 'cli'
    cli.write_text('#!/bin/sh\n[ "$*" = "auth print-token" ] || exit 2\n' + command + '\n')
    cli.chmod(0o755)
    config = tmp_path / 'config.yaml'
    config.write_text('general_settings:\n  master_key: os.environ/LOCAL_KEY\nrelay: os.environ/RELAY_KEY\n')
    env = {
        'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin',
        'TMPDIR': os.environ['TMPDIR'],
        'OP_SERVICE_ACCOUNT_TOKEN': 'fixture-service-account',
        'LITELLM_DEPLOYMENT': 'test', 'OP_BIN': str(op),
        'CLI_TOKEN_BIN': str(cli), 'CREDENTIALS_MANIFEST': str(manifest(tmp_path)),
        'LITELLM_PROXY_CONFIG': str(config), 'LITELLM_PROXY_PYTHON': str(python),
    }
    installed = tmp_path / 'installed'
    installed.mkdir()
    for name in ('run-proxy-wrapper.sh', 'parse_credentials.py', 'check_pin.py', 'check_patch_sentinel.py'):
        (installed / name).write_bytes((CORE / name).read_bytes())
    (installed / 'pin.lock').write_text('base_version = "fixture"\n')
    r = subprocess.run(['/bin/bash', str(installed / 'run-proxy-wrapper.sh'), *port_args, '--preflight'], env=env, capture_output=True, text=True, timeout=15)
    assert r.returncode == expected, r.stdout + r.stderr
    assert not launch.exists(), 'preflight must not execute a proxy launcher'
    assert 'fixture-relay-value' not in r.stdout + r.stderr
    assert 'fixture-master-value' not in r.stdout + r.stderr
    if expected == 0:
        assert 'preflight complete' in r.stdout


def test_unknown_wrapper_argument_refused():
    r = subprocess.run(['/bin/bash', str(CORE / 'run-proxy-wrapper.sh'), '--unknown'], capture_output=True, text=True)
    assert r.returncode == 64


def test_managed_wrapper_preserves_occupied_listener(tmp_path):
    import http.server
    import threading
    import urllib.request
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'original')
        def log_message(self, format, *args):
            pass
    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        r = subprocess.run(['/bin/bash', str(CORE / 'run-proxy-wrapper.sh'), str(port)],
                           env={'HOME':str(tmp_path), 'PATH':'/usr/bin:/bin',
                                'LITELLM_DEPLOYMENT':'test', 'LITELLM_PROXY_LABEL':'test.proxy'},
                           capture_output=True, text=True, timeout=10)
        assert r.returncode == 78
        assert 'preserving existing listener' in r.stdout
        assert 'resolved' not in r.stdout
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}', timeout=5) as response:
            assert response.status == 200 and response.read() == b'original'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('port', ['not-a-port', '0', '65536'])
def test_managed_port_probe_fails_closed(port, tmp_path):
    r = subprocess.run(['/bin/bash', str(CORE / 'run-proxy-wrapper.sh')],
                       env={'HOME':str(tmp_path), 'PATH':'/usr/bin:/bin',
                            'LITELLM_DEPLOYMENT':'test', 'LITELLM_PROXY_LABEL':'test.proxy',
                            'LITELLM_LOCAL_PORT':port}, capture_output=True, text=True, timeout=10)
    assert r.returncode == 78
    assert 'cannot establish loopback port availability' in r.stdout
