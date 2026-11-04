#!/usr/bin/env python3
"""Template the launchd plists for a deployment (spec 015, Contract 4).

Modes: --render (write substituted plists to a target dir), --check (fail if
target plists differ from freshly rendered ones). Never loads/unloads launchd
jobs — the operator runs launchctl commands (EDR Mac; hardened-endpoint posture).
"""
from __future__ import annotations

import argparse
import os
import plistlib
import sys
from pathlib import Path
from urllib.parse import urlsplit


def runtime_environment_from_overlay(path: Path | None) -> dict[str, str]:
    """Only non-secret runtime settings may be materialized in a plist.

    Credential-bearing database URLs belong in credentials.toml (env_var =
    DATABASE_URL), never in this allowlisted, persisted environment block.
    """
    if path is None:
        return {}
    import yaml

    overlay = yaml.safe_load(path.read_text())
    env = overlay.get("runtime_environment", {})
    allowed = {
        "DATABASE_URL",
        "LITELLM_MCP_STDIO_EXTRA_COMMANDS",
        "LITELLM_ENABLE_MCP_STDIO",
        # Bounded, non-secret availability control for MCP discovery. This is
        # deliberately allowlisted instead of admitting arbitrary LITELLM_*;
        # persisted launchd settings must not become a credential/config bypass.
        "LITELLM_MCP_TOOL_LISTING_TIMEOUT",
        # Non-secret TypeSafe-compatible endpoint for Phase-3 Jev routing.
        # The API key stays in credentials.toml; this value is deliberately
        # separate so no credential can be persisted in a LaunchAgent plist.
        "TYPESAFE_API_BASE",
    }
    if not isinstance(env, dict) or set(env) - allowed:
        raise ValueError("runtime_environment contains unsupported keys")
    if any(not isinstance(v, str) or not v or "\x00" in v for v in env.values()):
        raise ValueError("runtime_environment values must be nonempty strings")
    if "DATABASE_URL" in env:
        url = urlsplit(env["DATABASE_URL"])
        if (url.scheme not in {"postgres", "postgresql"} or url.password is not None
                or url.query or url.fragment or not url.hostname or not url.path.strip("/")):
            raise ValueError("DATABASE_URL must be a password-free PostgreSQL URI without query/fragment")
    if "TYPESAFE_API_BASE" in env:
        url = urlsplit(env["TYPESAFE_API_BASE"])
        if (url.scheme != "https" or url.username is not None or url.password is not None
                or url.query or url.fragment or not url.hostname):
            raise ValueError("TYPESAFE_API_BASE must be an https URL without userinfo/query/fragment")
    stdio_enabled = env.get("LITELLM_ENABLE_MCP_STDIO")
    if stdio_enabled is not None and stdio_enabled not in {"true", "false"}:
        raise ValueError("stdio enablement must be literal true or false")
    listing_timeout = env.get("LITELLM_MCP_TOOL_LISTING_TIMEOUT")
    if listing_timeout is not None:
        try:
            timeout_value = float(listing_timeout)
        except ValueError as exc:
            raise ValueError("MCP tool-listing timeout must be a number") from exc
        if not 1.0 <= timeout_value <= 30.0:
            raise ValueError("MCP tool-listing timeout must be between 1 and 30 seconds")
    commands = env.get("LITELLM_MCP_STDIO_EXTRA_COMMANDS", "")
    if commands:
        import re
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", c) for c in commands.split(",")):
            raise ValueError("stdio allowlist must contain comma-separated command basenames")
    return env

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
    runtime_environment: dict[str, str] | None = None,
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
    rendered = {
        f"{label}.plist": substitute((TMPL / "proxy.plist.tmpl").read_text(), values),
        f"{label}.rotate.plist": substitute((TMPL / "proxy.rotate.plist.tmpl").read_text(), values),
    }
    if runtime_environment:
        # Serialize XML structurally; environment values must not be substituted
        # as raw XML (e.g. an ampersand in a value would corrupt the plist).
        proxy = plistlib.loads(rendered[f"{label}.plist"].encode())
        proxy["EnvironmentVariables"].update(runtime_environment)
        rendered[f"{label}.plist"] = plistlib.dumps(proxy, sort_keys=False).decode()
    return rendered


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
    ap.add_argument("--runtime-overlay", type=Path, help="overlay containing allowlisted non-secret runtime_environment")
    ap.add_argument("--home", default=None, help="HOME to bake into the plist (default: operator's)")
    ap.add_argument("--out", type=Path, help="directory to write rendered plists into")
    ap.add_argument("--check", type=Path, help="compare rendered plists against this dir instead of writing")
    args = ap.parse_args()

    try:
        runtime_environment = runtime_environment_from_overlay(args.runtime_overlay)
    except Exception as exc:
        # Do not echo input-bearing YAML/URI parser errors into logs.
        print(f"invalid runtime environment ({type(exc).__name__})", file=sys.stderr)
        return 1
    rendered = render_plists(
        args.label, args.port, args.log_dir, args.wrapper_dir, args.work_dir,
        args.deployment, args.proxy_python, args.home,
        args.op_keychain_service, args.op_keychain_account, runtime_environment,
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
