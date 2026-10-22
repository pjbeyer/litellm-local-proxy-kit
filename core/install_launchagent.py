#!/usr/bin/env python3
"""Template the launchd plists for a deployment (spec 015, Contract 4).

Modes: --render (write substituted plists to a target dir), --check (fail if
target plists differ from freshly rendered ones). Never loads/unloads launchd
jobs — the operator runs launchctl commands (EDR Mac; flex-dekil posture).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMPL = REPO / "launchd"

PLACEHOLDERS = ("{{LABEL}}", "{{PORT}}", "{{LOG_DIR}}", "{{WRAPPER_PATH}}", "{{WORK_DIR}}", "{{WRAPPER_DIR}}", "{{HOME}}", "{{DEPLOYMENT}}", "{{PROXY_PYTHON}}", "{{OP_KEYCHAIN_SERVICE}}", "{{OP_KEYCHAIN_ACCOUNT}}")


def substitute(text: str, values: dict[str, str]) -> str:
    out = text
    for key in PLACEHOLDERS:
        out = out.replace(key, values.get(key.strip("{}"), ""))
    if "{{" in out:
        leftover = [w for w in out.split("{{")[1:]]
        raise ValueError(f"unsubstituted placeholder remains: {{}}{leftover[0][:40]}")
    return out


def render_plists(
    label: str,
    port: str,
    log_dir: str,
    wrapper_dir: str,
    work_dir: str,
    deployment: str,
    proxy_python: str,
    home: str | None = None,
    op_keychain_service: str = "",
    op_keychain_account: str = "",
) -> dict[str, str]:
    values = {
        "LABEL": label,
        "PORT": port,
        "LOG_DIR": log_dir,
        "WRAPPER_PATH": f"{wrapper_dir}/run-proxy-wrapper.sh",
        "WORK_DIR": work_dir,
        "WRAPPER_DIR": wrapper_dir,
        "HOME": home or os.path.expanduser("~"),
        "DEPLOYMENT": deployment,
        "PROXY_PYTHON": proxy_python,
        "OP_KEYCHAIN_SERVICE": op_keychain_service,
        "OP_KEYCHAIN_ACCOUNT": op_keychain_account,
    }
    return {
        f"{label}.plist": substitute((TMPL / "proxy.plist.tmpl").read_text(), values),
        f"{label}.rotate.plist": substitute((TMPL / "proxy.rotate.plist.tmpl").read_text(), values),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", required=True)
    ap.add_argument("--port", required=True)
    ap.add_argument("--log-dir", required=True)
    ap.add_argument("--wrapper-dir", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--deployment", default="work")
    ap.add_argument("--proxy-python", required=True)
    ap.add_argument("--op-keychain-service", required=True)
    ap.add_argument("--op-keychain-account", required=True)
    ap.add_argument("--home", default=None, help="HOME to bake into the plist (default: operator's)")
    ap.add_argument("--out", type=Path, help="directory to write rendered plists into")
    ap.add_argument("--check", type=Path, help="compare rendered plists against this dir instead of writing")
    args = ap.parse_args()

    rendered = render_plists(
        args.label, args.port, args.log_dir, args.wrapper_dir, args.work_dir,
        args.deployment, args.proxy_python, args.home,
        args.op_keychain_service, args.op_keychain_account,
    )
    if args.check:
        bad = []
        for name, content in rendered.items():
            target = args.check / name
            if not target.exists() or target.read_text() != content:
                bad.append(name)
        if bad:
            print("STALE: plist(s) differ from template render:", ", ".join(bad), file=sys.stderr)
            return 1
        print("check OK: plists match template render")
        return 0
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for name, content in rendered.items():
            (args.out / name).write_text(content)
            print(f"wrote {args.out / name}")
        return 0
    for name, content in rendered.items():
        print(f"--- {name} ---")
        print(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
