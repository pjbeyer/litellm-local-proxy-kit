"""lpkit-63x: graceful bounded decoder for benign stringified relay envelopes.

FP class (verified 2026-11-18): composition/relay layers emit the ``calls``
envelope as a *stringified* call-list in non-canonical dialects — single-quoted
JSON, a Python ``repr`` of a single-call dict, BOM-prefixed / whitespace-padded
JSON — and the guard's strict ``json.loads`` unwrap bounced them with LGV-020
SHAPE_ENVELOPE even though the SAME content in canonical form ALLOWs. The fix
decodes benign literal dialects gracefully (``json.loads`` then
``ast.literal_eval``, a non-executing pure-literal parser) and feeds the
decoded object back through the canonical analysis pipeline.

Security boundary pinned here:
- The decoded object re-enters canonical analysis, so a genuine kill payload
  inside a single-quoted envelope still VETOes (never a decode bypass).
- ``ast.literal_eval`` never executes; no widened parser.
- The severe variant (model text interleaved with an envelope) is NOT
  salvaged — it stays SHAPE_ENVELOPE by design. We never guess at ambiguous
  content.

Offline fixture-driven only; nothing is executed. No raw production IDs or
private paths in vectors; fixture PID 4711 / label example.litellm only.
"""
import json
import unittest

from test_lifecycle_guard import GuardHarness, guard, write_policy


# A benign single call we expect to ALLOW in canonical form.
BENIGN_CALL = {"name": "terminal", "arguments": {"command": "echo ok"}}

# A genuine kill payload (the security core: must VETO, never decode-away).
KILL_CALL = {"name": "terminal", "arguments": {"command": "kill -9 4711"}}


class TestEnvelopeDialects(GuardHarness):
    """The lpkit-63x matrix: benign stringified dialects ALLOW; the severe
    interleaved variant stays refused; genuine kill payloads still veto."""

    def ev_toolcall(self, calls_wrapper):
        # Envelope-level evaluation (the real surface for stringified calls).
        return guard.evaluate(
            "tool_call", {"calls": calls_wrapper}, self.policy,
            discover=self.discover_fake)

    # --- benign dialect forms ALLOW (the FPs fixed) ----------------------
    def test_canonical_list_control_allows(self):
        self.assertEqual(self.ev_toolcall([BENIGN_CALL]), None)

    def test_single_quote_json_calls_string_allows(self):
        wrapper = "[{'name': 'terminal', 'arguments': {'command': 'echo ok'}}]"
        self.assertEqual(self.ev_toolcall(wrapper), None)

    def test_python_repr_single_call_dict_allows(self):
        wrapper = repr(BENIGN_CALL)  # strict json.loads rejects; literal_eval takes it
        self.assertEqual(self.ev_toolcall(wrapper), None)

    def test_bom_and_whitespace_json_allows(self):
        wrapped = json.dumps([BENIGN_CALL])
        self.assertEqual(self.ev_toolcall("\ufeff" + wrapped), None)
        self.assertEqual(self.ev_toolcall("  \t" + wrapped + "\n  "), None)

    def test_double_wrapped_json_allows(self):
        # already tolerated pre-fix; pinned so the iterative unwrap stays
        self.assertEqual(
            self.ev_toolcall(json.dumps(json.dumps([BENIGN_CALL]))), None)

    def test_outer_single_quote_text_with_leading_quote_is_not_literal(self):
        # an envelope NOT bounded by a pure literal open/close (but still a
        # pure literal after quote stripping) — conservative: refused
        d = self.ev_toolcall("'[{\"name\": \"terminal\", "
                             "\"arguments\": {\"command\": \"echo ok\"}}]'")
        self.assertIsNotNone(d)
        self.assertEqual(d.get("category"), "SHAPE_ENVELOPE")

    # --- severe variant stays refused (never salvaged) -------------------
    def test_interleaved_text_stays_shape_envelope(self):
        wrapped = (json.dumps([BENIGN_CALL])
                   + "\n\nHere is the tool call I need to run:")
        d = self.ev_toolcall(wrapped)
        self.assertIsNotNone(d)
        self.assertEqual(d.get("category"), "SHAPE_ENVELOPE")

    def test_garbled_envelope_stays_shape_envelope(self):
        d = self.ev_toolcall("not-a-list")
        self.assertIsNotNone(d)
        self.assertEqual(d.get("category"), "SHAPE_ENVELOPE")

    def test_ambiguous_mixed_wrapper_stays_shape_envelope(self):
        # JSON that embeds an unquoted python-repr fragment — not salvageable
        d = self.ev_toolcall("{'name': terminal, 'arguments': None}")
        self.assertIsNotNone(d)
        self.assertEqual(d.get("category"), "SHAPE_ENVELOPE")

    # --- security core: kill payload in a dialect envelope still vetoes --
    def test_dialect_kill_payload_still_vetoes(self):
        # single-quote JSON carrying a real kill command
        wrapper = "[{'name': 'terminal', 'arguments': {'command': 'kill -9 4711'}}]"
        self.assert_veto("tool_call", {"calls": wrapper})
        # python-repr single-call dict carrying a kill command
        self.assert_veto("tool_call", {"calls": repr(KILL_CALL)})

    def test_dialect_execute_code_kill_still_vetoes(self):
        # execute_code inside a single-quoted envelope with a kill payload
        wrapper = ("[{'name': 'execute_code', 'arguments': "
                   "{'code': 'import os; os.kill(4711, 9)'}}]")
        self.assert_veto("tool_call", {"calls": wrapper})

    # --- non-decoding / non-literal strings refuse (fail closed) ---------
    def test_shallow_nonliteral_envelope_refuses_shape(self):
        # a string that is neither JSON nor a pure literal -> SHAPE_ENVELOPE
        for bad in [
            "not-a-list",
            "true",           # json.loads would parse; but not a dict/list envelope
            "[1, 2, 3]",      # parses literally but is not a call-list shape
            "42",
        ]:
            d = self.ev_toolcall(bad)
            self.assertIsNotNone(d, bad)
            self.assertEqual(d.get("category"), "SHAPE_ENVELOPE", bad)


if __name__ == "__main__":
    unittest.main()