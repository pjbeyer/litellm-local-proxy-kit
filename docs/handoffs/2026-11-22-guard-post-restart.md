# Guard post-restart verification and implementation handoff

> Historical record. The handoff branch was incorporated into the
> incidence-reduction branch. Do not fetch the old branch or treat the
> observations below as current. See the
> [guard runbook](../../integrations/hermes/proxy-lifecycle-guard/README.md).

Date: 2026-11-22. Audience: the implementation session.
Scope: source-only specification and work preparation, not activation approval.

The handoff scoped source work to syntax categorization first, then bounded HOME
resolution, with versioned diagnostics designed separately. It did not authorize
resuming the denied diagnostic, modifying installed files, activating the guard,
or restarting a service.

Read [the proposed amendment](../contracts/guard-incidence-reduction.md) and
[the existing sink contract](../contracts/decision-sink.md) together. The former
owns incidence-reduction requirements; the latter retains ownership of writer,
filesystem, queue and cleanup safety. Neither authorizes live changes.

## Verified observations

- `hermes --version` returned Hermes Agent v0.23.3 (2026.9.24), local commit
  `caafe5389ae5`, Python 3.14.9. This identifies the CLI installation, not by
  itself the version of a running daemon.
- Read-only process inspection found the Hermes ancestor chain starting at
  2026-11-22 14:30:35 local time. Process age corroborates a restart, not loaded
  module identity.
- The live guarded verification cell executed successfully with inert
  signal-family data that had previously been refused. This supports activation
  of the intended data-versus-execution behavior on that invocation. The probe
  was not an exact replay of the earlier entire invocation and did not bind all
  policy/environment/discovery inputs. Therefore exact serving callback digest
  remains unverified. Earlier chat claims that the whole generation was proven
  from this ALLOW were overstated and are superseded by this qualification.
- Reviewed checkout and installed evaluator bytes both hashed to
  `9af90b1cf578bb42928a8ccfc01f417c988969e124a98b535e1e66cb1fef010a`.
  Reviewed and installed adapters both hashed to
  `01d8478cd53213cdf410692551829b3f52951a0a132d76c5838d965778a9c43e`.
  Byte parity proves placement, not in-memory identity. The adapter captures
  `evaluate` at registration (`__init__.py:18-25`).
- Reviewed and runtime-script watchdog hashes matched:
  `07d9a402bf6af6003e4540c05eccbd022651099b252f6ffcaae3b00e5bb5cc8a`.
  The plugin-directory watchdog differed:
  `edb8796bd4316c97ad29fbcb67ae0660048a4045af64af0ba32b15fa2167fdc7`.
  This review did not establish which copy the active scheduler invokes, so
  per IR-011 the mismatch's runtime significance is unknown: potential
  distribution drift or a live-path failure until invocation evidence exists.
  Classifying the copies is implementation-session work. No installed copy
  was changed by this handoff.
- At the initial inspection, the sink held 1,485 rows, including 17 dated
  November 22: six VETO and eleven SHAPE_ENVELOPE. Fifteen were in recurring
  diagnostic time windows; timing alone is not provenance. All 17 preceded the
  observed 14:30 local restart. At that snapshot there were zero subsequent rows.
  That is neither a refusal-rate comparison nor proof of healthy logging or
  enforcement after restart. This review later produced a fresh refusal; the
  zero-row observation must not be treated as a current count.
- Fetch showed local main `0ca16ad` was one commit behind `origin/main`
  `9b24a1f02708`, with no local-only commits. This was lag, not divergence. The
  handoff branch was created from `origin/main`; guard bytes were unchanged by
  the intervening deployment-documentation commit.

## Findings and bounded work

1. **Small syntax errors have misleading guidance.** In the evaluator,
   `guard.py:3091-3094` maps under-cap `ast.parse` SyntaxError to BOUND_SIZE.
   Change only that path to SHAPE_ENVELOPE; keep the genuine over-cap head path
   at `3072-3082` bounded. Scope: IR-008, AC-001, AC-003, AC-005, AC-006.
2. **Direct HOME path resolution remains asymmetric.** The default resolver
   at `2114-2197` supports allowlisted HOME inside written defaults, whereas
   unresolved leading dollar tokens refuse at `2269-2270`. Extend the bounded
   resolver, not the allowlist, respecting assignment and quoting semantics.
   Scope: IR-007, AC-001, AC-002, AC-005, AC-006. The previous investigation's
   reproduction must be re-established in an authorized offline context before
   implementation; source inspection alone is not a new red test.
