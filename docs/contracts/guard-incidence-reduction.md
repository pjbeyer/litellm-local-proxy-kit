# Guard incidence reduction — proposed amendment

Status: source-only implementation contract, pending independent review and owner release approval.
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
- **IR-006**: Prefer isolated disposable state for synthetic probes. Do not infer
  probe provenance from a timestamp or an agent-controlled argument. If a probe
  marker is implemented, its producer must be trusted diagnostic context, not
  tool-call content, and it must never affect enforcement. Legacy row origin is
  unknown unless corroborated independently. Do not publish an organic refusal
  rate without an organic invocation denominator; counts alone are not rates.
- **IR-007**: Resolve direct command-position `$HOME` and `${HOME}` path prefixes
  with the existing bounded static-resolution machinery, only when the effective
  value is provable in the modeled shell environment. Apply this across command
  and pipeline positions already in scope, without executing a shell, reading
  scripts or resolving arbitrary environment variables. Respect same-payload
  assignments, unset/empty behavior, quoting and literal single-quoted forms;
  inherited HOME must not override an explicit assignment. Never expand prefixes
  such as `$HOMEOTHER`. Unprovable, unsafe, recursive or over-bound values refuse.
  Re-analyze the resolved command through the full protection pipeline; a known
  path is not an allow decision. This is not a general shell evaluator rewrite.
- **IR-008**: Route under-cap top-level Python syntax failures to SHAPE_ENVELOPE /
  LGV-020 rather than BOUND_SIZE / LGV-010. Malformed code remains refused. Keep
  the actual over-cap/head-truncation path a bound refusal; do not globally replace
  every SyntaxError mapping. Retain the existing treatment of execution operands
  unless separately specified and reviewed. Update explanatory comments and tests.
- **IR-009**: Keep human refusal messages fixed; if structured corrective guidance
  is added, use reviewed closed enum values, never source excerpts. Incomplete
  envelope, missing calls and unterminated grammar must remain refusals rather
  than best-effort salvage. Guidance must reflect the failing branch, not a guess.
  Repeat-amplification monitoring is deferred until a privacy-reviewed correlation
  design exists; do not add payload hashes or identifiers by implication.
- **IR-010**: Treat every reported false-positive mechanism as a hypothesis until
  a byte-exact offline reproduction and a one-element falsification delta support
  it under valid policy and discovery fixtures. Assert fixture construction outside
  the evaluator's catch so setup exceptions cannot masquerade as correct VETOs.
  A denied verification check stops in its current authorization context; changing
  source paths, tools, payload encoding or agents is not permission to retry it.
- **IR-011**: Verify all copies actually used or distributed, including reviewed,
  runtime-script and plugin-directory watchdog copies. Record their digests and
  the live invocation path. A stale unused copy is distribution drift, not proof
  that the scheduled runtime probe is failing. Reconcile copies only through the
  approved installation path, separately from source-only changes.

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
