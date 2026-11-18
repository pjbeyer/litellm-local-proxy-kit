"""Structural closure: every refusal code the backend can raise must be a
declared engine code.

Engine coerces unknown codes to ``internal_error``
(``engine.Fault.__init__``), so a deliberate runtime refusal whose name was
never added to ``CODES`` surfaces anonymously. This test sweeps every
``Fault('name')`` literal raised across the backend sources and refuses the
suite if any of them is missing from the declared vocabulary.

Note: codes may also be constructed via ``Fault(code=...)`` or f-strings in
tests; this sweep covers the literal form the production backend uses.
"""
import re
from pathlib import Path

import pytest

from core.restart_control.engine import CODES

BACKEND_DIR = Path(__file__).resolve().parent.parent / "core" / "restart_control"


def _raised_codes():
    raised = set()
    pattern = re.compile(r"Fault\(\s*['\"]([a-z_]+)['\"]\s*\)")
    for source in sorted(BACKEND_DIR.glob("*.py")):
        text = source.read_text()
        raised |= set(pattern.findall(text))
        # single/double quoted variants of the same literal
        raised |= set(re.findall(r"Fault\(\s*\"([a-z_]+)\"\s*\)", text))
    return raised


def test_every_raised_fault_code_is_declared():
    raised = _raised_codes()
    assert raised, "sweep found no Fault literals; the regex drifted from the sources"
    missing = raised - CODES
    assert not missing, (
        "backend raises codes absent from engine.CODES (they would surface as "
        f"internal_error): {sorted(missing)}"
    )


def test_ambiguous_listener_is_a_declared_code():
    # Regression: runtime._listener raises ambiguous_listener for a multi-pid
    # lsof reading; engine used to coerce it to internal_error (the stale-bundle defect).
    assert "ambiguous_listener" in CODES