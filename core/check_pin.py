#!/usr/bin/env python3
"""Assert the pinned build's provenance matches pin.lock (FR-006, P1 review fix).

The patch sentinel proves only that a symbol exists in an importable module —
it cannot distinguish builds. This check adds two stronger assertions:

  1. dist-info: the interpreter's installed `litellm` distribution version must
     equal pin.lock's `base_version`.
  2. source tip: the `litellm` package actually imported must live in a git
     worktree whose HEAD equals pin.lock's `patch_tip` (editable installs make
     the worktree the runtime source; a moved/advanced worktree must fail here,
     not silently change what production imports at next restart).

Exit 0 = provenance proven; exit 1 = refuse (fail closed).

Usage: check_pin.py <pin.lock> [--python <interpreter-path>]
The --python form re-execs itself under the pinned interpreter so the dist-info
and import resolve in the build's own environment, not the caller's.
"""
from __future__ import annotations

import subprocess
import sys
from importlib import metadata
from pathlib import Path


def load_pin(path: Path) -> dict[str, str]:
    """Parse the flat `key = "value"` form (TOML-anchored lines, comments ok)."""
    fields: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith("["):  # inline array (patch_commits) is not a scalar field
            continue
        if value.startswith("#") or not value:  # blank or bare-comment line
            continue
        if key in fields:
            continue
        # A quoted value is a whole string — take the closing quote as the end
        # and ignore anything after it (trailing comment). An unquoted value
        # ends at the first " #" comment marker.
        quote = value[0] if value[0] in ('"', "'") else None
        if quote:
            end = value.find(quote, 1)
            if end == -1:
                continue  # malformed: unterminated quote, fail closed below
            value = value[1:end]
        else:
            hash_pos = value.find(" #")
            if hash_pos != -1:
                value = value[:hash_pos].rstrip()
        fields[key] = value
    return fields


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--python":
        if len(args) != 3:
            print("usage: check_pin.py --python <interpreter> <pin.lock>", file=sys.stderr)
            return 1
        pin = str(Path(args[2]).resolve())
        reexec = subprocess.run(
            [args[1], Path(__file__).resolve(), pin],
            capture_output=True, text=True, timeout=120,
        )
        sys.stdout.write(reexec.stdout)
        sys.stderr.write(reexec.stderr)
        return reexec.returncode
    if len(args) != 1:
        print("usage: check_pin.py <pin.lock> [--python <interpreter>]", file=sys.stderr)
        return 1

    pin = load_pin(Path(args[0]))
    base = pin.get("base_version", "")
    tip = pin.get("patch_tip", "")
    if not base or not tip:
        print("FAIL: pin.lock lacks base_version or patch_tip", file=sys.stderr)
        return 1

    try:
        dist_version = metadata.version("litellm")
    except metadata.PackageNotFoundError:
        print("FAIL: no litellm distribution in this interpreter — refusing", file=sys.stderr)
        return 1
    if dist_version != base:
        print(f"FAIL: dist-info version {dist_version!r} != pin base_version {base!r}",
              file=sys.stderr)
        return 1

    import litellm
    package_root = Path(litellm.__file__).resolve().parent.parent
    head = subprocess.run(["git", "-C", str(package_root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=30)
    if head.returncode != 0:
        print(f"FAIL: litellm imports from {package_root}, which is not a git worktree "
              f"(editable source expected); cannot assert patch_tip", file=sys.stderr)
        return 1
    actual_head = head.stdout.strip()
    # pin.lock stores a short SHA; the worktree HEAD must start with it.
    # A full-HEAD equality check would reject a legitimate long-form tip.
    if not tip or not actual_head.startswith(tip):
        print(f"FAIL: worktree HEAD {actual_head[:10]} != pin patch_tip {tip}",
              file=sys.stderr)
        return 1
    print(f"pin provenance OK: dist-info {dist_version}, HEAD {actual_head[:10]} matches pin.lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
