"""Exercise the owner targets with disposable policy, clients and HTTP fixtures."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def maintenance(tmp_path):
    base = tmp_path.resolve()
    core = base / "bound kit" / "core"
    core.mkdir(parents=True)
    deployment = base / "deployment repo"
    deployment.mkdir()
    (deployment / "deploy.conf.sh").touch()
    controller = base / "installed controller"
    controller.mkdir()
    plist = base / "primary proxy.plist"
    policy = base / "owner policy.json"
    policy.write_text(json.dumps({"kit_root": str(core.parent), "deployment_root": str(deployment),
                                  "controller_root": str(controller), "plist": str(plist),
                                  "candidate_port": 4101}))
    policy.chmod(0o600)
    (core / "installer.sh").write_text('printf "installer <%s> <%s>\\n" "$1" "$2"\n'
                                        'if [ "$2" = apply ]; then exit "${FIXTURE_APPLY_RC:-0}"; fi\n')
    (core / "drift_check.py").write_text('import sys\nprint("drift", sys.argv[1], sys.argv[2])\n')
    (controller / "restart_controller.py").write_text('import json, sys\nprint(json.dumps(sys.argv[1:]))\n')
    plist.write_bytes(plistlib.dumps({"EnvironmentVariables": {"LITELLM_PROXY_PYTHON": sys.executable}}))

    def run(*args, **env):
        return subprocess.run(["make", *args], cwd=ROOT, capture_output=True, text=True, timeout=30,
                              env={**os.environ, "POLICY": str(policy), "REQUEST_ID": "",
                                   "ASSET_DIGEST": "", **env})

    return run, policy, deployment, plist


@pytest.fixture
def binding(maintenance, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    import owner_maintenance
    from restart_control.engine import canonical_digest
    from restart_control.runtime_identity import CONTROLLER_INPUTS, file_fact

    run, policy, _, _ = maintenance
    values = json.loads(policy.read_text())
    kit = Path(values["kit_root"])
    installed = Path(values["controller_root"])
    for name in CONTROLLER_INPUTS:
        source = kit / name
        source.parent.mkdir(parents=True, exist_ok=True)
        if name == "core/restart_control/runtime_identity.py":
            shutil.copy2(ROOT / name, source)
        else:
            source.write_text("fixture controller source, not executable Python\n")
        copy = installed / name.removeprefix("core/")
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, copy)
    receipt = installed / "install.json"
    receipt.write_text(json.dumps({"version": 1, "policy_digest": canonical_digest(values),
                                   "sources": {name: file_fact(kit / name)
                                               for name in CONTROLLER_INPUTS}}))
    receipt.chmod(0o600)
    monkeypatch.setenv("POLICY", str(policy))
    monkeypatch.delenv("REQUEST_ID", raising=False)
    monkeypatch.delenv("ASSET_DIGEST", raising=False)
    return owner_maintenance, run, policy, receipt, kit, installed


@pytest.mark.parametrize(("change", "expected"), [
    ("none", "match"), ("policy", "mismatch"), ("kit bytes", "mismatch"),
    ("installed bytes", "mismatch"), ("kit mode", "mismatch"),
    ("installed mode", "mismatch"), ("missing receipt", "input error"),
    ("bad json", "invalid receipt"), ("incomplete coverage", "invalid receipt"),
    ("boolean version", "invalid receipt"), ("boolean mode", "invalid receipt"),
    ("nonregular", "input error"), ("unreadable", "input error"),
    ("missing source", "input error"), ("receipt link", "input error"),
    ("invalid policy root", "input error"), ("approval arguments", "input error"),
])
def test_binding_is_read_only_and_sanitized(binding, monkeypatch, capsys, change, expected):
    module, _, policy, receipt, kit, installed = binding
    source = kit / "core/restart_controller.py"
    copy = installed / "restart_controller.py"
    if change in ("policy", "invalid policy root"):
        values = json.loads(policy.read_text())
        if change == "policy":
            values["candidate_port"] += 1
        else:
            del values["kit_root"]
        policy.write_text(json.dumps(values))
    elif change in ("kit bytes", "installed bytes"):
        (source if change == "kit bytes" else copy).write_text("different bytes")
    elif change in ("kit mode", "installed mode"):
        (source if change == "kit mode" else copy).chmod(0o700)
    elif change == "missing receipt":
        receipt.unlink()
    elif change == "bad json":
        receipt.write_text("invalid fixture contents")
    elif change in ("incomplete coverage", "boolean version", "boolean mode"):
        values = json.loads(receipt.read_text())
        if change == "incomplete coverage":
            values["sources"].popitem()
        elif change == "boolean version":
            values["version"] = True
        else:
            values["sources"]["core/restart_controller.py"]["mode"] = True
        receipt.write_text(json.dumps(values))
    elif change == "nonregular":
        copy.unlink()
        copy.mkdir()
    elif change == "missing source":
        source.unlink()
    elif change == "receipt link":
        renamed = receipt.rename(installed / "original.json")
        receipt.symlink_to(renamed)
    elif change == "approval arguments":
        monkeypatch.setenv("REQUEST_ID", "fixture-approval-argument")
        monkeypatch.setenv("ASSET_DIGEST", "fixture-approval-digest")

    import builtins
    import io

    def reject(*args, **kwargs):
        pytest.fail("disk diagnostic tried to mutate, contact a service, or spawn a process")

    def observe_open(opener):
        def read_only(file, mode="r", *args, **kwargs):
            assert mode in ("r", "rb"), "disk diagnostic tried to write"
            if change == "unreadable" and file == source:
                raise PermissionError("private path must not reach output")
            return opener(file, mode, *args, **kwargs)
        return read_only

    original_open = os.open

    def observe_os_open(path, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return original_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "open", observe_open(builtins.open))
        scoped.setattr(io, "open", observe_open(io.open))
        scoped.setattr(os, "open", observe_os_open)
        for name in ("mkdir", "unlink", "rename", "replace", "chmod", "system"):
            scoped.setattr(os, name, reject)
        scoped.setattr(subprocess, "Popen", reject)
        scoped.setattr(module.http.client, "HTTPConnection", reject)
        result = module.main("check-binding")
    out, err = capsys.readouterr()
    assert (result == 0) == (expected == "match")
    assert (out + err).strip() == f"on-disk controller binding only: {expected}"


def test_binding_refuses_a_different_registry_even_if_the_receipt_matches(binding):
    from restart_control.runtime_identity import file_fact

    _, run, _, receipt, kit, installed = binding
    name = "core/restart_control/runtime_identity.py"
    source = kit / name
    source.write_text(source.read_text() + "\n# Different helper revision.\n")
    shutil.copy2(source, installed / name.removeprefix("core/"))
    values = json.loads(receipt.read_text())
    values["sources"][name] = file_fact(source)
    receipt.write_text(json.dumps(values))
    result = run("check-binding")
    assert result.returncode != 0
    assert "on-disk controller binding only: input error (different binding helper;" in result.stderr


def test_binding_reports_unsupported_python_before_reading_policy(binding, monkeypatch, capsys):
    module, _, _, _, _, _ = binding
    with monkeypatch.context() as scoped:
        scoped.setattr(module.sys, "version_info", (3, 10))
        result = module.main("check-binding")
    assert result == 78
    assert capsys.readouterr().err.strip() == (
        "on-disk controller binding only: input error (requires Python 3.11 or newer)"
    )


def test_make_check_binding_dispatches_without_policy_bound_code(binding):
    _, run, _, _, _, _ = binding
    assert "make check-binding" in run("help").stdout
    result = run("check-binding")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "on-disk controller binding only: match"
    refused = run("check-binding", POLICY="/missing-fixture-policy")
    assert refused.returncode != 0
    assert "on-disk controller binding only: input error" in refused.stderr


def test_owner_helper_does_not_change_controller_source_inventory(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "core"))
    from restart_control.runtime_identity import source_facts

    assert source_facts(ROOT)


def test_help_and_separate_invocations(maintenance):
    run, _, _, _ = maintenance
    help_result = run(POLICY="/missing-fixture-policy")
    assert help_result.returncode == 0, help_result.stderr
    assert "make preview   Preview installer changes" in help_result.stdout
    combined = run("-j2", "preview", "apply")
    assert combined.returncode != 0
    assert "Run one maintenance target at a time" in combined.stderr
    assert not combined.stdout


def test_preview_and_apply_delegate_separately(maintenance):
    run, _, deployment, _ = maintenance
    preview = run("preview")
    assert preview.returncode == 0, preview.stderr
    assert preview.stdout.splitlines() == [f"installer <{deployment}> <--dry-run>"]
    applied = run("apply")
    assert applied.returncode == 0, applied.stderr
    assert applied.stdout.splitlines() == [f"installer <{deployment}> <apply>",
                                           f"installer <{deployment}> <--verify>"]
    refused = run("apply", FIXTURE_APPLY_RC="78")
    assert refused.returncode != 0
    assert refused.stdout == f"installer <{deployment}> <apply>\n"


def test_verify_uses_pinned_python(maintenance):
    run, _, deployment, plist = maintenance
    # Run Make's Python but put a different interpreter in the plist. A broken
    # pinned path must fail rather than fall back to the ambient python3.
    result = run("verify")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"installer <{deployment}> <--verify>",
                                          f"drift --deployment-root {deployment}"]
    unavailable = str(plist.parent / "unavailable pinned python")
    plist.write_bytes(plistlib.dumps({"EnvironmentVariables": {"LITELLM_PROXY_PYTHON": unavailable}}))
    refused = run("verify")
    assert refused.returncode != 0
    assert "unavailable pinned python" in refused.stderr
    assert "drift --deployment-root" not in refused.stdout


def test_stale_deployment_refuses_without_installing(maintenance):
    run, _, deployment, _ = maintenance
    shutil.rmtree(deployment)
    refused = run("apply")
    assert refused.returncode != 0
    assert "deployment_root has no deploy.conf.sh" in refused.stderr
    assert not refused.stdout


def test_controller_arguments_and_missing_approval_plan_inputs(maintenance):
    run, policy, _, _ = maintenance
    for target in ("status", "prepare"):
        result = run(target)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == ["--policy", str(policy), target]
    refused = run("restart", "REQUEST_ID=", "ASSET_DIGEST=")
    assert refused.returncode != 0
    assert "usage: make restart" in refused.stderr
    assert not refused.stdout
    request_id, digest = "a" * 32, "b" * 64
    submitted = run("restart", f"REQUEST_ID={request_id}", f"ASSET_DIGEST={digest}")
    assert submitted.returncode == 0, submitted.stderr
    assert json.loads(submitted.stdout) == ["--policy", str(policy), "restart", request_id, digest]


def test_health_uses_policy_primary_port_and_exact_statuses(maintenance, monkeypatch):
    # Load the same dispatcher as Make but stub the HTTP transport: CI need not
    # bind a socket, and an incorrect destination cannot reach a live service.
    monkeypatch.syspath_prepend(str(ROOT))
    import owner_maintenance

    _, policy, _, plist = maintenance
    monkeypatch.setenv("POLICY", str(policy))
    plist.write_bytes(plistlib.dumps({"ProgramArguments": ["/bin/bash", "wrapper", "4187"]}))
    seen = []
    statuses = {"/health/liveliness": 200, "/health/readiness": 200, "/v1/models": 401}

    class Connection:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("127.0.0.1", 4187, 5)

        def request(self, method, path):
            assert method == "GET"
            self.status = statuses[path]
            seen.append(path)

        def getresponse(self):
            return self

        def close(self):
            pass

    monkeypatch.setattr(owner_maintenance.http.client, "HTTPConnection", Connection)
    assert owner_maintenance.main("health") == 0
    assert seen == list(statuses)
    statuses["/v1/models"] = 200
    with pytest.raises(ValueError, match=r"/v1/models: HTTP 200 \(expected 401\)"):
        owner_maintenance.main("health")
