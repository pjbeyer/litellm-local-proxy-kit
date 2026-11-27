"""Dispatch owner maintenance targets to the installed policy's existing tools."""
from __future__ import annotations

import http.client
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys

# Keep this CLI outside the controller's closed core source inventory.
# Reuse the private policy reader without importing from the caller's cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent / "core"))
from restart_control.engine import Fault, _private_path, _read_json, canonical_digest, strict_json


_BINDING_SCOPE = "on-disk controller binding only"


def binding_result(result):
    match = result == "match"
    print(f"{_BINDING_SCOPE}: {result}", file=sys.stdout if match else sys.stderr)
    return 0 if match else 78


def valid_install_receipt(receipt, inventory):
    def digest(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

    return (set(receipt) == {"version", "policy_digest", "sources"}
            and type(receipt["version"]) is int and receipt["version"] == 1
            and digest(receipt["policy_digest"])
            and isinstance(receipt["sources"], dict) and set(receipt["sources"]) == set(inventory)
            and all(isinstance(fact, dict) and set(fact) == {"sha256", "mode"}
                    and digest(fact["sha256"]) and type(fact["mode"]) is int
                    and 0 <= fact["mode"] <= 0o7777 for fact in receipt["sources"].values()))


def check_binding(path):
    """Compare only the receipt and disk facts; never confer live authority."""
    if os.environ.get("REQUEST_ID") or os.environ.get("ASSET_DIGEST"):
        return binding_result("input error")
    # runtime_identity uses stdlib tomllib on 3.11+; do not require an
    # extra package or change the installed controller's source inventory.
    if sys.version_info < (3, 11):
        return binding_result("input error (requires Python 3.11 or newer)")
    try:
        from restart_control import runtime_identity

        inventory = runtime_identity.CONTROLLER_INPUTS
        file_fact = runtime_identity.file_fact

        policy = _read_json(path)
        if not isinstance(policy, dict):
            return binding_result("input error")
        kit = policy_path(policy, "kit_root").resolve(strict=True)
        bundle = policy_path(policy, "controller_root")
        policy_digest = canonical_digest(policy)
        receipt_path = bundle / "install.json"
        if not _private_path(receipt_path, False) or receipt_path.lstat().st_nlink != 1:
            return binding_result("input error")
        with receipt_path.open("rb") as stream:
            data = stream.read(65537)
    except (OSError, ValueError, Fault, ImportError, RuntimeError):
        return binding_result("input error")

    try:
        receipt = strict_json(data) if len(data) <= 65536 else None
    except Fault:
        return binding_result("invalid receipt")
    try:
        # Use only our trusted helper. Its registry is authoritative for this
        # comparison only if the policy-bound kit has identical helper bytes.
        helper = Path(runtime_identity.__file__).with_suffix(".py")
        bound_helper = kit / "core/restart_control/runtime_identity.py"
        if any(parent.is_symlink() for parent in (bound_helper.parent, kit / "core")):
            return binding_result("input error")
        if file_fact(bound_helper)["sha256"] != file_fact(helper)["sha256"]:
            return binding_result("input error (different binding helper; update the maintenance worktree)")
    except (OSError, ValueError):
        return binding_result("input error")

    if not isinstance(receipt, dict) or not valid_install_receipt(receipt, inventory):
        return binding_result("invalid receipt")

    mismatch = policy_digest != receipt["policy_digest"]
    try:
        for name in inventory:
            recorded = receipt["sources"][name]
            for source in (kit / name, bundle / name.removeprefix("core/")):
                if source.parent.is_symlink() or not stat.S_ISDIR(source.parent.lstat().st_mode):
                    return binding_result("input error")
                if file_fact(source) != recorded:
                    mismatch = True
    except (OSError, ValueError):
        return binding_result("input error")
    return binding_result("mismatch" if mismatch else "match")


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
    if target == "check-binding":
        return check_binding(path)
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
