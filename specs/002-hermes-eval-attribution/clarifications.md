# Clarifications: Hermes Exact Quality-Eval Attribution

This records design choices in the [spec](spec.md), not operator approvals. No live authorization or human comprehension approval was recorded.

| Question | Draft disposition |
| --- | --- |
| Which endpoints and modes are in V1? | Investigate all four endpoint/mode pairs; show at least one supported mode on each required endpoint. Advertise only individually qualified modes. No support inferred from source or from a raw HTTP request. |
| What is a V1 population? | A bounded synthetic roster fixed before dispatch and reconciled by exact identity, with independent exchange/billing census. No free-form traffic, backfill or scoring. |
| Can a UID or a successful wire response stand in for durable lineage? | No. Require exact accepted-exchange lineage and current physical revisions or demonstrated immutability for both rows. Unknown means stall. |
| What happens when a public seam is unavailable? | Keep the path untagged and unsupported. Identify the gap and return it for maintainer decision; don't add a private hook or reduce the two-endpoint bar without an explicit amendment. |
| When can a complete verdict be returned? | At a provable common validation point. Retry only positively identified visibility lag once within the 30-second absolute budget. A complete empty target is `no samples`, not a pass. |
| Are retention and read bounds measured? | No. The 24-hour retention cap and 30-second read budget are draft defaults. Validate them against the candidate runtime before implementation; revise in review, not in code. |

What remains unresolved is feasibility and approval, not ambiguity to be settled by the implementer. See the [evidence gates](plan.md#evidence-gates).
