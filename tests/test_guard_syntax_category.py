"""IR-008 (guard incidence reduction): under-cap malformed-Python category.

The amendment (docs/contracts/guard-incidence-reduction.md) pins:
- A SMALL (under MAX_TEXT_CHARS) Python payload that fails ast.parse is a
  SHAPE DEFECT — refuse SHAPE_ENVELOPE / LGV-020 ("restructure it"), not
  BOUND_SIZE / LGV-010 (misleading "reduce the input size" guidance for
  input that is not oversized at all).
- The genuine over-cap path (head-truncation branch) KEEPS BOUND_SIZE.
- The over-cap head SyntaxError and the execution-operand SyntaxError
  routing are preserved unchanged.
- Malformed code stays REFUSED (fail-closed); only the category changes.
- Protected-operation refusals still refuse in both categories.

Expectations below are pinned from the amendment text, independently of
the implementation constants. Offline, fixture-discovery only; nothing
executes. Fixture PID 4711 / label example.litellm only.
"""
import sys
import unittest
from pathlib import Path

MODULE_PATH = (Path(__file__).resolve().parents[1] /
               "integrations/hermes/proxy-lifecycle-guard/guard.py")

from test_lifecycle_guard import GuardHarness, guard, write_policy

# A real newline inside a single-quoted string: the documented byte-exact
# refusal shape (git multi-paragraph -m convention pasted into code) —
# invalid Python, under any size bound.
SMALL_MALFORMED = "x = 'line one\n\nline two'"
# Malformed AND carrying signal vocabulary: still refused, still SHAPE.
SMALL_MALFORMED_SIGNAL = "cmd = 'pkill -f litellm\nrest"
# Well-formed benign control.
WELL_FORMED = "x = 'line one\\n\\nline two'\nprint(x)"
# Protected-operation controls in each category class.
KILL_PYTHON = "import os\nos.kill(4711, 9)"
OVERCAP_SAFE = "x = 1\n" * 20000            # > MAX_TEXT_CHARS (65536)
OVERCAP_SIGNAL = OVERCAP_SAFE[:2000] + "\nimport os\nos.kill(4711, 9)\n" + "y = 2\n" * 20000


class TestSyntaxCategoryIR008(GuardHarness):

    def ev_term(self, code):
        return guard.evaluate("execute_code", {"code": code}, self.policy,
                              discover=self.discover_fake)

    # --- the intended change: small malformed Python -> SHAPE_ENVELOPE ---
    def test_small_malformed_python_is_shape_envelope(self):
        d = self.ev_term(SMALL_MALFORMED)
        self.assertIsNotNone(d)
        self.assertEqual(d.get("category"), "SHAPE_ENVELOPE")
        self.assertEqual(d.get("log_code"), "LGV-020")
        self.assertIn("could not be proven safe", d.get("message", ""))

    def test_small_malformed_signal_code_is_still_refused(self):
        # malformed + signal vocabulary: fail-closed, SHAPE category
        d = self.ev_term(SMALL_MALFORMED_SIGNAL)
        self.assertIsNotNone(d)
        self.assertEqual(d.get("category"), "SHAPE_ENVELOPE")

    # --- retained controls (must not change) ------------------------------
    def test_wellformed_python_allows(self):
        self.assertIsNone(self.ev_term(WELL_FORMED))

    def test_wellformed_kill_python_vetoes(self):
        d = self.ev_term(KILL_PYTHON)
        self.assertIsNotNone(d)
        self.assertEqual(d.get("category"), "VETO")
        self.assertEqual(d.get("log_code"), "LGV-001")

    def test_overcap_safe_code_allows_via_bound_row_a(self):
        # genuine over-cap, provably benign: FR-007 row (a) ALLOW control —
        # the bound decision table must not over-block (amendment: only the
        # syntax category changes; benign over-cap allows are retained).
        self.assertIsNone(self.ev_term(OVERCAP_SAFE))

    def test_overcap_kill_intent_still_vetoes(self):
        # over-cap with kill intent: veto is never lost to the bound
        d = self.ev_term(OVERCAP_SIGNAL)
        self.assertIsNotNone(d)
        self.assertEqual(d.get("category"), "VETO")

    def test_overcap_malformed_head_stays_bound_size(self):
        # over-cap whose HEAD does not parse keeps BOUND_SIZE (IR-008:
        # the head-truncation catch is preserved)
        big = "'" + "a" * 70000  # unterminated string literal, over cap
        d = self.ev_term(big)
        self.assertIsNotNone(d)
        self.assertEqual(d.get("category"), "BOUND_SIZE")


if __name__ == "__main__":
    unittest.main()
