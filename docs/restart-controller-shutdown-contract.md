# Cap-aware controller shutdown amendment

**OWNER-APPROVED DESIGN AND IMPLEMENTATION PLAN — offline implementation in progress. Not deployed or platform-qualified.**

This proposed amendment extends [the controller contract](restart-controller.md)
and [operations guide](restart-controller-operations.md). It does not create a
second deployment source of truth or a new Spec Kit workspace. The repository's
existing documentation-contract layout is retained; no full Spec Kit CLI
pipeline is claimed. Approval of this written amendment precedes an implementation
plan and tests. Production activation and handoff remain separate owner gates.

## Intent and observed constraint

Provide a controller that stops admitting work when asked to shut down, preserves
uncertain mutation outcomes, and can be qualified against its actual supervisor.
Do not make qualification pass by ignoring the platform's shorter shutdown window.
The healthy proxy must remain untouched throughout development and isolated tests.

On the investigated GUI controller bootstrap, launchd explicitly logged that the
requested `ExitTimeOut` exceeded its maximum and was changed to 60 seconds. The
loaded job also reported 60. This is evidence for that bootstrap, not a universal
limit for all macOS versions or launchd domains. The current source requests 1000,
permits 900-second operations, and joins worker threads for up to 1000 seconds.
Those internal bounds cannot establish a 1000-second platform shutdown grace.

The observed loaded environment also contains launchd-origin `OSLogRateLimit=64`,
absent from the generated plist. Its origin does not justify accepting arbitrary
values or ignoring additional environment fields.

## Design decision and alternatives

**Proposed: a new explicit 60-second supervision generation, with a separate
50-second shutdown deadline and tested interruption/manual-recovery semantics.**
Long normal operations may remain possible, but cannot be promised completion
after shutdown begins. Qualification requires both declared and observed timeout
60, not an exception accepting an observed 60 against a declared 1000.

Rejected for this slice:

- Merely changing the configured timeout to 60 and retaining the existing drain
  assumption would hide, not repair, the shutdown mismatch.
- Keeping a requested 1000 while trusting a record saying effective 60 adds a
  second timeout authority without providing a longer grace period. A deliberate
  future requested-versus-effective model would need its own reviewed contract.
- Moving transactions into another persistent supervisor expands the subsystem
  and still does not guarantee completion during machine shutdown.

## Requirements

### SC-001 — Exact supervision and logging declaration

The next generated controller definition explicitly sets `ExitTimeOut: 60` and
`EnvironmentVariables.OSLogRateLimit: "64"`. Installer and qualification use the
same generator. The logging value is an intentional non-secret configuration
setting, not an implicitly ignored key. Compare its loaded value exactly; missing,
different, duplicate or additional explicit environment entries refuse. Retain
only the existing matching `XPC_SERVICE_NAME` metadata normalization.

The loaded timeout must be one unique top-level observation equal to the declared
integer 60. Missing, duplicate, nested-only, malformed or different observations
refuse. No universal platform cap is assumed and no plist value is rewritten in
place to match a platform observation.

### SC-002 — One shared shutdown state and deadline

Signal handling, wire admission, engine actions and runtime helpers share one
monotonic shutdown state. First SIGTERM/SIGINT sets it once and fixes a deadline
50 seconds later; later signals cannot extend it. This deadline includes draining,
owned-child cleanup, evidence writes and socket cleanup. It is not a fresh budget
per worker or phase. Normal-operation and shutdown deadlines are distinct; use
the earlier applicable deadline for ongoing work.

Owner-approved primitive semantics: `checkpoint()` permits normal work before
shutdown and raises `ShutdownRequested` afterward, rather than reporting elapsed
time. Cleanup waits genuinely sleep for at most their requested real-time
interval or the remaining shared deadline; false means the interval ended with
budget remaining, true means the deadline expired. A new latch may wake an
already waiting cleanup caller for recheck without claiming deadline expiry.
Frozen injected clocks must not make interval waits unbounded or busy-spin.
Finite negative clock origins are valid; booleans/nonfinite values and observed
backward progression fail closed. Limits/intervals remain finite and nonnegative,
with positive wait intervals. Prove actual same-thread signal-style reentry into
state handling, not merely sequential requests after the latch.

