#!/usr/bin/env python3
"""Render a LiteLLM proxy config from a deployment overlay (spec 015, Contract 2).

Byte-deterministic: same overlay + same template -> identical bytes (FR-003).
Reference-only: secrets appear solely as os.environ/... (FR-004).
Reads NOTHING from the live deployment (FR-002 / R-FP-3): input is the overlay
file and this repo's template only.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def _render_yaml(obj: object) -> str:
    """Deterministic YAML dump: sorted keys, no anchors, trailing newline."""
    return yaml.safe_dump(
        obj, default_flow_style=False, sort_keys=False, width=120, allow_unicode=True
    )


def render(overlay: dict, template: str, *, home: str | None = None) -> str:  # noqa: C901
    """Produce the final config bytes: template with overlay-driven blocks.

    `${HOME}` in overlay values is substituted with the operator's home at
    RENDER time (never tracked, FR-004): MCP stdio commands receive literal
    paths — litellm does not expand `~`/`$HOME` in stdio `command` (verified
    against the pinned build's StdioServerParameters path).
    """
    if home is None:
        home = os.environ.get("HOME", "")
    if not home:
        raise ValueError(
            "HOME undefined in the render environment — ${HOME} placeholders "
            "would go unresolved (fail-closed, FR-004)"
        )
    else:

        def _subst(obj: object) -> object:
            if isinstance(obj, dict):
                return {k: _subst(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_subst(v) for v in obj]
            if isinstance(obj, str):
                return obj.replace("${HOME}", home)
            return obj
        overlay = _subst(overlay)  # type: ignore[assignment]
    template_part = template.split("# ===BEGIN GENERATED===")[0] + "# ===BEGIN GENERATED===\n"

    # model_list: explicit entries + optional wildcard
    models: list[dict] = []
    base = {
        "api_base": overlay["upstream"]["api_base"],
        "api_key": f"os.environ/{overlay['upstream']['api_key_env']}",
        "timeout": 600,
        "stream_timeout": 240,
    }
    for m in overlay["upstream"].get("models", []):
        params: dict = dict(base)
        params.update({k: v for k, v in m.items() if k != "name"})
        params["model"] = f"openai/{m['name']}"
        models.append({"model_name": m["name"], "litellm_params": params})
    if overlay["upstream"].get("wildcard"):
        wildcard_params: dict = {"model": "openai/*", **base}
        # The live wildcard entry carries NO timeouts (deliberate: unlisted
        # models use litellm defaults). Preserve that shape exactly.
        if not overlay["upstream"].get("wildcard_timeouts", True):
            for k in ("timeout", "stream_timeout"):
                wildcard_params.pop(k, None)
        models.append(
            {
                "model_name": "*",
                "litellm_params": wildcard_params,
            }
        )
    # Extra model_list entries pass through verbatim (e.g. non-openai providers
    # such as bedrock embeddings). Overlay-only; no live-state reads (FR-002).
    for extra in overlay.get("extra_model_list") or []:
        if not isinstance(extra, dict) or "model_name" not in extra or "litellm_params" not in extra:
            raise ValueError("extra_model_list entries need model_name + litellm_params")
        models.append(extra)
    doc: dict = {"model_list": models}

    ls = overlay.get("litellm_settings") or {}
    if ls:
        doc["litellm_settings"] = ls
    add = overlay.get("litellm_settings_addendum") or {}
    if add:
        if "litellm_settings" not in doc:
            doc["litellm_settings"] = {}
        for k, v in add.items():
            if k in doc["litellm_settings"] and doc["litellm_settings"][k] != v:
                raise ValueError(f"litellm_settings_addendum conflicts with litellm_settings: {k}")
            doc["litellm_settings"][k] = v
    gs = dict(overlay.get("general_settings") or {})
    gs["master_key"] = f"os.environ/{overlay['master_key_env']}"
    doc["general_settings"] = gs

    rs = overlay.get("router_fallbacks") or []
    if rs:
        doc["router_settings"] = {
            "fallbacks": [{r["from"]: r["to"]} for r in rs]
        }
    mcp = overlay.get("mcp_servers") or {}
    if mcp:
        doc["mcp_servers"] = mcp

    return template_part + _render_yaml(doc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--overlay", required=True, type=Path)
    ap.add_argument("--template", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--check", type=Path, help="fail if rendered != this file (staleness gate)")
    args = ap.parse_args()

    overlay = yaml.safe_load(args.overlay.read_text())
    template = args.template.read_text()
    result = render(overlay, template)

    if args.out:
        args.out.write_text(result)
        print(f"wrote {args.out} ({len(result)} bytes, sha256 {hashlib.sha256(result.encode()).hexdigest()[:12]})")
    if args.check:
        target = args.check.read_text()
        if result != target:
            print("STALE: rendered output differs from checked file", file=sys.stderr)
            return 1
        print("check OK: rendered matches")
    if not args.out and not args.check:
        sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
