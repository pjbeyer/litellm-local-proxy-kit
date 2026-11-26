# Data model: Exact quality-eval attribution

Logical model only. No change to Hermes or LiteLLM schemas. See [contracts](contracts/attribution.md) for admission and reader semantics.

| Entity | Owner | Minimum evidence and relationship |
| --- | --- | --- |
| Compatibility entry | Kit maintainer | Exact Hermes/LiteLLM builds, endpoint, streaming mode, relevant client/proxy configuration, sanitized receipt and explicit supported/unsupported state. An absent entry is unsupported. |
| Generation | Plugin | Opaque profile-local incarnation. Only one active for a scope; disable revokes it permanently. Re-enable starts a fresh generation. Loss of generation authority fails closed. Not sent upstream. |
| Scheduled turn | Synthetic controller | Cohort-local roster ID, generation, expected trusted scope and exact controller-to-Hermes outcome identity. Freeze before dispatch. One affirmative disposition per member: terminal or proven nonterminal. Unknown is unresolved. |
| Proxy exchange | Proxy authority | Trusted instance/client/route, exact exchange identity, expected SpendLog projection and any separately logged billing attempt identities. Member accounting does not depend on tag presence. |
| Spend row | LiteLLM | Exact row identity, trusted provenance, projected call ID, token when present, and persisted router tier when applicable. Multiple candidates are a conflict, not a ranking problem. |
| Transcript row | Hermes | Physical session, physical UID, role and supported non-content revision evidence or qualified immutability proof. Exact readback must establish both rows and continuing lineage. |
| Binding | Plugin | Schema version, cohort/generation reference, opaque token, accepted wire call ID, physical session and two message UIDs, original observation and expiry, revision evidence when needed, disposition and reason. Only `ready` is eligible; positive exclusions do not require a terminal binding. |
| Qualification report | Evaluator | Verdict and sanitized counts by unit and tier, unresolved/excluded reason counts, and an as-of evidence marker for a complete verdict. Contains no raw IDs, tags, paths or content. Does not grant activation. |

## Binding lifecycle

An observation with incomplete evidence is pending and cannot be read as ready. Only an active generation with complete, exact evidence may publish ready atomically. Equivalent redelivery is a no-op. Conflicting evidence takes precedence over pending or ready and cannot be cleared by redelivery. Revoked or unprovable generations are ineligible, including observations arriving late. Expiry is at most 24 hours from original observation, covers revision material and conflict markers, and applies at read time regardless of pruning. Pruning does not authorize replay or refresh expiry. A partial or unrecognized record is unavailable, never ready.

The plugin owns writes to its own state only. Use owner-only profile-scoped storage under the XDG state root. Transcript and SpendLog access is exact and read-only. Artifact design does not assume any upstream table structure, grant broad credential access or prescribe a shared database. No message bodies, prompts, tool arguments, credentials, raw headers, wire logs or content-derived fingerprints enter binding state or receipts.

## Independent accounting

Keep three units separate: scheduled turns, terminal roster members, and expected proxy SpendLog rows. Each scheduled member must map to a terminal member or a proven nonterminal disposition; report unresolved admissions rather than reducing the denominator. Partition terminal members into qualified, positively excluded or unresolved. Partition proxy rows separately; a retry never increments turn totals. The completed cohort's report reconciles these partitions to their own denominators. Neither a balanced count nor a stable row reread alone proves identity or completeness.