The 50-second deadline leaves headroom before the declared 60-second platform
limit, but is not an assurance against scheduler starvation, hung kernel I/O,
power loss or earlier forced death. Such failures require durable recovery state.

### SC-003 — Stop admission and prevent later mutation

On shutdown, stop accepting new connections promptly and refuse already accepted
`prepare`/`restart` requests that have not started. The signal latch and deadline
must take effect without waiting for the long-running mutation lock. Separately,
a short submission gate serializes individual action commitments. Define its
linearization point as the successful shutdown-open check admitting one specific
action; this is distinct from eventual OS submission or completion. An action
committed before the latch may already be in flight or reach the OS afterward;
classify it conservatively as possibly effective, and cancel it if still safely
cancellable. No action may commit after the latch, and a committed action grants
no permission for another action. The implementation must prove this ordering,
not merely place an unsynchronized check next to Popen.

Check shutdown at mutation admission, before consuming intent, and at every
helper, credential-resolution, candidate-creation, HTTP acceptance, service-control
and rollback admission. After the latch, permit only bounded cleanup/evidence
work and handling of the single previously committed action; do not begin the
next step of an already-running prepare. Return a closed `controller_stopping`
code when a response remains possible. No RPC expands the closed interface.

A service-control action committed before shutdown may have an unknown outcome. It is not replayed, inferred successful, or followed by another
service-control action. This rule applies after a helper returns as well as
before the next step. No automatic start/rollback is added to hide uncertainty.

### SC-004 — Cooperative interruption and owned-child evidence

Make helper waits, acceptance polling, HTTP deadlines and cleanup observe the
shared shutdown state/deadline. A blocked selector or sleep must not retain the
old 900/1000-second allowance. Permit only bounded cleanup of retained owned
candidate/helper process groups after checking UID/PID/birth/group identity.
Never signal a primary listener or an unknown/reused identity; never escalate to
SIGKILL or substitute another service-control verb.

Before child creation or action commitment, durably retain a bounded spawn intent
with purpose and frozen operation binding, without argv, credentials or environment
values. After spawn, acquire and durably record identity when obtainable. A crash
between spawn and identity persistence cannot be eliminated: a surviving intent
without proven identity is uncertain, blocks further mutation, and authorizes no
automatic process/group signal. If identity acquisition or cleanup is uncertain,
retain that uncertainty and block further mutating work. A separate-session helper/candidate surviving controller death is
not assumed reaped by launchd. No blind PID-file cleanup is permitted.

### SC-005 — Durable mutation fence and recovery barrier

Before the first primary service-control action commits under the submission
gate, create and sync a private operation fence bound to request, controller epoch,
frozen digests and current phase. An uncertain
consumed-intent write or fence-write outcome stops control and is not retried.
Clear the fence only after a durable accepted terminal result and verified owned
child cleanup. A crash between terminal persistence and fence removal may leave a
conservative recovery requirement; it must not authorize replay.

On startup and under the mutation lock before every prepare/restart admission,
check the complete bounded set of operation/fence, consumed-intent and spawn/child
records, including incomplete writes. Recheck before each helper/action admission.
An unresolved fence, uncertain consumed operation or unreconciled spawn/child
record blocks new mutating work in the same process as well as after restart,
not just replay of the old request. Distinguish the exact currently owned active
operation from unresolved prior uncertainty; do not exempt a record merely because
it names the current epoch. Status remains diagnostic and does not resolve
credentials. Corrupt, ambiguous, missing-required or oversized evidence
refuses; it is never interpreted as no outstanding operation. Enumerate records
with explicit resource bounds; exhaustion is refusal, not truncation.

Only a trusted operator may reconcile this barrier by inspecting durable phases,
current managed identities/assets and retained child evidence. No RPC clears or
automatically repairs it. Changing epoch, obtaining a fresh qualification or
creating a new request does not bypass recovery.

### SC-006 — Truthful outcomes under interruption

If shutdown is latched before the first primary service-control commitment,
interruption must preserve the original primary and leave no service-control
calls. After that commitment, including before OS submission or observed effect,
treat the outcome as uncertain unless the action is proven safely cancelled.
Preserve the last durable phase and record `manual_recovery_required` when possible;
otherwise the fence and prior records must retain the uncertainty.

