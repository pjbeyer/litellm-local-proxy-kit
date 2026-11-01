"""Installed-layout regressions: exercise the real installer in a sandbox.

The fake interpreter bypasses only the LiteLLM sentinel gate. Renderer,
manifest parser, plist renderer, apply and verify all run their real code.
No service, credential lookup, or launchctl operation is invoked.
"""
import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "core/installer.sh"


def sandbox(tmp_path):
    root = tmp_path / "deployment"
    dep = root / "deployments/test"
    dep.mkdir(parents=True)
    config = tmp_path / "installed"
    logs = tmp_path / "logs"
    interpreter = tmp_path / "python"
    interpreter.write_text(
        '#!/bin/bash\ncase "$1" in */check_patch_sentinel.py|*/check_pin.py) exit 0;; esac\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n'
    )
    interpreter.chmod(0o755)
    fields = {
        "LITELLM_DEPLOYMENT": "test", "LITELLM_CONFIG_DIR": str(config),
        "LITELLM_LOG_DIR": str(logs), "LITELLM_PROXY_LABEL": "test.proxy",
        "LITELLM_LOCAL_PORT": "4998", "LITELLM_PROXY_PYTHON": str(interpreter),
        "OP_KEYCHAIN_SERVICE": "test", "OP_KEYCHAIN_ACCOUNT": "test",
    }
    (root / "deploy.conf.sh").write_text(
        "".join(f"{k}={shlex.quote(v)}\n" for k, v in fields.items())
    )
    (dep / "overlay.yaml").write_text(
        "upstream:\n  api_base: https://example.invalid/v1\n"
        "  api_key_env: UPSTREAM_KEY\n  models: []\n"
        "master_key_env: LOCAL_KEY\n"
        "runtime_environment:\n"
        "  DATABASE_URL: postgresql://test@127.0.0.1:5432/test\n"
        "  LITELLM_MCP_STDIO_EXTRA_COMMANDS: obscura,dayflow\n"
        "  LITELLM_ENABLE_MCP_STDIO: 'true'\n"
        "  LITELLM_MCP_TOOL_LISTING_TIMEOUT: '10'\n"
        "  TYPESAFE_API_BASE: https://gateway.example.invalid/typesafe\n"
    )
    (dep / "credentials.toml").write_text(
        '[[credential]]\nenv_var = "LOCAL_KEY"\n'
        'op_ref = "op://test/item/credential"\nrequired = true\n'
    )
    (dep / "pin.lock").write_text('base_version = "test"\n')
    return root, dep, config


def run(root, mode):
    env = {**os.environ, "PYTHONPATH": "", "PYTHONHOME": ""}
    return subprocess.run(["/bin/bash", str(INSTALLER), str(root), mode],
                          env=env, capture_output=True, text=True, timeout=30)


