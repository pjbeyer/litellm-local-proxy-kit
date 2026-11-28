# Implementation Plan: Hermes Exact Quality-Eval Attribution

**Status:** Draft plan for maintainer review. Implementation entry is blocked; no supported transport is claimed.
**Spec:** [Hermes Exact Quality-Eval Attribution](spec.md)

## Summary

Separate two operations: a default-off Hermes plugin records exact terminal exchange bindings, and a read-only evaluator qualifies a fixed synthetic cohort. Reuse the kit's plugin packaging and offline tests. Neither component enables scoring, modifies inference policy, or changes upstream schemas. Source inspection identifies a gap in the shipped Hermes interfaces; the architecture below is conditional on supported evidence, not a claim that the gap has been solved.

## Technical context

- **Runtime:** Python on the exact supported Hermes build; LiteLLM is a separate, local dependency. Source baselines are Hermes v0.23.3 and LiteLLM v1.110.0, not runtime support pins. No new dependency is selected.
- **Storage:** Minimal profile-private XDG state for plugin records. Use supported, exact read-only access to source-owned evidence; no private SDK/DB interface or new proxy service. Concrete adapters and storage selection are deferred to the feasibility decision, not left to an implementer to guess.
- **Tests:** Use the kit's offline pytest convention on macOS and Linux. Qualification is one bounded synthetic cohort on one loopback proxy and one profile. The read-side budget is 30 seconds from the first read; retention is capped at 24 hours from original observation. Test these draft limits; amend in review if unsupported.

## Constitution check

The [kit constitution](../../.specify/memory/constitution.md) governs this import. The source project's draft constitution is historical input, not a second authority.

| Kit principle | Draft disposition |
| --- | --- |
| I. Public source, private deployments | Publish only sanitized artifacts. Keep transcripts, resolved credentials, local paths, and raw evidence out of Git. |
| II. Preserve operator authority | Use a dedicated worktree; any disposable canary or live probe needs separate owner authorization. No changes to installed policy are part of this design import. |
| III. Specify consequential changes and verify offline | Keep gates open until the operator and reviewer complete them. Test refusals as well as success. Missing evidence stalls scoring and qualification without blocking ordinary inference; source tests do not qualify a transport. |
| IV. Keep maintenance simple | Reuse supported interfaces and kit packaging. If a supported transport cannot prove exact identity and provenance, return to design rather than privately intercepting it. |

These are draft design dispositions, not findings of conformance. No extension validation or human sign-off is recorded. See the [analysis and import gate record](analysis.md).

## Architecture and artifact placement

1. **Admit and observe:** A separately packaged integration under `integrations/hermes/quality-attribution/` admits only a qualified final local transport, adds the opaque tag, and observes supported response/terminal signals without changing transport or retry behavior.
2. **Bind:** The plugin's proposed `bindings.py` publishes a versioned ready record only after trusted wire and durable transcript evidence agree. Other dispositions and lifecycle rules are defined in the [data model](data-model.md).
3. **Qualify:** A separate proposed `qualification.py` in the integration takes independent roster and exchange evidence. It matches all eligible pairs and issues a bounded verdict at one coherent point or stalls. It does not depend on plugin registration or judge/ledger interfaces. The entry point and adapter APIs are not yet selected.
4. **Package and test:** Proposed `plugin.yaml`, `__init__.py`, `transport.py`, `bindings.py`, `qualification.py`, and `README.md` live in the integration; proposed predicate and integration tests live in `tests/test_quality_attribution_*.py`. The [tasks](tasks.md) name destinations. These paths are plans, not existing files. Existing guard, router, deployment configuration and tier census stay unchanged.

## Evidence gates

| Gate | Owner | Evidence needed and stop condition | State |
| --- | --- | --- | --- |
| G1 — Maintainer design review | Kit maintainer and operator | Review the design against the kit constitution, record the applicable extension reviews, and complete operator comprehension against the exact spec revision. | Open |
| G2 — Public transport and transcript interfaces | Kit maintainer | Investigate all four combinations; demonstrate exact final dispatch/non-egress, accepted-response header, both durable message UIDs and continuing row lineage for at least one mode on each endpoint. Reject same-UID guesses; record the supported interface or precise gap. | Blocked: the disposable probe did not bind an accepted proxy call to the exact persisted pair. See [R4](research.md#r4-separate-proof-phases-and-keep-the-failure-boundary-narrow). |
| G3 — Independent cohort and coherent verdict | Kit maintainer and evaluator owner | Show independent roster/outcome identity, authenticated proxy provenance, complete exchange/billing census and complete reads even with missing tags, plus a supported coherent cut and measured bounds. If any link cannot be supplied, return to spec review. | Not demonstrated. |
| G4 — Installed-package qualification | Operator | After red-first offline implementation and validation, authorize bounded disposable canaries on both endpoints and each advertised mode; retain sanitized receipts. No operator authorization or implementation is implied here. | Later; not executed. |

**Entry decision:** G1–G3 must be satisfied or the maintainer must explicitly approve a revised spec before any feature implementation. G4 is intentionally after implementation. An exploratory feasibility probe may only use supported interfaces and requires separate authorization if it touches a live runtime. Scoring activation is outside this plan.
