#!/usr/bin/env python3
"""Positive patch-presence assertion for the pinned LiteLLM build (FR-006).

Never checks for the ABSENCE of an upstream string (F-CI-2): a rename would
defeat that. Asserts a symbol the patch INTRODUCED is present in the module
source. Exit 0 = patched; exit 1 = refuse.
"""
from __future__ import annotations

import importlib
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: check_patch_sentinel.py <sentinel-symbol> <module>", file=sys.stderr)
        return 1
    sentinel, module = sys.argv[1], sys.argv[2]
    try:
        mod = importlib.import_module(module)
        mod_file = mod.__file__
        if not mod_file:
            print(f"FAIL: module {module} has no source file", file=sys.stderr)
            return 1
        src = open(mod_file, encoding="utf-8").read()
    except Exception as exc:  # unpatched/missing build must fail closed
        print(f"FAIL: cannot load {module}: {exc}", file=sys.stderr)
        return 1
    if sentinel not in src:
        print(
            f"FAIL: patch sentinel '{sentinel}' absent from {module} "
            "- stock/unpatched build refused",
            file=sys.stderr,
        )
        return 1
    print(f"patch assertion OK: sentinel '{sentinel}' present in {module}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
