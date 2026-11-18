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
sys.dont_write_bytecode = True
from restart_control.runtime_identity import native_receipt_fact, read_pin, file_fact, PROXY_INPUTS

REPO = Path(__file__).resolve().parent.parent
MANAGED = (*PROXY_INPUTS, '*.plist')


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_file(checks: list[dict], kind: str, target: Path, expected_bytes: bytes | None, label: str) -> None:
    if target.is_symlink() or not target.is_file():
        checks.append({"kind": kind, "target": label, "status": "divergent", "detail": "missing"})
        return
    try:
        file_fact(target)  # managed inputs must be regular single-link files
    except (OSError, ValueError):
        checks.append({"kind":kind,"target":label,"status":"divergent","detail":"invalid managed file type"})
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

    # Strict metadata pairing is non-exemptible, including explicit absence.
    states = []
    if (config_dir/'restart_control').exists() or (config_dir/'restart_control').is_symlink():
        checks.append({'kind':'helper-layout','target':'restart_control',
                       'status':'divergent','detail':'unexpected installed nested helper layout'})
    for pin in (deploy_dir/'pin.lock', config_dir/'pin.lock'):
        try:
            states.append(native_receipt_fact(pin))
        except Exception:
            checks.append({'kind':'native-receipt','target':str(pin.parent/'native-build.json'),
                           'status':'divergent','detail':'invalid pin/receipt pairing'})
    if len(states) == 2:
        hashes = [fact['sha256'] if fact is not None else None for fact in states]
        checks.append({'kind':'native-receipt','target':'native-build.json',
                       'status':'conformant' if hashes[0] == hashes[1] else 'divergent',
                       'detail':None if hashes[0] == hashes[1] else 'receipt bytes or presence differ'})

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
        if name not in ('pin.lock', 'runtime_identity.py') and any(label.endswith(e) or e in label for e in exemptions):
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
            src = _core_file(REPO, name)
            expected = src.read_bytes() if src.exists() else None
        check_file(checks, "rendered-vs-live", config_dir / name, expected, label)

    # 2. core-hash across repos (script files only; plists/manifest/render are per-deployment)
    if args.other_root:
        other = args.other_root.resolve()
        for name in MANAGED:
            if "*" in name or name in ("config.rendered.yaml", "credentials.toml", "pin.lock"):
                continue
            mine = _core_file(REPO, name)
            theirs = _core_file(other, name)
            if not theirs.exists():
                checks.append({"kind": "core-hash", "target": name, "status": "divergent", "detail": "missing in other repo"})
                continue
            if sha256(mine) != sha256(theirs):
                checks.append({"kind": "core-hash", "target": name, "status": "divergent", "detail": f"{sha256(mine)[:12]} vs {sha256(theirs)[:12]}"})
            else:
                checks.append({"kind": "core-hash", "target": name, "status": "conformant"})
        # 3. pin comparison
        mine_pin = deploy_dir / "pin.lock"
        theirs_pin = other / "deployments" / _other_deploy_name(other) / "pin.lock"
        native_receipt_fact(theirs_pin)
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


def _core_file(root, name):
    return root/'core'/('restart_control/runtime_identity.py' if name == 'runtime_identity.py' else name)


def _pin_identity(pin_path):
    pin = read_pin(pin_path)
    return {'base_version':pin['base_version'], 'patch_tip':pin['patch_tip'],
            'native_artifact':pin.get('native_artifact')}


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("internal error: drift inputs invalid", file=sys.stderr)
        sys.exit(2)