Never report `restarted` or `rolled_back` without the existing full acceptance
checks. A durable consumed intent proves replay prevention, not availability.
Manual recovery may be necessary with the primary stopped or partially started.
If acceptance was durably completed before shutdown was observed, its recorded
terminal result remains a fact; shutdown does not erase that history.

### SC-007 — Generation and qualification binding

Bind an explicit shutdown-contract version and its fixed deadline/logging policy
in the qualification record, alongside existing epoch, source/policy/plist,
controller-supervision and canary-evidence bindings. The implementation plan must
freeze the exact closed schema before code is written. Missing/legacy bindings
refuse; no automatic upgrade or inferred platform qualification is permitted.

Retain the current exact loaded-field checks. A new source digest, bundle, plist
or schema is a new generation, not a reinterpretation of existing evidence.

### SC-008 — Isolated process tests

Use private temporary state, real Unix sockets/owned loopback children, and an
explicitly fake service-control backend. Parameterized reduced test clocks may
exercise timing, but production limits are fixed and separately checked. Tests
must not read credentials, use production policies/state, call real service
control, signal production processes or resolve arbitrary environment values.

Cover graceful stop while idle; queued work; preflight/candidate interruption;
shutdown before/after intent consumption and between primary commitment and OS
submission; every stop/start/postcheck and rollback boundary; concurrent requests; shutdown during helper control; lost response;
child exit/reuse uncertainty; failed durable writes; repeated signals; deadline
exhaustion; and restart with unresolved state. Assert the exact service-control
call sequence and absence of subsequent calls, not just the returned status.

Run forced controller-death cases only against retained disposable test children.
Reconstruct from disk in a new process and prove the recovery barrier, no replay,
unchanged pre-disruption primary where applicable, and no false acceptance claim.

### SC-009 — Disposable launchd canary evidence

Offline fake-backend success is not launchd evidence. A real canary uses only
operator-armed, fixed-scope disposable labels and private files derived by trusted
code; no supplied arbitrary executable, label, port, shell or environment. Refuse
any collision with an existing job or either production label. Verify UID,
parentage, loaded source/argv/environment/timeout, birth identities, and exact
canary source/plist hashes. Exercise normal KeepAlive behavior and bootout, plus
shutdown/interruption evidence relevant to this contract. Record actual results,
including refusal or incomplete cleanup, rather than issuing qualification.

Evidence admitted for production qualification must originate in the exact
intended controller context. An external-shell bootstrap or a sibling test
controller alone does not satisfy that requirement. The implementation plan must
specify and review the operator-armed in-context canary trigger before adding it;
the closed production RPC does not gain an arbitrary lifecycle endpoint.

Do not retry a refused live control operation through another tool/interpreter.
No canary may operate on the production proxy or stop/reload the existing
controller. Activation of a fresh intended production controller remains separately
authorized; isolated evidence must be labeled with its actual scope.

### SC-010 — Immutable delivery and owner gates

Do not overwrite the running bundle, rewrite its live plist, relax the lifecycle
guard, issue qualification/approval, or restart/handoff the proxy in this slice.
Stage any later generation in a fresh directory with fresh bindings; update the
existing closed code-input registry for any added helper. Never delete running
state or auto-migrate outstanding requests. Design approval, plan/execution-method
approval, implementation/test verification, live canary permission, generation
activation and production handoff are distinct evidence/authorization steps.

## Acceptance and review boundaries

The implementation must provide deterministic negative tests for every admitted
binding and interruption boundary, a full-suite regression run, isolated real
process/socket tests, and mutation tests proving the shutdown/recovery gates can
fail for their intended reasons. Independent review must challenge admission
races, ownership loss, durable write ordering, stale-generation acceptance and
mislabeling fixture evidence as live qualification.

This amendment does not claim successful availability recovery after interruption
or impose a promise that 900 seconds of work fits into 60 seconds. It replaces
that assumption with bounded quiescence and explicit uncertain-state handling.

**Current deliverable:** owner-approved written amendment only. No implementation tests
or live canary results are claimed for this proposed behavior. Existing deployed
behavior remains governed by the current contract and qualification refusal until
a separately reviewed generation implements and proves the amendment.
