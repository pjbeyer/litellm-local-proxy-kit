# Port assessment: 1.106.2+7 → 1.110.0 (custom patches vs upstream)

Date: 2026-11-18 · Method: full fetch of BerriAI tags through `v1.110.0`,
throwaway worktree probe, cherry-pick + resolve + empirical test matrix
(offline; no provider credentials, no service contact, no live inference).
Trigger: `choice.index=None` TypeError in `_chat_stream_texts`
dropping in-flight tool calls (2026-11-18 incident).

## Publication boundary and outcome

This is the pre-upgrade assessment from 2026-11-18; references to the old
"deployed" or "current" runtime below describe that assessment-time state.
Option A was subsequently selected, built, and deployed. The accompanying
`deployments/personal/pin.lock` and `native-build.json` record the 1.110.0
source and native artifact; publishing them does not activate a service.
The router-clamp and MCP-list-cache ports remain deferred.

The source branch `pin/v1.110.0-ports` at
`89cb4042afbcd963ee0fd0ae52595202d369a9e6` was verified locally, but was
**not published on the declared fork remote** at publication review on
2026-11-19. The fork's advertised older pin branch also stops before the
rollback source commit. Historical local patch hashes in this assessment are
not a claim of remote availability. Fork publication/recovery is separate,
outstanding work; do not treat this record as a remotely reproducible build.

Private tracker shorthand and workstation-specific prose have been removed.
All parsed pin values are preserved: the `source_worktree` value is retained
as historical provenance, not an installation target or a portable path
requirement. The native baseline digest covers parsed TOML excluding
`native_artifact`, so comment redaction does not invalidate the paired native
receipt. Separate runtime/controller checks can bind raw file bytes; installed
sealed artifacts must not be edited to match this publication copy.

## Verdict

**Partial adoption is required; full bug-for-bug parity is not free.**
Upstream fixed the stream-index crash and the provider-metadata AttributeError, but the
two Responses-bridge boundary defects remain live in every stable through
`v1.110.0`, and the unknown-alias reasoning-effort gap our router patch covers
is still upstream behavior. Minimum fixed stable for the stream-index crash is `v1.109.1`.

## Defect-by-defect status on v1.110.0

