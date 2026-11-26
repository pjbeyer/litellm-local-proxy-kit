"""Dispatch owner maintenance targets to the installed policy's existing tools."""
from __future__ import annotations

import http.client
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

# Keep this CLI outside the controller's closed core source inventory.
# Reuse the private policy reader without importing from the caller's cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent / "core"))
from restart_control.engine import Fault, _read_json


def policy_path(policy, name):
    value = policy.get(name)
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(f"controller policy needs an absolute {name}")
    return Path(value)


def proxy_job(policy):
    with policy_path(policy, "plist").open("rb") as stream:
        return plistlib.load(stream)


def health(policy):
    # Same primary port source as the controller; never use the candidate port.
    port = int(proxy_job(policy)["ProgramArguments"][2])
    if not 1 <= port <= 65535 or port == policy["candidate_port"]:
        raise ValueError("invalid primary port in the proxy plist")
    for path, expected in (("/health/liveliness", 200),
                           ("/health/readiness", 200), ("/v1/models", 401)):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path)
            code = connection.getresponse().status
        except (OSError, http.client.HTTPException):
            raise ValueError(f"{path}: no HTTP response on loopback port {port}") from None
        finally:
            connection.close()
        if code != expected:
            raise ValueError(f"{path}: HTTP {code} (expected {expected})")
        print(f"{path}: HTTP {code}")
    return 0


def main(target):
    path = Path(os.environ.get("POLICY", "")).absolute()
    try:
        policy = _read_json(path)
    except (OSError, Fault):
        raise ValueError("cannot read the private controller policy; check POLICY") from None
    if not isinstance(policy, dict):
        raise ValueError("expected a controller policy object")

    if target in ("status", "prepare", "restart"):
        args = []
        if target == "restart":
            args = [os.environ.get("REQUEST_ID", ""), os.environ.get("ASSET_DIGEST", "")]
            if not all(re.fullmatch(pattern, value) for pattern, value in
                       zip((r"[0-9a-f]{32}", r"[0-9a-f]{64}"), args)):
                raise ValueError("usage: make restart REQUEST_ID=<request id> ASSET_DIGEST=<asset digest>")
        client = policy_path(policy, "controller_root") / "restart_controller.py"
        return subprocess.run([sys.executable, "-I", "-B", client, "--policy", path, target, *args]).returncode
    if target == "health":
        return health(policy)
    if target not in ("preview", "apply", "verify"):
        raise ValueError("unknown maintenance target; see make help")

    core = policy_path(policy, "kit_root") / "core"
    deployment = policy_path(policy, "deployment_root")
    if not (deployment / "deploy.conf.sh").is_file():
        raise ValueError("policy deployment_root has no deploy.conf.sh; use the owner install runbook to reconcile it")
    installer = ["/bin/bash", core / "installer.sh", deployment]
    if target == "preview":
        return subprocess.run([*installer, "--dry-run"]).returncode
    if target == "apply":
        rc = subprocess.run([*installer, "apply"]).returncode
        if rc:
            return rc
    rc = subprocess.run([*installer, "--verify"]).returncode
    if rc or target == "apply":
        return rc

    # Reuse the same pinned Python as the controller's drift gate (PyYAML).
    python = proxy_job(policy)["EnvironmentVariables"]["LITELLM_PROXY_PYTHON"]
    if not isinstance(python, str) or not Path(python).is_absolute():
        raise ValueError("the proxy plist needs an absolute pinned Python path")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([python, core / "drift_check.py", "--deployment-root", deployment], env=env).returncode


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except (ValueError, KeyError, IndexError, TypeError, OSError, plistlib.InvalidFileException) as error:
        print(f"owner maintenance: {error}", file=sys.stderr)
        sys.exit(78)
