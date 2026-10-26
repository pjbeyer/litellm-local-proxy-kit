"""Core tests for the shared deployment core (spec 015 T007).

Fail-closed tests prove BOTH directions per the negative-case gate rule:
each failure mode must exit 78 with no listener; the success path must
actually start a server (covered by live stage runs, recorded in evidence/).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"

WRAPPER = CORE / "run-proxy-wrapper.sh"
RENDER = CORE / "render_config.py"
PLIST = CORE / "install_launchagent.py"
PARSE = CORE / "parse_credentials.py"
SENTINEL = CORE / "check_patch_sentinel.py"

OP_SHIM_DIR = Path("/tmp/litellm-kit-test-opshim")


def _write_op_shim(tmp_path: Path) -> Path:
    shim_dir = tmp_path / "opshim"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "op"
    shim.write_text("#!/bin/sh\necho test-key-value\n")
    shim.chmod(0o755)
    return shim_dir


def _run_wrapper(env: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    shim = _write_op_shim(tmp_path)
    base = {
        "HOME": str(tmp_path),
        "PATH": f"{shim}:/usr/bin:/bin",
        "OP_SERVICE_ACCOUNT_TOKEN": "test-token",
        "LITELLM_DEPLOYMENT": "test",
        "LITELLM_PROXY_PYTHON": "/usr/bin/true",
        "OP_BIN": str(shim / "op"),
    }
    base.update(env)
    return subprocess.run(
        ["bash", str(WRAPPER)], env=base, capture_output=True, text=True, timeout=60
    )


def _write_creds(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "credentials.toml"
    p.write_text(
        "[[credential]]\n"
        'env_var = "LITELLM_API_KEY"\n'
        'op_ref = "op://testvault/testitem/credential"\n'
        "required = true\n" + extra
    )
    return p


def _write_config(tmp_path: Path, master_var: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(f"general_settings:\n  master_key: os.environ/{master_var}\n")
    return p


# ---------- renderer (FR-002/003/004) ----------

def test_render_deterministic_and_refs_only(tmp_path):
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "upstream:\n"
        "  api_base: https://example.invalid/v1\n"
        "  api_key_env: LITELLM_API_KEY\n"
        "  models:\n"
        "    - name: test-model\n"
        "  wildcard: true\n"
        "master_key_env: LITELLM_LOCAL_KEY\n"
        "litellm_settings:\n"
        "  telemetry: false\n"
    )
    template = tmp_path / "template.txt"
    template.write_text("# header\n# ===BEGIN GENERATED===\n")
    out1 = tmp_path / "out1.yaml"
    out2 = tmp_path / "out2.yaml"
    for out in (out1, out2):
        r = subprocess.run(
            [sys.executable, str(RENDER), "--overlay", str(overlay), "--template", str(template), "--out", str(out)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
    assert out1.read_bytes() == out2.read_bytes(), "renderer must be byte-deterministic (FR-003)"
    text = out1.read_text()
    assert "os.environ/LITELLM_API_KEY" in text, "refs-only output (FR-004)"
    assert "sk-" not in text.lower()


def test_render_staleness_check_fails_on_divergence(tmp_path):
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "upstream:\n  api_base: https://example.invalid/v1\n"
        "  api_key_env: K\n  models: []\n  wildcard: false\nmaster_key_env: MK\n"
    )
    template = tmp_path / "template.txt"
    template.write_text("# ===BEGIN GENERATED===\n")
    stale = tmp_path / "stale.yaml"
    stale.write_text("# wrong content\n")
    r = subprocess.run(
        [sys.executable, str(RENDER), "--overlay", str(overlay), "--template", str(template), "--check", str(stale)],
        capture_output=True, text=True,
    )
    assert r.returncode == 1, "staleness gate must fail on divergence"


# ---------- wrapper fail-closed ladder (FR-005, both directions) ----------

def test_wrapper_fails_closed_without_token(tmp_path):
    shim = _write_op_shim(tmp_path)
    env = {
        "PATH": f"{shim}:/usr/bin:/bin",  # no OP_SERVICE_ACCOUNT_TOKEN
        "OP_BIN": str(shim / "op"),
        "LITELLM_DEPLOYMENT": "test",
        "LITELLM_PROXY_PYTHON": "/usr/bin/true",
    }
    r = subprocess.run(["bash", str(WRAPPER)], env={**{"HOME": str(tmp_path)}, **env},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 78, f"expected EX_CONFIG 78, got {r.returncode}: {r.stderr}"


def test_wrapper_fails_closed_on_unresolvable_required_credential(tmp_path):
    creds = _write_creds(tmp_path)
    config = _write_config(tmp_path, "LITELLM_API_KEY")
    # op shim returns a value, so resolution succeeds — instead break op by
    # pointing OP_BIN at /usr/bin/false
    shim = _write_op_shim(tmp_path)
    false_bin = tmp_path / "opshim" / "op"
    false_bin.write_text("#!/bin/sh\nexit 1\n")
    r = subprocess.run(
        ["bash", str(WRAPPER)],
        env={
            "HOME": str(tmp_path),
            "PATH": f"{shim}:/usr/bin:/bin",
            "OP_SERVICE_ACCOUNT_TOKEN": "tok",
            "OP_BIN": str(false_bin),
            "LITELLM_DEPLOYMENT": "test",
            "LITELLM_PROXY_CONFIG": str(config),
            "CREDENTIALS_MANIFEST": str(creds),
            "LITELLM_PROXY_PYTHON": "/usr/bin/true",
        },
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 78, "required credential unresolvable must refuse to start"
    assert "refusing" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"


def test_wrapper_fails_closed_on_renamed_master_key_var(tmp_path):
    """F-CI-1 regression: config references a var the wrapper did not resolve."""
    creds = _write_creds(tmp_path)
    config = _write_config(tmp_path, "LITELLM_MASTER_KEY")  # NOT in the manifest
    r = _run_wrapper({"LITELLM_PROXY_CONFIG": str(config), "CREDENTIALS_MANIFEST": str(creds)}, tmp_path)
    assert r.returncode == 78, "renamed master-key var must be caught (fail closed)"
    assert "NOT resolved by this wrapper" in r.stdout + r.stderr


def test_wrapper_master_key_gate_passes_when_var_resolved(tmp_path):
    """Control: config references the manifest-resolved var -> gate passes,
    then the sentinel check fails on /usr/bin/true (no litellm) with exit 78.
    Either way it must NOT be the master-key gate that fails."""
    creds = _write_creds(tmp_path)
    config = _write_config(tmp_path, "LITELLM_API_KEY")
    r = _run_wrapper({"LITELLM_PROXY_CONFIG": str(config), "CREDENTIALS_MANIFEST": str(creds)}, tmp_path)
    assert "master-key gate OK" in r.stdout + r.stderr, r.stdout + r.stderr
    assert r.returncode == 78  # fails later, at sentinel on a python without litellm
    assert "master-key" not in r.stdout.split("master-key gate OK")[-1], "gate passed — later stage must be the failure"


def test_wrapper_sentinel_negative_on_python_without_litellm(tmp_path):
    creds = _write_creds(tmp_path)
    config = _write_config(tmp_path, "LITELLM_API_KEY")
    r = _run_wrapper(
        {"LITELLM_PROXY_CONFIG": str(config), "CREDENTIALS_MANIFEST": str(creds),
         "LITELLM_PROXY_PYTHON": "/usr/bin/python3"},
        tmp_path,
    )
    assert r.returncode == 78
    assert "patch assertion FAILED" in r.stdout + r.stderr, "stock build must be refused (F-CI-2)"


# ---------- coverage gate: config references vs manifest rows (FR-005) ----------

def test_wrapper_fails_closed_on_omitted_manifest_row(tmp_path):
    """Regression for the Phase-3 drill finding: a required credential row
    deleted from the manifest previously let the wrapper reach launch; the
    upstream proxy then silently DROPS the dependent deployment while serving
    everything else (verified live: liveliness 200 with smart-router dropped).
    The coverage gate must refuse before launch."""
    creds = _write_creds(tmp_path)  # only LITELLM_API_KEY declared
    # Config references TWO vars; TYPESAFE_API_KEY has no manifest row at all.
    config = tmp_path / "config.yaml"
    config.write_text(
        "general_settings:\n  master_key: os.environ/LITELLM_API_KEY\n"
        "model_list:\n"
        "  - model_name: smart-router\n"
        "    litellm_params:\n"
        "      model: auto_router/complexity_router\n"
        "      api_key: os.environ/TYPESAFE_API_KEY\n"
    )
    r = _run_wrapper({"LITELLM_PROXY_CONFIG": str(config), "CREDENTIALS_MANIFEST": str(creds)}, tmp_path)
    assert r.returncode == 78, f"omitted row must refuse to start, got {r.returncode}: {r.stdout} {r.stderr}"
    assert "no manifest row covers it" in r.stdout + r.stderr


def test_wrapper_coverage_gate_accepts_env_resolved_var(tmp_path):
    """Control: an unset-in-manifest var that IS non-empty in the environment
    (operator env / shim source) is covered — gate passes and the run proceeds
    to the sentinel stage (exit 78 there, on a python without litellm)."""
    creds = _write_creds(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        "general_settings:\n  master_key: os.environ/LITELLM_API_KEY\n"
        "model_list:\n"
        "  - model_name: smart-router\n"
        "    litellm_params:\n"
        "      model: auto_router/complexity_router\n"
        "      api_key: os.environ/TYPESAFE_API_KEY\n"
    )
    r = _run_wrapper(
        {"LITELLM_PROXY_CONFIG": str(config), "CREDENTIALS_MANIFEST": str(creds),
         "TYPESAFE_API_KEY": "fixture-env-value", "LITELLM_PROXY_PYTHON": "/usr/bin/python3"},
        tmp_path,
    )
    assert "coverage gate OK" in r.stdout + r.stderr, r.stdout + r.stderr
    assert r.returncode == 78  # fails later at sentinel, not at coverage
    assert "no manifest row covers it" not in r.stdout + r.stderr


def test_wrapper_coverage_gate_allows_optional_manifest_row(tmp_path):
    """An optional manifest row is a documented degrade path: covered even when
    unresolvable, per the JUSTIFIED_WARN classification."""
    creds = _write_creds(
        tmp_path,
        extra='[[credential]]\nenv_var = "TYPESAFE_API_KEY"\n'
              'op_ref = "op://testvault/optional/credential"\n'
              "required = false\n",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "general_settings:\n  master_key: os.environ/LITELLM_API_KEY\n"
        "model_list:\n"
        "  - model_name: smart-router\n"
        "    litellm_params:\n"
        "      model: auto_router/complexity_router\n"
        "      api_key: os.environ/TYPESAFE_API_KEY\n"
    )
    # op shim returns a value, so the optional row resolves fine; to exercise
    # the optional-unresolved branch, point OP_BIN at a failing op for it.
    shim = _write_op_shim(tmp_path)
    # First op read succeeds for LITELLM_API_KEY, fails for TYPESAFE's ref:
    flaky = tmp_path / "opshim2"
    flaky.mkdir()
    (flaky / "op").write_text('#!/bin/sh\ncase "$*" in *optional*) exit 1;; *) echo test-key-value;; esac\n')
    (flaky / "op").chmod(0o755)
    r = _run_wrapper(
        {"LITELLM_PROXY_CONFIG": str(config), "CREDENTIALS_MANIFEST": str(creds),
         "OP_BIN": str(flaky / "op"), "LITELLM_PROXY_PYTHON": "/usr/bin/python3"},
        tmp_path,
    )
    assert "coverage: TYPESAFE_API_KEY declared in the manifest" in r.stdout + r.stderr, r.stdout + r.stderr
    assert "WARN (JUSTIFIED): optional credential TYPESAFE_API_KEY" in r.stdout + r.stderr
    assert "no manifest row covers it" not in r.stdout + r.stderr
    assert r.returncode == 78  # sentinel stage, not coverage


# ---------- sentinel checker (FR-006 positive assertion) ----------

def test_sentinel_checker_positive_and_negative(tmp_path):
    good = tmp_path / "mod.py"
    good.write_text("args_already_complete = set()\n")
    pkg = tmp_path / "fakepkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    pkg = tmp_path / "sentinelpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("args_already_complete = set()\n")
    env = {"PYTHONPATH": str(tmp_path), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, str(SENTINEL), "args_already_complete", "sentinelpkg.mod"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, str(SENTINEL), "NOT_THERE", "sentinelpkg.mod"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1, "absent sentinel must fail"


# ---------- plist templating (Contract 4) ----------

NEW_ARGS = ["--deployment", "work", "--proxy-python", "/tmp/venv/bin/python",
            "--op-keychain-service", "test-sa", "--op-keychain-account", "testuser"]

def test_plist_render_and_check(tmp_path):
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, str(PLIST), "--label", "test.proxy", "--port", "4001",
         "--log-dir", str(tmp_path / "logs"), "--wrapper-dir", "/tmp/wd",
         "--work-dir", "/tmp/work", "--out", str(out)] + NEW_ARGS,
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert (out / "test.proxy.plist").exists()
    assert (out / "test.proxy.rotate.plist").exists()
    import plistlib
    d = plistlib.loads((out / "test.proxy.plist").read_bytes())
    assert d["Label"] == "test.proxy"
    assert d["ProgramArguments"][-1] == "4001"
    assert d["StandardOutPath"].endswith("proxy.log")
    # wrapper-contract keys must be present and fully substituted (no leaks)
    env = d["EnvironmentVariables"]
    assert env["LITELLM_DEPLOYMENT"] == "work"
    assert env["LITELLM_PROXY_PYTHON"] == "/tmp/venv/bin/python"
    assert env["CREDENTIALS_MANIFEST"] == "/tmp/wd/credentials.toml"
    assert env["LITELLM_PROXY_CONFIG"] == "/tmp/wd/config.rendered.yaml"
    assert env["OP_KEYCHAIN_SERVICE"] == "test-sa"
    assert env["OP_KEYCHAIN_ACCOUNT"] == "testuser"
    assert "{{" not in (out / "test.proxy.plist").read_text()
    # check mode: passing
    r = subprocess.run(
        [sys.executable, str(PLIST), "--label", "test.proxy", "--port", "4001",
         "--log-dir", str(tmp_path / "logs"), "--wrapper-dir", "/tmp/wd",
         "--work-dir", "/tmp/work", "--check", str(out)] + NEW_ARGS,
        capture_output=True, text=True,
    )
    assert r.returncode == 0, "check must pass on freshly rendered plists"
    # check mode: stale
    (out / "test.proxy.plist").write_text("<plist></plist>")
    r = subprocess.run(
        [sys.executable, str(PLIST), "--label", "test.proxy", "--port", "4001",
         "--log-dir", str(tmp_path / "logs"), "--wrapper-dir", "/tmp/wd",
         "--work-dir", "/tmp/work", "--check", str(out)] + NEW_ARGS,
        capture_output=True, text=True,
    )
    assert r.returncode == 1, "check must fail on stale plist"


# ---------- manifest parser ----------

def test_parse_credentials_source_semantics(tmp_path):
    # source=aws-sso-shim rows are valid without op_ref and emit the source.
    good = tmp_path / "shim.toml"
    good.write_text('[[credential]]\nenv_var = "AWS_ACCESS_KEY_ID"\nsource = "aws-sso-shim"\nrequired = true\n')
    r = subprocess.run([sys.executable, str(PARSE), str(good)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "AWS_ACCESS_KEY_ID\t-\t1\taws-sso-shim"
    # op_ref + source=aws-sso-shim is contradictory and must fail closed.
    bad = tmp_path / "contradiction.toml"
    bad.write_text('[[credential]]\nenv_var = "X"\nsource = "aws-sso-shim"\nop_ref = "op://a/b/c"\nrequired = true\n')
    r = subprocess.run([sys.executable, str(PARSE), str(bad)], capture_output=True, text=True)
    assert r.returncode == 1, "op_ref is invalid for source=aws-sso-shim"
    # unknown source must fail closed.
    bad2 = tmp_path / "unknown.toml"
    bad2.write_text('[[credential]]\nenv_var = "X"\nsource = "carrier-pigeon"\nrequired = true\n')
    r = subprocess.run([sys.executable, str(PARSE), str(bad2)], capture_output=True, text=True)
    assert r.returncode == 1, "unknown source must fail closed"
    # source=op row without op_ref must fail closed.
    bad3 = tmp_path / "no-ref.toml"
    bad3.write_text('[[credential]]\nenv_var = "X"\nrequired = true\n')
    r = subprocess.run([sys.executable, str(PARSE), str(bad3)], capture_output=True, text=True)
    assert r.returncode == 1, "source=op requires op_ref"


def test_render_extra_model_list_and_addendum(tmp_path):
    template = tmp_path / "header.txt"
    template.write_text("# ===BEGIN GENERATED===\n")
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "upstream:\n"
        "  api_base: https://up.example/v1\n"
        "  api_key_env: KEY_ENV\n"
        "  models: []\n"
        "master_key_env: MASTER_ENV\n"
        "extra_model_list:\n"
        "- model_name: bedrock/embed\n"
        "  litellm_params:\n"
        "    model: bedrock/embed\n"
        "litellm_settings_addendum:\n"
        "  mcp_semantic_tool_filter:\n"
        "    enabled: true\n"
    )
    out = tmp_path / "out.yaml"
    r = subprocess.run(
        [sys.executable, str(RENDER), "--overlay", str(overlay), "--template", str(template), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    assert "bedrock/embed" in text
    assert "mcp_semantic_tool_filter" in text and "enabled: true" in text
    # addendum conflicting with litellm_settings must fail closed
    overlay2 = tmp_path / "conflict.yaml"
    overlay2.write_text(
        "upstream:\n"
        "  api_base: https://up.example/v1\n"
        "  api_key_env: KEY_ENV\n"
        "  models: []\n"
        "master_key_env: MASTER_ENV\n"
        "litellm_settings:\n"
        "  drop_params: true\n"
        "litellm_settings_addendum:\n"
        "  drop_params: false\n"
    )
    r = subprocess.run(
        [sys.executable, str(RENDER), "--overlay", str(overlay2), "--template", str(template)],
        capture_output=True, text=True,
    )
    assert r.returncode == 1, "addendum conflict must fail closed"


def test_parse_credentials_valid_and_rejecting(tmp_path):
    creds = _write_creds(tmp_path, extra='purpose = "test"\n')
    r = subprocess.run([sys.executable, str(PARSE), str(creds)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "LITELLM_API_KEY\top://testvault/testitem/credential\t1\top"
    # rejection: missing required flag
    bad = tmp_path / "bad.toml"
    bad.write_text('[[credential]]\nenv_var = "X"\nop_ref = "op://a/b/c"\n')
    r = subprocess.run([sys.executable, str(PARSE), str(bad)], capture_output=True, text=True)
    assert r.returncode == 1, "missing explicit required flag must fail closed"
    # rejection: disallowed key
    bad2 = tmp_path / "bad2.toml"
    bad2.write_text('[[credential]]\nenv_var = "X"\nop_ref = "op://a/b/c"\nrequired = true\nweird_key = 1\n')
    r = subprocess.run([sys.executable, str(PARSE), str(bad2)], capture_output=True, text=True)
    assert r.returncode == 1, "disallowed keys must be rejected"