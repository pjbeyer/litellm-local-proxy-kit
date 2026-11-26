# Interface contract: Attribution and qualification

**Status:** Design contract. Names below describe required evidence, not Hermes APIs or an available evaluator command.

## 1. Admission and wire projection

Operator inputs: an explicit profile opt-in, exact loopback scheme/host/port plus endpoint, named alias allowlist, and a qualified build/mode/configuration entry. Require proof of the final dispatch and no tag egress on redirects, fallback or provider dispatch. Unsupported input, missing proof, scope drift or a malformed/prepopulated attribution field leaves the original request unchanged. Detached, background, delegated and auxiliary work is out of scope.

The sole new wire field is a versioned opaque value under the logical key `hermes_eval_attribution_v1` in `x-litellm-spend-logs-metadata`. The key is proposed for the kit and must be verified against the pinned parser, including ambiguity and size limits. Preserve unrelated headers and metadata; never adopt a caller's value, set the proxy call ID, or send Hermes IDs. The strictly bounded token generation is local; a retry repeating the token is not evidence of a unique wire attempt. Require the accepted `x-litellm-call-id` and issued token to identify exactly one authoritative row through `metadata.litellm_call_id` and `metadata.spend_logs_metadata`, against independently verified proxy/client provenance. A single match does not prove terminal acceptance.

## 2. Binding producer and reader

Require the supported runtime to supply exact accepted-exchange-to-final-row lineage and the exact associated input row. Verify roles, physical session, durability and revision currency. Buffered, rejected, partial, interrupted, replayed, superseded or synthetic recovery responses do not bind. Use the [data model](../data-model.md) for minimal persistence, atomic conflict precedence, idempotence, generation capture and expiry. Missing, malformed, conflicting, expired or unknown evidence never becomes ready. Do not retry inference or modify transcripts to recover a missing join.

## 3. Qualification input and output

Input is a fixed, bounded, synthetic cohort roster and its independent authoritative scope/accounting evidence: trusted proxy instance, authenticated client, route, endpoint/mode, profile and generation. Neither expected-exchange census nor roster admission may be based on which rows are tagged or bound. Read every in-scope expected row, not a success-only subset. Positive nonterminal/unrelated exclusions require independent evidence. Exclusion of a terminal member by router tier requires independent exact linkage to the authoritative SpendLog. `COMPLEX` is the Smart Router target, `REASONING` is accounted separately, and alternates are qualified separately without a fabricated tier.

| Result | Exit | Meaning |
| --- | --- | --- |
| `pass` | 0 | Whole-cohort evidence coherent and complete, no unresolved items, at least one eligible sample for the requested cohort/tier. |
| `no samples` | 0 | Whole-cohort evidence coherent and complete, no unresolved items, zero eligible samples for that cohort/tier. Not a pass. |
| `EVAL STALL` | 2 | Unsupported scope, invalid input, incomplete census/read, non-unique or missing evidence, revoked generation or no provable coherent verdict point. No success verdict. |

For completed verdicts, validate roster outcomes, complete proxy records, conflict state, both row revisions, expiry and active generation at one as-of point. A conditional design is documented in [research](../research.md); simple sequential rereads or wall-clock coincidence do not establish coherence. Expiry is enforced at that point. Later revocation prevents reuse. One retry within the absolute 30-second first-read budget is allowed only for positively identified transient visibility lag, not for ambiguous or stale evidence; retry keeps cohort membership fixed and repeats whole-cohort validation.

Sanitized reasons use a stable vocabulary distinguishing unsupported mode/scope, missing outcome, incomplete read, missing expected spend, provenance/tier unavailable, ambiguity/conflict, expired/revoked and unprovable coherence. Exclusion reasons distinguish positive nonterminal, unrelated, and recognized non-target tier. No raw identifiers, tokens, payloads or headers appear in results. Read-only qualification cannot invoke judges, write ledgers, change the scheduler or enable scoring.
