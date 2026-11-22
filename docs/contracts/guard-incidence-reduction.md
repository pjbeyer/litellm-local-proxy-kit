# Guard incidence reduction — proposed amendment

Status: source-only implementation contract. Independent review round 1 (2026-11-22)
returned REQUEST_CHANGES (3×P2, 1×P3); all recommended edits — IR-007 bounded
resolution wording, IR-003/004/005 decision-neutrality amendment, IR-008
parse-site pinning, IR-010 testable falsification wording, IR-011 live-path
evidence — are incorporated below. Owner release approval still pending.
Date: 2026-11-22.
Audience: the guard implementation session.

This extends the existing guard and [decision-sink contract](decision-sink.md).
It does not authorize deployment, a restart, policy relaxation, or retry of a
previously refused invocation. Existing protection and bounded-evaluation rules
remain binding. No new numbered Spec Kit feature or completed Spec Kit pipeline
is claimed: the historical feature checkout is unavailable in the inspected
worktrees; this amendment lives alongside the extant portable contract.

## Requirements

- **IR-001**: Preserve fail-closed enforcement. Missing, invalid, ambiguous or
  unreadable safety preconditions refuse the protected operation. Diagnostics
  failure must never convert a refusal to an allow. Preserve existing refusal
  messages and category/code pairs, except the explicitly required syntax-error
  category correction below. No read-only-name allowlist, blanket bridge-tool
  exemption, guessed envelope repair, cooldown or session reset is introduced.
- **IR-002**: Distinguish observed evidence from interpretation. A VETO can mean
  detected protected-operation intent or an unresolved operand; it is not proof
  of malicious intent. Historical four-field sink rows cannot establish the
  cause or serving source digest. A successful behavioral discriminator proves
  the observed behavior on that invocation, not an exact callback digest or
  fleet-wide activation. Unknown attribution remains unknown.
- **IR-003**: Add bounded, closed-vocabulary diagnostic reasons at the decision
  origin, not by parsing the final message. Before implementation, enumerate the
  complete reason enum and map each producing branch to it. At minimum separate
  recognized lifecycle intent, self-protection, unresolved execution operand,
  envelope/parse failure, bound refusal, policy/integrity failure and attribution
  failure. Do not label every lifecycle-intent match a proven target hit. Keep an
  explicit unclassified value where the implementation cannot justify a finer
  distinction. Reason metadata is never an enforcement input.
- **IR-004**: Identify the serving callback generation from the source bytes
  associated with its registration, including adapter, evaluator and policy
  bindings. Capture identity outside the hook hot path. A later disk hash is not
  serving-generation evidence. If identity cannot be bound reliably, emit a
  bounded unknown value; do not invent identity or infer it from mtimes. Identity
  failure cannot disable enforcement. Pin the representation and its validation
  before implementation; do not add self-referential hashes to source files.
- **IR-005**: Keep the current four-key sink valid and unchanged until a separate,
  reviewed versioned schema and consumer migration are ready. The migration must
  define exact keys/types/enums, byte caps, legacy-read behavior, mixed-version
  rotation/append behavior, rollback, and invalid-record handling. Reuse the
  existing bounded writer, queue and lifecycle guarantees; do not add hook-path
  filesystem I/O, blocking, worker spawning, argument text, paths, process IDs,
  session identifiers, exception strings or arbitrary object conversion. Update
  every exact-schema consumer, fixture and watchdog in the same release slice.
  A consumer incompatibility is a release blocker, not permission to delete old
  evidence or silently accept an unknown schema.
- **IR-003/004/005 amendment — reason/generation decision neutrality**: Reason
  and serving-generation identity are observability metadata only. Derive each
  reason locally from that invocation's decision branch. Do not add cross-call
  decision caches, prior-decision lookups, correlation, cooldowns, or
  deduplication. The only new in-memory state permitted is an immutable
  registration-time generation binding and records admitted to the existing
  bounded sink queue; do not add a new persistent store. Collection, lookup,
  validation, serialization, or enqueue failure must not change the
  allow/refuse result, category, code, or fixed refusal message. If generation
  identity cannot be bound, emit the bounded unknown value without changing
  the decision. Existing queue counters remain bounded telemetry and must not
  influence enforcement.
- **IR-006**: Prefer isolated disposable state for synthetic probes. Do not infer
  probe provenance from a timestamp or an agent-controlled argument. If a probe
  marker is implemented, its producer must be trusted diagnostic context, not
  tool-call content, and it must never affect enforcement. Legacy row origin is
  unknown unless corroborated independently. Do not publish an organic refusal
  rate without an organic invocation denominator; counts alone are not rates.
