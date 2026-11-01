#!/usr/bin/env python3
"""Drift check (spec 015, Contract 3 / FR-011).

Checks (each reported; any divergence fails the gate):
  1. rendered-vs-live: managed apply-target files byte-match the repo render
  2. core-hash: shared core files hash-equal across both deployment repos
  3. pin: pin.lock versions equal across deployments
Exemptions come from exemptions.yaml (enumerated, justified, in-repo) —
adding one is a code change, never a runtime dodge (anti-ratchet).

Usage: drift_check.py --deployment-root <repo-with-deploy.conf.sh> [--other-root <repo2>]
Exit: 0 conformant, 1 divergence, 2 internal error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANAGED = ("config.rendered.yaml", "run-proxy-wrapper.sh", "check_patch_sentinel.py",
           "check_pin.py", "parse_credentials.py", "rotate-logs.sh",
           "aws-sso-cred-shim.sh", "harvest-filter-metrics.sh", "credentials.toml", "pin.lock", "check_credential_coverage.py",
           "*.plist")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_file(checks: list[dict], kind: str, target: Path, expected_bytes: bytes | None, label: str) -> None:
    if not target.exists():
        checks.append({"kind": kind, "target": label, "status": "divergent", "detail": "missing"})
        return
    actual = target.read_bytes()
    if expected_bytes is not None and actual != expected_bytes:
        checks.append({"kind": kind, "target": label, "status": "divergent", "detail": "bytes differ"})
    else:
        checks.append({"kind": kind, "target": label, "status": "conformant"})


def load_exemptions(root: Path) -> list[str]:
    p = root / "drift-exemptions.yaml"
    if not p.exists():
        return []
    import yaml
    data = yaml.safe_load(p.read_text()) or {}
    return list(data.get("exempt", []))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--deployment-root", required=True, type=Path)
    ap.add_argument("--other-root", type=Path, help="second deployment repo for core-hash/pin comparison")
    args = ap.parse_args()

    checks: list[dict] = []
    root = args.deployment_root.resolve()

    # deploy.conf.sh supplies apply-target paths
    env: dict[str, str] = {}
    for line in (root / "deploy.conf.sh").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    config_dir = Path(env["LITELLM_CONFIG_DIR"]).expanduser()
    deploy_name = env["LITELLM_DEPLOYMENT"]
    deploy_dir = root / "deployments" / deploy_name

    # 1. rendered-vs-live for each managed file (glob patterns expand)
    exemptions = load_exemptions(root)
    managed_names: list[str] = []
    for name in MANAGED:
        if "*" in name:
            managed_names.extend(sorted(p.name for p in config_dir.glob(name)))
        else:
            managed_names.append(name)
    for name in managed_names:
        label = str(config_dir / name)
        if any(label.endswith(e) or e in label for e in exemptions):
            checks.append({"kind": "exempt", "target": label, "status": "exempt", "reason": "enumerated in drift-exemptions.yaml"})
            continue
        expected = None
        if name == "config.rendered.yaml":
            r = subprocess.run(
                [sys.executable, str(REPO / "core" / "render_config.py"),
                 "--overlay", str(deploy_dir / "overlay.yaml"),
                 "--template", str(REPO / "core" / "config.template.header.txt")],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                checks.append({"kind": "rendered-vs-live", "target": label, "status": "divergent", "detail": f"render failed: {r.stderr[:200]}"})
                continue
            expected = r.stdout.encode()
        elif name.endswith(".plist"):
            # Plists are rendered by install_launchagent.py, not copied from core/.
            expected = None  # byte-render comparison handled by installer --verify
            r = subprocess.run(
                [sys.executable, str(REPO / "core" / "install_launchagent.py"),
                 "--label", env.get("LITELLM_PROXY_LABEL", ""),
                 "--port", env.get("LITELLM_LOCAL_PORT", ""),
                 "--log-dir", env.get("LITELLM_LOG_DIR", str(config_dir)),
                 "--wrapper-dir", str(config_dir),
                 "--work-dir", str(config_dir),
                 "--deployment", deploy_name,
                 "--proxy-python", env.get("LITELLM_PROXY_PYTHON", sys.executable),
                 "--runtime-overlay", str(deploy_dir / "overlay.yaml"),
                 "--op-keychain-service", env.get("OP_KEYCHAIN_SERVICE", ""),
                 "--op-keychain-account", env.get("OP_KEYCHAIN_ACCOUNT", ""),
                 "--check", str(config_dir)],
                capture_output=True, text=True,
            )
            status = "conformant" if r.returncode == 0 else "divergent"
            checks.append({"kind": "rendered-vs-live", "target": label, "status": status,
                           "detail": None if r.returncode == 0 else r.stderr.strip()[:200]})
            continue
        elif name in ("credentials.toml", "pin.lock"):
            expected = (deploy_dir / name).read_bytes()
        else:
            src = REPO / "core" / name
            expected = src.read_bytes() if src.exists() else None
        check_file(checks, "rendered-vs-live", config_dir / name, expected, label)

    # 2. core-hash across repos (script files only; plists/manifest/render are per-deployment)
    if args.other_root:
        other = args.other_root.resolve()
        for name in MANAGED:
            if "*" in name or name in ("config.rendered.yaml", "credentials.toml", "pin.lock"):
                continue
            mine = REPO / "core" / name
            theirs = other / "core" / name
            if not theirs.exists():
                checks.append({"kind": "core-hash", "target": name, "status": "divergent", "detail": "missing in other repo"})
                continue
            if sha256(mine) != sha256(theirs):
                checks.append({"kind": "core-hash", "target": name, "status": "divergent", "detail": f"{sha256(mine)[:12]} vs {sha256(theirs)[:12]}"})
            else:
                checks.append({"kind": "core-hash", "target": name, "status": "conformant"})
        # 3. pin comparison
        mine_pin = (deploy_dir / "pin.lock").read_text()
        theirs_pin = (other / "deployments" / _other_deploy_name(other) / "pin.lock").read_text()
        if _pin_identity(mine_pin) == _pin_identity(theirs_pin):
            checks.append({"kind": "pin", "target": "pin.lock", "status": "conformant"})
        else:
            checks.append({"kind": "pin", "target": "pin.lock", "status": "divergent",
                           "detail": f"{_pin_identity(mine_pin)} vs {_pin_identity(theirs_pin)}"})

    verdict = "PASS" if all(c["status"] in ("conformant", "exempt") for c in checks) else "FAIL"
    print(json.dumps({"checks": checks, "verdict": verdict}, indent=1))
    return 0 if verdict == "PASS" else 1


def _other_deploy_name(other: Path) -> str:
    for d in sorted((other / "deployments").iterdir()):
        if d.is_dir():
            return d.name
    raise ValueError("no deployment dir in other repo")


def _pin_identity(pin_text: str) -> str:
    import re
    base = re.search(r'base_version\s*=\s*"([^"]+)"', pin_text)
    tip = re.search(r'patch_tip\s*=\s*"([^"]+)"', pin_text)
    return f"{base.group(1) if base else '?'}+{tip.group(1) if tip else '?'}"


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"internal error: {exc}", file=sys.stderr)
        sys.exit(2)
