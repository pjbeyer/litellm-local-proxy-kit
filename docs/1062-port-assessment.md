# Port assessment: 1.103.0+4 → 1.106.2 (custom patches vs upstream)

Date: 2026-10-22 · Method: full fetch of `v1.106.2` (no clone), throwaway
worktree probe, cherry-pick + resolve + empirical test matrix.
1.107.x is excluded per owner instruction (reported compromised; not
fetched, not inspected).

## Verdict

**All three defects we patched locally still exist in 1.106.2 — none were
fixed upstream.** Every delta ports cleanly with one mechanical conflict;
the ported patches pass their full regression suite on 1.106.2.

## Our four deltas (fffbc4be45 tip) and their 1.106.2 status

1. `a01ac33996` — empty/absent stream deltas abort the whole bridge stream.
   1.106.2: **still raises** at `transformation.py:1747` (function args) and
   `:1813` (text). Ports clean.
2. `1da7971bf3` — gateway double-sends complete tool arguments
   (added + identical delta stream); clients assemble duplicate
   concatenations and reject valid JSON (multi-tool turns only).
   1.106.2: **no dedupe** (`args_already_complete` absent; deltas forwarded
   unconditionally). Ports with ONE real conflict: 1.106.2 wrapped
   `chunk_parser` in a new service-tier propagation wrapper — resolved to
   keep both.
3. `ffc4a6da84` — regression tests pinning exactly-once streamed
   arguments. Ports verbatim.
4. `fffbc4be45` — `tool_call_chunk.provider_specific_fields = …` attribute
   assignment on a TypedDict (dict) → `AttributeError` → 500 on every
   streamed tool call from metadata-carrying models. 1.106.2: **still
   assigns via attribute** at `transformation.py:1708`. Ports clean.

## Evidence (empirical, throwaway worktree at /tmp, since removed)

- Cherry-pick matrix: 3/4 auto-merge with test-dir rename only
  (`tests/test_litellm/` → `tests/unit/` upstream); fix 2 = the single
  content conflict above.
- **Mutation check:** on vanilla 1.106.2 our test files fail 13/16
  (`test_responses_bridge_boundary_delta` 8/8 fail,
  `provider_fields` 5/5 fail) — the defects are live there.
- **Fix check:** with the four ports applied, same tests 16/16 pass.
- **Regression check:** upstream's own 193-test bridge suite: identical
  fail/error sets on vanilla vs patched (13 fail + 3 errors common to
  both — probe-env artifacts: missing optional deps like `respx` in a
  minimal venv, not code). **Zero fails caused by our patches; zero
  pre-existing fails fixed accidentally.** Isolation is total.

## Feature-parity notes for the experimentation plan

`complexity_router/jev_classifier.py` and
`proxy/_experimental/mcp_server/semantic_tool_filter.py` are both present
in 1.106.2 (`classifier_type` config unchanged, 63 hits). The
post-cutover Jev/semantic-filter plan (jev-routing-mcp-semantic-filter.md)
is version-agnostic across 1.103.0 ↔ 1.106.2.

## Decision requested

Adopt 1.106.2+ports as the new pin (recommended: bug-for-bug current on
our defects, same feature surface, 3 releases of upstream fixes), carry
the port before the launchd cutover, then re-pin + re-verify. Rollback
remains the 1.103.0 tag. Alternative: stay on 1.103.0+4 and re-evaluate at
the next upstream release.
