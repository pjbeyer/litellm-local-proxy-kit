# Research: Hermes Exact Quality-Eval Attribution

**Evidence level:** Read-only source inspection. No turns, probes against deployed stores, or live canaries were run. Nothing here qualifies a mode.

## R1. Keep wire identity separate from acceptance and persistence

**Decision:** Use the token and observed proxy call ID together for exact SpendLog matching; require independent authenticated client/route evidence and a separate accepted-exchange to physical-row lineage. Source inspection of [LiteLLM v1.110.0](https://github.com/BerriAI/litellm/tree/v1.110.0) identifies the candidate fields, not a guarantee of deployment behavior. A caller-supplied ID, normalized response, request counter or successful HTTP status cannot establish the complete chain.

**Evidence and gap:** At [Hermes v0.23.3](https://github.com/NousResearch/hermes-agent/tree/caafe5389ae585ef77387a962aee05b1bcf17029), `agent/turn_api_call.py` can discard a result after `llm_execution` returns; `agent/turn_response_intake.py` and `agent/api_request_hooks.py` expose a normalized `post_api_request` payload without raw headers; `agent/turn_finalizer.py` gives `post_llm_call` turn context and history, but no explicit terminal request identity paired with two durable revision-bearing row identities. `persistence/sqlite/conversation/_store/messages.py` can update a user row’s content without changing its UID. This refutes blanket UID immutability; it does not prove mutation after every possible binding point. A narrower immutability guarantee or a supported non-content revision signal still needs demonstration for both rows.

**Rejected:** Private SDK hooks, core patches, matching hook payload or DB content, selecting the last successful request, and treating a provider reply or current UID lookup as durable lineage.

| Candidate mode | Source-level disposition | Current support |
| --- | --- | --- |
| Chat Completions, non-streaming | No complete public chain demonstrated; a normalized response is not a wire receipt. | Unsupported |
| Chat Completions, streaming | Same gap; opening headers are not terminal acceptance. | Unsupported |
| Responses, non-streaming | Same gap; Chat Completions observations cannot qualify it. | Unsupported |
| Responses, streaming | Same gap; require mode-specific header, completion, dispatch and persistence evidence. | Unsupported |

**Stop:** If the maintainer cannot exhibit a supported end-to-end seam on at least one mode of each required endpoint, use a supported upstream build or bring the unchanged bar back for explicit spec review. Do not implement a disabled shell as a substitute or silently narrow the matrix.

## R2. Prove collection completeness independently of the join

**Decision:** Freeze a controller-owned roster before dispatch, and require a separate authoritative census of accepted and nonterminal outcomes, in-scope proxy exchanges, and expected billable rows. The census must be independent of the tag and of successful binding. Pin the existing authenticated identity, route and mode evidence and the row uniqueness/projection contract to the candidate builds.

**Rationale:** A missing SpendLog cannot be discovered by querying only SpendLogs. Stable page counts cannot establish a complete read if the snapshot or pagination can move; source or fixture success cannot prove that the runtime exposes a census. Require authoritative enumeration and closure/completeness evidence, including whether each retry generates a separate row, and a read boundary that proves no relevant page or row was omitted.

**Rejected:** Success-only, tag-only, known-binding-only, best-effort paginated reads, and expected-vs-actual aggregate counts without per-member reconciliation. An empty result is not evidence of lag or exclusion. No supported independent census or completeness signal is pinned yet.

## R3. Choose only a provable verdict point

**Decision:** Require one common validated point. A possible design is to bracket a local atomic generation/conflict/expiry decision with qualified provenance-preserving source observations. For each external authority, a complete observation and its later check must establish an unchanged interval spanning that point, with lossless change/incarnation evidence (including same-value restoration, deletion/reinsertion, and source reset) for the relevant population and revisions. Every required source must validate; separate snapshots alone are not a coherent cut. Closed immutable evidence is an alternative only with proof of closure and continuing validity through the cut. Neither mechanism is established for the baseline.

**Rejected:** Two equal sequential reads, comparing distinct-store timestamps, keeping a local mutex while uncoordinated stores change, and using UID or row count as a revision. Prove the mechanism on supported interfaces, or stop. Only positively identified transient visibility lag may trigger a whole-cohort retry, once within the draft 30-second absolute limit; report an earlier invalidation rather than turning it into lag.

## R4. Separate proof phases and keep the failure boundary narrow

**Decision:** Reuse kit plugin packaging and offline pytest conventions; do not modify the lifecycle guard or add an upstream persistence dependency. Do not choose a concrete read API, storage engine, census feed or report command until supported seams are shown. The 24-hour cap and 30-second read window are draft defaults, not measured properties. See the [plan](plan.md) for decision owners and [quickstart](quickstart.md) for evidence scenarios.

**Rejected:** Implementing against guessed APIs and using the eventual plugin canary to satisfy a pre-implementation dependency. Pre-implementation evidence may exercise supported interfaces on a separately authorized disposable setup; installed-plugin qualification is a later gate. Neither authorizes scoring.

## R5. Release recheck: public storage APIs, no demonstrated terminal binding

**Scope:** Source-only follow-up for G2 and G3. Reviewed [Hermes v0.23.4](https://github.com/NousResearch/hermes-agent/tree/8fc0374447313e276f6b9a2764bfc750190c966a), checked the relevant interface changes on [Hermes main](https://github.com/NousResearch/hermes-agent/tree/5ba14569d4dd46606b179df4f0b3c6d083b56edf), and reviewed [LiteLLM v1.111.0](https://github.com/BerriAI/litellm/tree/e3370c9d6152cc4407a55e06c101e3c7dd4168ab). No inference requests, runtime probes, or deployed-store reads.

| Path | What source inspection establishes | What is still missing |
| --- | --- | --- |
| Chat Completions, non-streaming | The [non-streaming helper](https://github.com/NousResearch/hermes-agent/blob/8fc0374447313e276f6b9a2764bfc750190c966a/agent/chat_completion_headers.py) returns a parsed completion; its raw-header capture is internal and specific to a Nous usage-window path. | A supported receipt for the exact accepted LiteLLM response. |
| Chat Completions, streaming | The [stream-open handler](https://github.com/NousResearch/hermes-agent/blob/8fc0374447313e276f6b9a2764bfc750190c966a/agent/chat_completion_helpers.py#L2892-L2900) reads the raw response inside the transport, before stream completion or cancellation is known. | A public signal that ties those headers to the completed, accepted attempt. |
| Responses, non-streaming | The inspected [direct Responses path](https://github.com/NousResearch/hermes-agent/blob/8fc0374447313e276f6b9a2764bfc750190c966a/agent/codex_runtime.py#L1322-L1334) forces `stream=True`. | A supported non-streaming Hermes path to this endpoint was not demonstrated. |
| Responses, streaming | The same path opens a raw SDK stream; its [stream handler](https://github.com/NousResearch/hermes-agent/blob/8fc0374447313e276f6b9a2764bfc750190c966a/agent/codex_runtime.py#L1348-L1353) keeps it in transport-local state. | A public, terminal receipt that carries the exact accepted response's proxy call ID. |

The request observer receives a normalized payload. The turn observer does not carry the accepted request ID with authoritative commit evidence for both message rows. Hermes can also [discard a result after execution middleware returns](https://github.com/NousResearch/hermes-agent/blob/8fc0374447313e276f6b9a2764bfc750190c966a/agent/turn_api_call.py#L139-L163). New client and turn-origin helpers can inform scope checks but supply neither the wire receipt nor the durable pair. Registering a new transport dialect is not proof of observation on the existing path and does not provide the post-commit receipt. Final dispatch and non-egress remain unproved in all four modes.

**Storage candidate:** The public conversation store exposes [`read_transcript`, `conversation_revision`](https://github.com/NousResearch/hermes-agent/blob/8fc0374447313e276f6b9a2764bfc750190c966a/persistence/conversation/messages.py#L1054-L1076), and [`read_snapshot`](https://github.com/NousResearch/hermes-agent/blob/8fc0374447313e276f6b9a2764bfc750190c966a/persistence/conversation/messages.py#L1182-L1184). The SQLite backend [increments the session revision for in-place content rewrites](https://github.com/NousResearch/hermes-agent/blob/8fc0374447313e276f6b9a2764bfc750190c966a/persistence/sqlite/_schema_ddl.py#L468-L486). That makes a conservative read-side change check worth investigating once exact rows are known. It is not an accepted-response-to-row mapping, a per-row revision or store-incarnation guarantee, or a coherent cut across Hermes and the proxy.

**Proxy candidate:** `/spend/logs/v2` still issues a bounded count and a separate [offset-paged row query](https://github.com/BerriAI/litellm/blob/e3370c9d6152cc4407a55e06c101e3c7dd4168ab/litellm/proxy/spend_tracking/spend_management_endpoints.py#L3093-L3137). Those reads do not establish a shared snapshot across pages or enumerate an exchange whose spend row never arrived. On the same scope, authenticated route/client provenance, an independent billing census, closure or change evidence, and the visibility bound still need proof.

**Disposition:** G2 remains blocked; G3 is not demonstrated. Keep all modes unsupported. First seek a supported Hermes contract that carries the final local dispatch and accepted response identity into a terminal persistence receipt naming both exact rows. Then demonstrate that contract and the independent census and coherent cut on the same authorized disposable setup. If no supported path can supply the evidence, return to maintainer spec review before writing feature code. Do not remove either endpoint or substitute an inferred join.