- **IR-007**: Resolve only direct leading `$HOME/` and `${HOME}/` expansions
  in command position, plus the existing `${VAR:-$HOME/...}` default form,
  using one shared bounded resolution rule, applied across the command and
  pipeline positions already in scope. For both forms, an unambiguous
  simple-literal `HOME` assignment visible to the existing bounded
  same-payload assignment model takes precedence over inherited `HOME`. An
  explicit empty or unresolved assignment must not fall back to inherited
  `HOME`; an unset, unsafe, recursive, ambiguous, or over-bound effective
  value refuses. If no same-payload binding is visible, inherited `HOME` may
  be used only when it is an exact, nonempty, safe string. Single-quoted
  `$HOME` forms are literal and must not be expanded; if quote context is
  unavailable, refuse. Never expand prefixes such as `$HOMEOTHER`. Re-analyze
  any resolved command through the full protection pipeline; resolution is
  not an allow decision. Do not broaden this into a general shell evaluator
  rewrite, and do not execute a shell, read scripts, or resolve arbitrary
  environment variables.
- **IR-008**: Route only the `ast.parse(text)` `SyntaxError` catch in
  `_analyze_code_text` reached after the input passes the `MAX_TEXT_CHARS`
  check to SHAPE_ENVELOPE / LGV-020. Keep the `ast.parse(head)` catch in the
  over-cap/head-truncation branch as BOUND_SIZE / LGV-010. Preserve the
  existing execution-operand `SyntaxError` handling and all other parse-site
  mappings. Malformed code remains refused. Update explanatory comments and
  tests.
- **IR-009**: Keep human refusal messages fixed; if structured corrective guidance
  is added, use reviewed closed enum values, never source excerpts. Incomplete
  envelope, missing calls and unterminated grammar must remain refusals rather
  than best-effort salvage. Guidance must reflect the failing branch, not a guess.
  Repeat-amplification monitoring is deferred until a privacy-reviewed correlation
  design exists; do not add payload hashes or identifiers by implication.
- **IR-010**: For each reported false-positive hypothesis, preserve an offline
  fixture manifest pinning the exact raw payload bytes, tool metadata, policy
  and discovery fixtures, relevant environment values, and evaluator/source
  version. Assert the reproduced outcome independently of fixture construction;
  setup assertions must remain outside the evaluator's catch. The falsification
  pair must keep every manifest element identical except one named input
  element or value, with before-and-after values recorded; "one-element" means
  one named fixture component or input value, not an unspecified conceptual
  change. A denied verification check stops in its current authorization
  context; changing source paths, tools, payload encoding, or agents is not
  permission to retry it.
- **IR-011**: Record each watchdog copy's path, digest, and verified role.
  Establish the live invocation path from active scheduler or invocation
  evidence; do not infer it from the copy's location or name. A mismatch in a
  verified-unused copy is distribution drift; a mismatch in the actual
  invocation copy is a runtime failure. Reconcile copies only through the
  owner-supported installation workflow, separately from source-only changes.
  If that workflow or the live path cannot be verified, record the drift and
  keep activation blocked; do not manually replace installed copies as part
  of a source-only change.

## Acceptance and execution order

- **AC-001**: First capture baseline reds for each intended behavior change and
  preserve passing enforcement controls. Expected results are pinned independently
  of implementation constants. No imported script or signal action is executed.
- **AC-002**: IR-007 tests cover direct and braced prefixes, quoted/unquoted and
  single-quoted literals, every supported pipeline position, assignments, unset /
  empty / unsafe HOME, variable-prefix collisions, recursive expansion and bounds.
  Full-analysis controls prove resolved protected-operation intent still refuses.
- **AC-003**: IR-008 has a small malformed-Python red, a well-formed allow control,
  a genuine over-cap bound control and protected-operation refusal controls. Only
  the small malformed-code category changes.
- **AC-004**: IR-003, IR-004, IR-005, IR-006 and IR-009 require a reviewed schema /
  producer-consumer design before code. Exercise all reason branches, stale disk
  vs registered callback identity, unknown identity, legacy/mixed/invalid records,
  rollback and consumer-version refusal. Repeat the decision-sink safety battery,
  including stalled writer, contention, queue/record caps and bounded cleanup.
- **AC-005**: Run the complete guard suite with disposable state and verify no
  synthetic append to the production sink. Report discovered test count, failures
  and exit status; never substitute an earlier green run. Independently review the
  implementation and tests, then review the combined candidate by exact hashes.
- **AC-006**: Source readiness and activation are separate gates. The source review
  does not authorize installed plugin/policy/integrity-reference edits or process
  lifecycle operations. Owner-led activation must bind reviewed bytes, verify the
  serving callback, exercise approved non-disruptive controls and confirm watchdog
  health afterward. An absence of new sink rows is not proof of lower false-positive
  incidence, telemetry health, or a false-negative-free guard.

Implement in small slices: syntax-category correction; bounded HOME resolution;
then separately designed diagnostics/schema migration. Investigation of envelope
composition belongs to the upstream composer; do not weaken this guard to accept
truncated or interleaved payloads. Bridge-tool and field-name theories remain
unadjudicated and are not reasons to expand a trusted registry.