3. **Telemetry cannot answer cause/generation/probe questions.** The current
   four-key schema and exact-schema readers prohibit simply appending fields.
   Design the versioned migration and callback-bound identity first. Keep all
   existing writer/lifecycle safety invariants. Scope: IR-002, IR-003, IR-004,
   IR-005, IR-006, IR-009 and AC-004. Missing telemetry must not affect enforcement.
4. **Copy mismatch needs live-path evidence and installation reconciliation.**
   Runtime significance remains unknown until the active invocation path is
   established. Scope: IR-011. Leave installed files to the approved owner workflow.
5. **Upstream envelope composition remains a separate investigation.** Historical
   review attributed many refusals to malformed emitted envelopes. This review
   did not establish the updated composer's current status or remeasure that
   distribution. Do not transfer old percentages into a post-restart success
   claim or loosen malformed-envelope refusal to improve counts.

## Unresolved hypotheses and preserved stops

- The bridge-tool attribution theory is not adjudicated. Four approximate
  variants allowed in an earlier offline check, but they were not the three
  byte-exact refused payloads. That does not falsify the historical mechanism.
  The recovered calls used unusual nested `calls` fields, including one labeled
  `read_file`; the claim that all three were missing bridge-registry names is
  unsupported. Do not add blanket bridge aliases or exemptions from this record.
- The field-name-as-executable theory remains unproven. The initial historical
  query incorrectly applied ISO text matching to numeric Unix timestamps, so
  its zero-row result is invalid evidence, not absence of refusals.
- A combined offline-adjudication cell was freshly guard-refused during this
  review. It combined evaluator import, payload evaluation and historical reads;
  its precise refusal mechanism is unknown. The subsequent attempt changed the
  import path to the repo copy: that retry was inappropriate without clearance
  and failed at an invalid evaluator keyword before any verdict. Neither check
  produced usable adjudication results. The affected payload evaluation is
  stopped pending explicit owner-approved verification context. No additional
  payload replay, signal test, guard suite or live activation is claimed here.
- The historical feature checkout was absent from inspected worktrees. The
  amendment extends the existing `docs/contracts/` surface instead of inventing
  a replacement feature tree or silently creating new Spec Kit governance.

## Implementation sequence and gates

- Start from current fetched main in a dedicated worktree; re-read repo rules
  and the private tracker continuation. Do not alter another session's source.
- Implement syntax-category correction first, bounded HOME resolution second.
  Capture real baseline reds and retained enforcement controls in disposable
  state. Use the evaluator's actual signature and declared snapshot fields;
  assert fixture validity outside the evaluator's catch.
- Design/review the diagnostics schema separately before changing any producer
  or consumer. Existing four-key data remains valid until migration is ready.
  Include watchdogs, direct writer tests, rotation readers and rollback.
- Review tests and implementation independently, run the complete authorized
  guard suite with sink isolation, and record actual discovered counts/exit
  status against candidate hashes. Documentation checks are not guard tests.
- Source approval does not grant deployment authority. Leave installed plugin,
  policy, integrity reference, process lifecycle and denied-check resumption to
  explicit owner approval. Verify callback identity and non-disruptive controls
  after activation; do not infer successful activation from disk or silence.

## Documentation review disposition

The first independent Codex review requested changes (three P2, one P3).
The contract now pins HOME precedence and quote handling, immutable and
decision-neutral diagnostics, exact fixture inputs and falsification deltas,
and scheduler evidence for watchdog-copy classification. A second independent
source-only review confirmed the first three and found one remaining P2:
the handoff classified the watchdog mismatch without invocation evidence.
The observation and work item above now explicitly leave runtime significance
unknown. These are documentation dispositions, not a green guard suite or
release approval.

## Definition of done for implementation

Meet the amendment's acceptance criteria with actual baseline reds, isolated
regression counts and candidate hashes; independently review tests and source;
record evidence and remaining work in the private tracker; sync Beads before
pushing source and verify the remote ref. Report any blocked sync explicitly.
Share the resulting handoff with the owner. Keep source integration separate
from the owner-approved live release.

The release boundary is always-gated because it modifies a live security control.
This handoff prepares work; it neither clears existing denials nor closes the
implementation backlog. No proxy/controller lifecycle operation or external
Notion/Jira write is part of this scope.
