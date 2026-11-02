#!/usr/bin/env python3
"""Shared pre-import runtime proof: repository or exact installed flat layout.

Usage: check_pin.py <pin.lock> or check_pin.py --python <interpreter> <pin.lock>
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True  # before importing even the proof helper
import importlib.util
import subprocess
from pathlib import Path


def _helper():
    parent = Path(__file__).absolute().parent
    path = (parent/'restart_control/runtime_identity.py'
            if (parent/'restart_control').exists() or (parent/'restart_control').is_symlink()
            else parent/'runtime_identity.py')
    if path.is_symlink() or not path.is_file():
        raise ValueError('runtime_helper_missing')
    spec = importlib.util.spec_from_file_location('pinned_runtime_proof', path)
    if spec is None or spec.loader is None:
        raise ValueError('runtime_helper_missing')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_pin(path):
    return _helper().read_pin(path)


def main():
    args = sys.argv[1:]
    try:
        if args and args[0] == '--python':
            if len(args) != 3:
                raise ValueError('invalid_input')
            result = subprocess.run([args[1], '-B', str(Path(__file__).absolute()), args[2]],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            if result.returncode:
                raise ValueError('runtime_invalid')
        else:
            if len(args) != 1:
                raise ValueError('invalid_input')
            _helper().prove_runtime(args[0])
    except Exception:
        print('FAIL: pinned runtime provenance invalid', file=sys.stderr)
        return 1
    print('pin provenance OK: shared runtime content proof')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
