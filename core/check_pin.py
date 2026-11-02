#!/usr/bin/env python3
"""Shared pre-import runtime proof: repository or exact installed flat layout.

Usage: check_pin.py [--python <interpreter>] [--repository] <pin.lock>
Installed flat layout is the default; repository mode is explicit.
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True  # before importing even the proof helper
import importlib.util
import subprocess
from pathlib import Path


def _helper(repository=False):
    # Wrappers always use the installed layout; only the repository installer
    # supplies --repository. Presence of a directory never chooses executable code.
    parent = Path(__file__).absolute().parent
    flat, nested = parent/'runtime_identity.py', parent/'restart_control'
    if repository:
        if flat.exists() or flat.is_symlink():
            raise ValueError('runtime_helper_ambiguous')
        path = nested/'runtime_identity.py'
    else:
        if nested.exists() or nested.is_symlink():
            raise ValueError('runtime_helper_ambiguous')
        path = flat
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError('runtime_helper_missing')
    spec = importlib.util.spec_from_file_location('pinned_runtime_proof', path)
    if spec is None or spec.loader is None:
        raise ValueError('runtime_helper_missing')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_pin(path):
    return _helper(repository=True).read_pin(path)


def main():
    args = sys.argv[1:]
    try:
        if args and args[0] == '--python':
            if len(args) not in (3,4):
                raise ValueError('invalid_input')
            result = subprocess.run([args[1], '-B', str(Path(__file__).absolute()), *args[2:]],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            if result.returncode:
                raise ValueError('runtime_invalid')
        else:
            repository = bool(args and args[0] == '--repository')
            if repository:
                args = args[1:]
            if len(args) != 1:
                raise ValueError('invalid_input')
            _helper(repository=repository).prove_runtime(args[0])
    except Exception:
        print('FAIL: pinned runtime provenance invalid', file=sys.stderr)
        return 1
    print('pin provenance OK: shared runtime content proof')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