1. **Stream index — `choice.index=None` TypeError (served_output_texts).** FIXED
   upstream: `b3c0f3457d` (#46947, 2026-10-17) normalizes with
   `choice.index or 0` before arithmetic and restructures the aggregator so no
   bare `choice.index + 1` remains. In `v1.109.1` and `v1.110.0`; NOT in
   `v1.108.11` and NOT in
   the previously evaluated 1.108.0 candidate, whose stream-index arithmetic
   still crashes. Upgrading only to 1.108.0 does NOT fix this defect.
2. **Duplicate streamed tool-call arguments** (`aa41d4030f`). STILL PRESENT:
   `output_item.added` emits complete `function.arguments` and
   `FUNCTION_CALL_ARGUMENTS_DELTA` forwards further args unconditionally; no
   dedupe/sentinel (`args_already_complete` absent). Empirically confirmed:
   our regression tests fail 4/8 mutation checks on vanilla v1.110.0.
3. **Empty/absent stream deltas abort the bridge stream** (`a45fd07d5e`).
   STILL PRESENT: `raise ValueError("Chat provider: Invalid function argument
   delta ...")` on falsy delta (v1.110.0 `transformation.py:2304`), plus the
   text-delta variant. Empirically confirmed (4/8 of the same matrix).
4. **provider_specific_fields attribute assignment on a dict**
   (`b3ce9c4c3a`). FIXED upstream by restructuring: v1.110.0 builds
   `_ChatToolCallDict` / `ChatCompletionToolCallFunctionChunk` with
   dict-subscript assignment only; zero attribute-style assignments remain.
   No port needed.
5. **Router reasoning_effort clamp** (`7897e75e79`). SUPERSEDED, semantics
   differ: capability handling moved to
   `litellm_core_utils/reasoning_effort_capability` + `router_rust`; the chat
   path clamps/drops per-provider via
   `map_openai_params(declared_reasoning_efforts=...)` (GPT-5/OpenAI,
   azure_ai, hosted_vllm, ...; wire tests cover clamp + 400-on-undeclared).
   BUT `OpenAIUnknownModelConfig` (unknown openai-provider alias) still
   intentionally forwards any effort to the server ("forward reasoning_effort
   and let the server decide") — exactly the surface our patch clamps. The
   unknown-alias gap remains unless model_info declares levels and the client
   path passes them (responses/anthropic paths clamp; the chat openai-unknown
   path does not). Decision required: accept upstream semantics, or port the
   clamp.
6. **MCP tools/list TTL cache + post-failure listing cooldown**
   (`688802e7c3`). v1.110.0 has NO tools-listing cache (`_DiscoveryCache`
   covers only prompts/resources/templates). `origin/main` gained
   `_tool_discovery_cache` with TTL + invalidation after v1.110.0, including
   an admin-TTL path — but only positive caching: no failure cooldown. Our
   fork stays ahead of stable on the cooldown half.
7. **401 raw-key inlining** (`0bd922c220`, present on the pin branch tip but
   NOT in the deployed runtime, whose source HEAD is `7897e75e79cf`). FIXED
   upstream: v1.110.0's invalid-token message is plain text with no key
   material; no `{api_key}` interpolation remains in `user_api_key_auth.py`
   error paths. Also note: this patch exists on the branch but is not in
   `pin.lock`'s patch_commits — a pin/provenance reconciliation item regardless
   of the upgrade decision.

## Evidence (empirical, throwaway worktree, scratch; branch retained)

- Worktree: `v1.110.0` (096ff01681) checked out to a scratch path; disposable
  uv venv with `litellm==1.110.0` deps; worktree code shadowed via PYTHONPATH;
  the wheel's `_native.abi3.so` copied into the worktree's `rust_bridge/` so
  `import litellm` resolves against v1.110.0 source.
- **Mutation check (vanilla v1.110.0 + our 10-test regression file):** 8
  failed, 2 passed — defects 2 and 3 are live there.
- **Fix check (cherry-picks `a45fd07d5e`, `aa41d4030f`, `c20800d0ab` onto
  v1.110.0):** same 10 tests pass 10/10; combined scratch suite 31/31 pass.
- **Isolation check:** upstream's own bridge suite
  (`tests/unit/interactions/test_litellm_responses_bridge.py`) and
  transformation unit tests pass 11/11 and 10/10 with the ports applied.
- Cherry-pick conflicts: one real content conflict (text-delta no-op vs
  upstream's new `reasoning_summary_part.added` branch — resolved to keep
  both), plus mechanical constructor/call-site churn for the per-stream
  dedupe state. Port difficulty: low-to-moderate, same class as the 1.106.2
  port.

## Feature-parity notes

`complexity_router` and the semantic tool filter remain present in v1.110.0
(the MCP server manager was relocated `proxy/_experimental/mcp_server/` →
`proxy/mcp_server/` with a types split). Semantic-filter integration planning remains
version-agnostic.

## Decision requested

Options, in order of preference:

A. **Upgrade to v1.110.0 + ports {2, 3} (+ optionally 5).** Fixes the stream-index crash and
   defect 4 upstream; carries the relocated MCP server; keeps our dedupe and
   boundary-delta behavior. Requires a fresh native-build receipt, pin.lock
   rewrite, full regression matrix, and the managed-service swap procedure.
B. **Upgrade to v1.109.1 + ports {2, 3} (+ 5, 6).** Minimum stream-index fix; same
   port burden; older base.
C. **Stay on 1.106.2 and port only the stream-index one-liner** onto the pin branch,
   rebuild the runtime, re-pin. Smallest diff, but diverges from upstream
   further and leaves the other upstream fixes unharvested.

Rollback for any option remains the current verified-7897e75e79cf runtime.

Also reconcile: `0bd922c220` (401 log-scrub) is on the pin branch but absent
from `pin.lock` patch_commits; either add it to the pin (and redeploy) or
document why the deployed runtime runs without it.
