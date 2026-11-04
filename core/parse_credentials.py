#!/usr/bin/env python3
"""Parse a credentials.toml manifest (Contract 1) into TSV rows for the wrapper.

Output: env_var<TAB>op_ref<TAB>required(0|1) per credential. No values are
read or printed — references only.

Minimal TOML subset parser (stdlib-only, no tomllib dependency): the manifest
grammar this repo accepts is deliberately constrained to
`[[credential]]` tables with `env_var` / `op_ref` / `required` scalar keys,
plus an optional top-level `identity` / `keychain_service` string pair.
Anything else is rejected — parse failure fails closed in the wrapper.
"""
from __future__ import annotations

import re
import sys

_ROW = re.compile(r"^\s*\[\[credential\]\]\s*$")
_KV = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\"[^\"]*\"|true|false)\s*(?:#.*)?$")
_ALLOWED_KEYS = {"env_var", "op_ref", "required", "purpose", "presented_by", "copies", "source"}
_SOURCES = {"op", "aws-sso-shim", "cli-token"}


def _unquote(value: str) -> str:
    return value[1:-1] if value.startswith('"') else value


def parse(text: str) -> list[dict]:
    creds: list[dict] = []
    current: dict | None = None
    top_keys: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0] if not raw.lstrip().startswith("#") else ""
        line = line.strip()
        if not line:
            continue
        if _ROW.match(line):
            current = {}
            creds.append(current)
            continue
        m = _KV.match(line)
        if not m:
            raise ValueError(f"line {lineno}: unsupported manifest syntax: {line!r}")
        key, value = m.group(1), m.group(2)
        if current is None:
            # top-level strings (identity/keychain_service) are ignored here
            if key not in ("identity", "keychain_service"):
                raise ValueError(f"line {lineno}: key outside [[credential]] table: {key}")
            if key in top_keys:
                raise ValueError(f"line {lineno}: duplicate top-level manifest key")
            top_keys.add(key)
            continue
        if key in current:
            raise ValueError(f"line {lineno}: duplicate credential key")
        if key not in _ALLOWED_KEYS:
            raise ValueError(f"line {lineno}: disallowed manifest key: {key}")
        if key in ("env_var", "op_ref", "purpose", "source"):
            current[key] = _unquote(value)
        elif key == "required":
            if value not in ("true", "false"):
                raise ValueError(f"line {lineno}: required must be true/false")
            current[key] = value == "true"
        else:
            raise ValueError(f"line {lineno}: list-valued keys are not accepted by this parser: {key}")
    names: set[str] = set()
    for credential in creds:
        name = credential.get('env_var')
        if name and name in names:
            raise ValueError('duplicate credential env_var')
        if name:
            names.add(name)
    return creds


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: parse_credentials.py <credentials.toml>", file=sys.stderr)
        return 1
    try:
        creds = parse(open(sys.argv[1], encoding="utf-8").read())
    except Exception as exc:
        print(f"FAIL: unreadable manifest: {exc}", file=sys.stderr)
        return 1
    if not creds:
        print("FAIL: manifest declares no credentials", file=sys.stderr)
        return 1
    for c in creds:
        if not c.get("env_var"):
            print(f"FAIL: credential entry missing env_var: {c}", file=sys.stderr)
            return 1
        source = c.get("source", "op")
        if source not in _SOURCES:
            print(f"FAIL: credential {c['env_var']}: unknown source {source!r}", file=sys.stderr)
            return 1
        if source == "op" and not c.get("op_ref"):
            print(f"FAIL: credential {c['env_var']}: source=op requires op_ref", file=sys.stderr)
            return 1
        if source != "op" and c.get("op_ref"):
            print(f"FAIL: credential {c['env_var']}: op_ref is invalid for source={source}", file=sys.stderr)
            return 1
        if "required" not in c:
            print(f"FAIL: credential entry missing explicit required flag: {c.get('env_var')}", file=sys.stderr)
            return 1
        # Use '-' for absent op_ref: bash `read` collapses adjacent tab IFS
        # delimiters, so an empty second field would shift source into `ref`.
        print(f"{c['env_var']}\t{c.get('op_ref') or '-'}\t{int(bool(c['required']))}\t{source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
