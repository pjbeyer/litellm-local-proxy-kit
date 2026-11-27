# Validation guide: Hermes Exact Quality-Eval Attribution

This is a validation design, not installation instructions for an available feature. Do not run the future fixture command or a live probe from this draft.

## 1. Review the design offline now

Read the [spec](spec.md), [plan and gate register](plan.md) and [interface contract](contracts/attribution.md). Review all four candidate modes in [research](research.md). Treat source inspection, runtime feasibility and feature qualification as separate evidence. No credential, real transcript, live service, or traffic is required for this review.

## 2. Establish feasibility before implementation

With separate operator authorization, use an isolated profile and disposable proxy configuration, deterministic provider/control and redirect sinks, bounded synthetic inputs, and explicit request and spend ceilings. Pin the exact builds, endpoints, stream modes and configuration fingerprint. No production data, production service changes or inferred provider calls. Reject the setup if the isolation or bound cannot be shown.

1. Investigate non-streaming and streaming on Chat Completions and Responses. For each mode, record whether supported interfaces prove final dispatch/no egress, the accepted exchange and both exact durable row identities and current revisions. Exercise response supersession, a duplicate/retry, interrupted stream, and a same-UID edit or prove the edit is disallowed within the qualified boundary. A raw HTTP client or a forced hook payload cannot substitute for a real Hermes turn.
2. Demonstrate the independent roster/outcome and billing census on the same qualified path. Verify authenticated provenance with a control client and show why reads are complete when tags are missing, spend arrives late or expected retry rows are absent. Demonstrate a coherent cut by making invalidation before the cut refuse even when all other evidence is valid. Show a post-cut revocation prevents reuse.
3. Retain only sanitized observations. Classify each mode separately. Missing or unsupported evidence stays a blocker; minimum is one demonstrated mode per required endpoint, with all four investigated. No probe is authorized by this guide.

## 3. Exercise the future offline fixtures first

Follow the kit’s [pytest setup](../../README.md) in an isolated environment. The [tasks](tasks.md) name the proposed test modules; only run them once they exist. Use synthetic labels and locations and no provider credentials. Pin exit status, verdict text and expected counts independently of code. Establish red against the predecessor before making a behavior change. Assert each negative test reaches the intended check, not an unrelated early rejection.

| Fixture | Expected result |
| --- | --- |
| Two overlapping turns in one session; reversed arrival, billed retry, tool follow-up, and unrelated client | Exactly the two expected UID pairs, no cross-turn, cross-client or rejected-attempt bindings. Alternate alias has no synthetic tier. |
| Independent roster and billing census with equal-but-wrong identities, untagged relevant traffic, missing retry row or incomplete pages | `EVAL STALL` (2), even if a terminal join matches; no judges or ledger writes. |
| Complete zero-eligible cohort; complete REASONING-only cohort; linked recognized tier exclusions | `no samples` (0) for the specified target, not a pass; separate counts and reason totals. Unlinked tier is unresolved. |
| Both row mutations, ABA, conflict, expiry and generation revocation around the as-of point | Pre-cut invalidation or unprovable coherence stalls; proven later revocation prevents reuse; late events cannot revive prior generation. |
| Positive lag within budget; ambiguity, incomplete read or deadline crossed | Retry once only for confirmed lag; one eligible join on recovery within the budget. Other failures stall, with no successful verdict emitted. |
| Redirect/fallback, control client and provider sink; plugin disabled or faulted | No plugin tag outside configured local ingress. Preserve inference, routing, affinity and trace semantics; no newly eligible binding while disabled or faulted. |

## 4. Later: qualify installed code, then roll back

After green offline fixtures, validate the package with `hermes plugins validate`. Run separately authorized disposable synthetic canaries with scoring disabled, one `COMPLEX` success on each required endpoint and mode-specific proof for every advertised mode. Collect a sanitized receipt linking the pinned build/configuration to observations of dispatch, header projection and non-egress, durable readback and qualification. Run disabled controls: stop new tags, revoke the active generation, discard late bindings, and verify ordinary inference and the tier census still work. Residual local records expire automatically; existing native SpendLog tags stay under native retention, inert for this feature. No change to production scoring or deployment follows from a receipt.