def test_installer_ships_manifest_and_verify_detects_loss(tmp_path):
    root, dep, config = sandbox(tmp_path)
    dry = run(root, "--dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert not config.exists()
    applied = run(root, "apply")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    plist = plistlib.loads((config / "test.proxy.plist").read_bytes())
    manifest = Path(plist["EnvironmentVariables"]["CREDENTIALS_MANIFEST"])
    assert manifest.is_file(), "installer omitted the manifest its plist requires"
    assert manifest.read_bytes() == (dep / "credentials.toml").read_bytes()
    assert run(root, "--verify").returncode == 0
    manifest.unlink()
    assert run(root, "--verify").returncode != 0
    assert run(root, "apply").returncode == 0
    manifest.write_text("# unexpected drift\n")
    assert run(root, "--verify").returncode != 0


def test_installer_preserves_runtime_environment_in_plist(tmp_path):
    root, dep, config = sandbox(tmp_path)
    result = run(root, "apply")
    assert result.returncode == 0, result.stdout + result.stderr
    env = plistlib.loads((config / "test.proxy.plist").read_bytes())["EnvironmentVariables"]
    assert "DATABASE_URL" in env, "installer omitted database runtime state"
    assert env["DATABASE_URL"] == "postgresql://test@127.0.0.1:5432/test"
    assert env["LITELLM_MCP_STDIO_EXTRA_COMMANDS"] == "obscura,dayflow"
    assert env["LITELLM_ENABLE_MCP_STDIO"] == "true"
    assert env["LITELLM_MCP_TOOL_LISTING_TIMEOUT"] == "10"
    assert env["TYPESAFE_API_BASE"] == "https://gateway.example.invalid/typesafe"
    assert run(root, "--verify").returncode == 0


def test_runtime_environment_cannot_override_auth_contract(tmp_path):
    root, dep, _ = sandbox(tmp_path)
    with (dep / "overlay.yaml").open("a") as stream:
        stream.write("  CREDENTIALS_MANIFEST: /untrusted/file\n")
    assert run(root, "apply").returncode != 0


def test_runtime_environment_rejects_ambiguous_stdio_enablement(tmp_path):
    root, dep, _ = sandbox(tmp_path)
    p = dep / "overlay.yaml"
    p.write_text(p.read_text().replace("'true'", "'yes'"))
    assert run(root, "apply").returncode != 0


def test_runtime_environment_rejects_invalid_listing_timeout(tmp_path):
    root, dep, _ = sandbox(tmp_path)
    p = dep / "overlay.yaml"
    p.write_text(p.read_text().replace("'10'", "'0.5'"))
    assert run(root, "apply").returncode != 0
    p.write_text(p.read_text().replace("'0.5'", "not-a-number"))
    assert run(root, "apply").returncode != 0


def test_runtime_environment_rejects_password_literal(tmp_path):
    root, dep, _ = sandbox(tmp_path)
    p = dep / "overlay.yaml"
    p.write_text(p.read_text().replace("test@127.0.0.1", "test:not-a-real-password@127.0.0.1"))
    r = run(root, "apply")
    assert r.returncode != 0
    assert "not-a-real-password" not in r.stdout + r.stderr


def test_runtime_environment_rejects_credential_bearing_typesafe_base(tmp_path):
    root, dep, _ = sandbox(tmp_path)
    p = dep / "overlay.yaml"
    credential_bearing_url = (
        "https://" + "not-a-real-key" + "@gateway.example.invalid/typesafe?"
        + "token" + "=" + "not-a-real-token"
    )
    p.write_text(p.read_text().replace(
        "https://gateway.example.invalid/typesafe", credential_bearing_url
    ))
    r = run(root, "apply")
    assert r.returncode != 0
    # The persisted-configuration guard must not echo sensitive URL components.
    assert "not-a-real-key" not in r.stdout + r.stderr
    assert "not-a-real-token" not in r.stdout + r.stderr


def test_installer_refuses_missing_credential_before_applying(tmp_path):
    root, dep, config = sandbox(tmp_path)
    assert run(root, "apply").returncode == 0
    before = {p.name: p.read_bytes() for p in config.iterdir()}
    overlay = dep / "overlay.yaml"
    overlay.write_text(overlay.read_text() + '\nmcp_servers:\n  relay:\n    auth_value: os.environ/RELAY_KEY\n')
    result = run(root, "apply")
    assert result.returncode == 78
    assert "undeclared config credentials: RELAY_KEY" in result.stdout + result.stderr
    assert {p.name: p.read_bytes() for p in config.iterdir()} == before


def test_installer_ships_pin_and_coverage_guard(tmp_path):
    root, dep, config = sandbox(tmp_path)
    assert run(root, "apply").returncode == 0
    assert (config / "pin.lock").read_bytes() == (dep / "pin.lock").read_bytes()
    assert (config / "check_credential_coverage.py").is_file()
    (config / "pin.lock").unlink()
    assert run(root, "--verify").returncode != 0


def test_installer_replaces_entrypoint_without_overwriting_open_inode(tmp_path):
    root, _, config = sandbox(tmp_path)
    assert run(root, "apply").returncode == 0
    wrapper = config / 'run-proxy-wrapper.sh'
    wrapper.write_text('old-running-entrypoint\n')
    with wrapper.open('rb') as old_inode:
        assert run(root, 'apply').returncode == 0
        assert old_inode.read() == b'old-running-entrypoint\n'
    assert wrapper.read_bytes() == (REPO / 'core/run-proxy-wrapper.sh').read_bytes()
    assert not list(config.glob('.*.install.*'))
