# Research: Hermes Exact Quality-Eval Attribution

**Evidence level:** Source inspection and disposable runtime observations ([R4](#r4-separate-proof-phases-and-keep-the-failure-boundary-narrow)). The positive control on each LiteLLM endpoint matched a SpendLog row. The observed Hermes hooks did not bind an accepted proxy call to the exact persisted message pair. No deployed store was queried, and no mode is qualified.

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

### Disposable runtime probe: proxy match verified, Hermes binding not demonstrated

The operator authorized a disposable probe with synthetic inputs and no production-store writes. The first run stopped before Hermes dispatch and SpendLog readback. The follow-up used [Hermes v0.23.4](https://github.com/NousResearch/hermes-agent/tree/8fc0374447313e276f6b9a2764bfc750190c966a) and a locally patched LiteLLM fork based on upstream v1.110.0. The fork included patches to ignore empty stream deltas and prevent duplicate streamed tool-call arguments, plus a regression test for the latter. The Responses observations therefore apply to the patched build, not unmodified upstream LiteLLM. The probe used a network-isolated container with a fresh Hermes profile, a disposable PostgreSQL database, and a loopback upstream fixture. It did not access a deployed store or provider.

**Proxy controls.** One direct non-streaming request per endpoint through `smart-router` returned `x-litellm-call-id`. In each case exactly one disposable SpendLog row matched the call ID (`litellm_call_id`), attempt tag, authenticated key hash, and router alias. `request_id` did not equal the call ID. The wrong-tag control found no tag match for the real call ID; the wrong-call-ID control found no row. The fixture received neither control tag. This verifies these two controls, not a complete or independently enumerated billing census.

**Hermes observer trials.** Each configuration completed an initial and resumed turn. The fixture recorded the following upstream exchanges; wire mode is observed at that fixture, not inferred from the display setting.

| Hermes configuration | Fixture exchanges | Successful `post_api_request` events | `post_llm_call` events |
| --- | --- | ---: | ---: |
| Chat Completions, streaming off | Non-streaming: one 503, then successful retry, tool follow-up, and resumed turn | 3 | 2 |
| Chat Completions, streaming on | Streaming: two successes | 2 | 2 |
| Responses, streaming off | Streaming: two successes | 2 | 2 |
| Responses, streaming on | Streaming: two successes | 2 | 2 |

The pinned Hermes `codex_responses` transport uses the stream runner even from the non-streaming dispatch branch (`agent/chat_completion_helpers.py`); the streaming-off trial did not exercise a non-streaming Hermes Responses exchange. The observer hooks exposed internal request and turn IDs and history with message UIDs. Readback from the disposable SQLite store found UIDs on every observed session's user and assistant rows. No observed hook payload contained `x-litellm-call-id`, and no terminal hook paired the accepted proxy call with the exact durable user and final assistant rows and their revisions.

A repeat added a passive `llm_execution` middleware wrapper at runtime. It returned the provider result unchanged and recorded the request and turn IDs, API mode, and whether the inspected public header accessors exposed the call ID. They did so on **0 of 9 successful returns**. The wrapper did not record header values, and this result does not establish that the header was absent on the Hermes–proxy wire. Hermes can still discard the result after the middleware returns if a redirect intervenes (`agent/turn_api_call.py`), so a header at that point would not prove acceptance. Neither run sent the control tag from Hermes or proved final-dispatch non-egress, row lineage, or a coherent read point.

**Disposition:** G2 and G3 remain unmet. The proxy-side control is no longer the immediate blocker. Require a supported Hermes interface that binds the accepted response's allowlisted call ID to the exact persisted message pair and revision lineage, or return to maintainer review with that gap. Do not implement the plugin or enable scoring on the strength of this probe.
