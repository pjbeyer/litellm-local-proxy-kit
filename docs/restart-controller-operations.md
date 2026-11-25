# Restart controller operations (macOS)

**Release posture: validation draft, not production-qualified.** Qualification
checks the generated controller plist and available loaded executable, arguments,
source path, working directory, environment and throttle settings, not just
PID/birth agreement. Operator evidence separately binds supervision fields that
launchctl cannot expose. Source verification is not live platform qualification:
activation, evidence issuance and production interruption remain operator gates.
The executable contract is [restart-controller.md](restart-controller.md). Installation does not
activate a job, qualify a platform, grant approval, or take over a bridge.
This is accident prevention for trusted same-user automation, not OS isolation.

The [cap-aware shutdown amendment](restart-controller-shutdown-contract.md) is
now **offline-implemented** (plan T001–T025 executed and test-verified offline),
but it is still **not deployed and not platform-qualified**: no live canary has
been run, no controller generation has been activated, and no qualification has
been issued. Behavior proven by the offline suite must not be read as evidence
about this host's live launchd behavior. Live activation, the isolated-canary
lane and production handoff remain separate operator gates.

## Existing deployment: before you prepare

Start with [routine maintenance](macos-managed-service.md#routine-maintenance)
for source changes and staging. Then use `status` on the installed, policy-bound
controller; confirm it is idle and that the trusted qualification matches the
installed generation and current daemon incarnation. `status` alone is not a
qualification check. If the policy or installed generation changed, follow the
applicable owner activation and qualification gates below. If evidence expired,
follow the installed generation's qualification contract; never reuse a dated
issuer or another incarnation's canary evidence.

When those gates are satisfied, use the [closed client interface](#closed-client-interface).
Allow time for its real candidate checks. Only the owner may approve the exact
prepared plan and submit the restart before the deadlines. On refusal or
uncertain progress, preserve the running listener and recovery state; use
[failure handling](#failure-handling), not an alternate control command.

## Provision and install

Use the pinned proxy Python with PyYAML. Provision an operator-owned policy JSON
file with mode 0600 under a private 0700 directory. Supply exactly these fields:

- `version`: integer `1`.
- `kit_root`, `deployment_root`: absolute source checkout paths.
- `plist`: installed proxy plist in its configuration directory.
- `auto_plist`: matching auto-load proxy plist in the user's LaunchAgents directory.
- `state_dir`: dedicated private controller state directory.
- `scratch_dir`: dedicated private controller scratch directory.
- `controller_root`: new, dedicated installation directory for controller code.
- `controller_label`: a new launchd label, different from the proxy label.
- `candidate_port`: a separate available loopback port, integer 1024–65535.
- `models`: exactly two distinct approved names: a direct model and the router.

No credentials belong in this policy or either service plist. Resolve XDG paths
from the operator's environment: persistent code under `$XDG_DATA_HOME/litellm/`,
state under `$XDG_STATE_HOME/litellm/`, and scratch under the approved cache root.
The Unix socket pathname must fit the platform's Unix-domain pathname limit.
Create necessary parent directories deliberately; existing directories must not
be symlinks or owned by another user. State and socket parents are 0700.

From the kit checkout, with `POLICY` set to that private policy path:

```sh
python3 core/install_restart_controller.py --policy "$POLICY"
python3 core/install_restart_controller.py --policy "$POLICY" --verify
```

The installer copies a byte/mode-matched controller bundle and writes only its new
controller LaunchAgent plist and private installation receipt. It refuses to
overwrite an existing bundle or plist. It does not call launchctl, replace proxy
assets, install approval/qualification records, or modify the command guard.
`--verify` compares installed bytes; it is not platform qualification.
Updating an installed bundle requires a separately reviewed operator procedure;
do not delete a running controller's state or overwrite it in place.

## Activation and platform qualification — operator only

The owner must review the generated controller plist and activate it outside a
model tool context that refuses service control. A refusal is not permission to
try another launch mechanism, signal, interpreter or policy exemption. A
Hermes-owned background daemon is not independently supervised.

Before restart can be enabled, the trusted operator must obtain real evidence
of canary bootstrap, normal KeepAlive behaviour and bootout **from the intended
controller execution context**. Use the [startup canary lane](#startup-canary-arm-and-worker-boundaries).
It produces evidence, not authorization. Fixture tests and a successful
external-shell bootstrap cannot substitute for that evidence. The owner
issues `qualification.json` using the [v2 schema](#qualification-schema-v2);
there is no automatic issuer.

Bind the current daemon epoch, exact source/policy/proxy-plist digests, dynamic
UID and kernel PID/microsecond birth identity. Both lifetimes are at most 24
hours. The controller must have PPID 1 and match its loaded launchd label's
PID/birth. The controller's unique top-level loaded `exit timeout` must also
exactly match the generated `ExitTimeOut`; missing, duplicate, nested-only,
malformed or clamped observations refuse. A configured long timeout is not
proof that the platform supplies that shutdown grace period. Do not lower the
expected timeout merely to qualify a host while request/drain bounds remain
longer. Extra explicit environment remains rejected except the matching
`XPC_SERVICE_NAME`; unexplained platform logging settings are not silently
whitelisted. The controller plist must be an owned 0600 regular non-symlink file
matching the definition shared by installer and verifier. Available loaded fields
must agree; only launchd's matching `XPC_SERVICE_NAME` is allowed as additional
explicit environment metadata. The operator's real canary evidence binds the
unexposed supervision fields to this loaded generation. Missing, stale or legacy
qualification records without the new bindings keep restart disabled; they are
never automatically upgraded.
Do not manufacture qualification from a design review or mocked success.

Initial proxy activation also needs an operator-issued `bootstrap.json` binding
version 1, UID, proxy label, listener PID/UID/birth, exact proxy-plist SHA-256 and
the `KeepAlive`/`ThrottleInterval`/`RunAtLoad` dictionary. Compare actual loaded
argv, environment and available supervision fields. The controller writes this
receipt only after its own subsequent successful bootstrap and acceptance.
A session-owned bridge cannot produce a restart-ready plan: its first managed
handoff remains a separate, explicitly authorized operator operation.

## Closed client interface

Use the installed entrypoint with the same private policy:

```sh
python3 "$CONTROLLER_ROOT/restart_controller.py" --policy "$POLICY" status
python3 "$CONTROLLER_ROOT/restart_controller.py" --policy "$POLICY" prepare
```

Status does not resolve credentials or contact the proxy. Prepare executes
installer verification, drift and installed preflight, then checks real primary
and candidate health/auth/models/direct/router traffic. Candidate inference,
DB/cache/spend side effects are real and shared, not isolated or reverted.
MCP/embedding parity remains separately unestablished. No raw response bodies,
prompts, resolved credentials or subprocess output belong in receipts.

A prepared plan expires within five minutes. After **fresh explicit owner
consent for that exact plan**, a trusted operator may create the corresponding
0600 `approvals/<request_id>.json`. Its schema is `approval_binding(plan)` plus
`version: 1`, `owner_uid`, and wall/monotonic issuance and expiry. All bindings
must match exactly; issuance follows preparation, neither lifetime exceeds five
minutes, and neither deadline exceeds the plan. RPC cannot create approval.
Do not pre-issue approvals or treat approval as qualification.

Only then may the fixed client submit the exact request ID and asset digest:

```sh
python3 "$CONTROLLER_ROOT/restart_controller.py" --policy "$POLICY" restart "$REQUEST_ID" "$ASSET_DIGEST"
```

The controller durably consumes intent before any service control. Replays,
restarts of the controller, clock regression and source/policy/runtime drift
invalidate outstanding intent. Do not retry an interrupted or consumed request.
A new prepare is not permission to ignore an uncertain previous operation.

## Runtime cache and native-artifact evidence

The managed installer and proxy wrapper force `PYTHONDONTWRITEBYTECODE=1` before
Python helpers or proxy execution. This prevents those entrypoints from rewriting
the frozen cache evidence; it neither repairs existing caches nor disables their
verification. Preserve and reconcile pre-existing mismatches separately.

The [native runtime artifact contract](native-runtime-artifact-contract.md)
defines the one supported optional native artifact and its required pin and
build receipt. An editable build or matching local Cargo output is not proof
of provenance. Keep the pin and `native-build.json` paired; do not whitelist
an observed hash or remove native code to force a pass.

## Failure handling

- `runtime_invalid`: preserve runtime/cache evidence. Verify the deployed pin,
  tracked blobs, distribution mapping and active-tag bytecode. Do not clear or
  rebuild caches automatically to make the gate green.
- `unmanaged_listener`/`ambiguous_listener`/`primary_changed`: preserve the
  listener. Reconcile birth,
  label, loaded policy and trusted bootstrap receipt; never kill by name or port.
- `candidate_cleanup_failed`: stop work and reconcile the retained owned child
  and process group. There is no SIGKILL escalation or arbitrary cleanup command.
- `service_control_denied`/`service_control_timeout`: no further control calls;
  reconcile partial state manually. Normal launchd supervision may still retry.
- `manual_recovery_required`: inspect the last durable phase and reported failed
  phase. Never assume the original process still serves or repeat consumed intent.
- `rolled_back`: current-generation LKG configuration was restored and accepted;
  this is not recovery of a previous software release or DB/cache/spend state.
- `controller_stopping`: the shutdown latch is set; work admission and further
  primary control are refused until restart. This is a clean stop, not a failure.
- `recovery_invalid`: the durable journal/namespace state is inconsistent or a
  consumed canary scope lacks coherent terminal evidence. Preserve everything;
  reconcile per "Recovery reconciliation" below. Never delete state to clear it.
- `canary_*` (`canary_binding_mismatch`, `canary_arm_expired`,
  `canary_identity_invalid`, `canary_first_load_failed`,
  `canary_exit_failed`, `canary_keepalive_failed`, `canary_cleanup_failed`,
  `canary_shutdown_receipt_missing`, `canary_shutdown_unresolved`,
  `canary_job_absent`): the startup canary lane refused; the scope retains
  its arm, receipts and a durable `diagnostic.json`. Read the diagnostic,
  reconcile the scope (any live canary worker is
  `local.restart-canary.<nonce>` under launchd, never the proxy), then
  re-arm once the cause is fixed. A refused lane never emits evidence.

## Startup canary: arm and worker boundaries

The amendment adds an operator-armed startup canary lane, run only inside a
normal `serve` after runtime/identity verification and before RPC admission:

- `arm-canary` (operator CLI, no service control, no credentials): writes one
  0600 `canary/intent.json` envelope binding the CURRENT policy/controller
  digests, generated controller plist digest, kernel boot identity and a
  300-second dual-clock lifetime. One arm at a time; any pending or consumed
  uncertainty refuses. The nonce derives the disposable label
  `local.restart-canary.<nonce>` and the private scope
  `state_dir/canary/<nonce>/`.
- On the next `serve`, the arm is consumed (atomic rename into its scope) and
  the lane runs the bounded sequence: derived canary plist → bootstrap →
  first incarnation (runs 1) → cooperative exit-marker exit → KeepAlive second
  distinct birth (runs 2) → bootout → proven shutdown receipt → proven
  absence. Evidence and detail records are written only after successful
  cleanup; every step is fixed-argv launchctl through the tracked runner.
- Exactly two worker incarnations exist per scope; a third refuses. Workers
  run with no Runtime, no HTTP, no credentials, and die cooperatively through
  the shared latch within the 50-second budget — never signal escalation.
- No arm means zero canary activity. A blocked lane leaves the journal blocked,
  the scope retained and `status` read-only; the serve process keeps running.

## Qualification schema v2

The owner issues `qualification.json` as an owned, regular, non-symlink 0600
file under `state_dir`. [The runtime validator](../core/restart_control/runtime.py)
requires exactly these fields:

- `version` (integer `2`), `operator_uid`, `controller_label`,
  `controller_epoch`, and `controller_identity` (`pid`, `uid`, `ppid`, `pgid`,
  `birth`) for the intended live controller.
- `controller_digest`, `policy_digest`, `plist_digest` (proxy plist), and
  `controller_plist_digest` (exact generated controller plist).
- `controller_supervision` with `KeepAlive`, `ThrottleInterval`, `RunAtLoad`,
  and `ExitTimeOut` from the generated controller definition, and
  `shutdown_contract` matching `SHUTDOWN_CONTRACT` in the
  [pinned controller source](../core/restart_control/shutdown.py).
- `issued_at`, `expires_at`, `issued_monotonic`, `expires_monotonic`, and
  `canary_evidence_digest`.

The evidence digest binds `state_dir/canary/evidence/<digest>.json`, an owned
0600 file. Its identity, epoch, digests, supervision and contract must match the
qualification, with two distinct worker births and all producer checks true.
Use real evidence for a live controller, not fixtures. Legacy v1 qualification
records refuse. Evidence does not issue qualification or authorize activation.

## Recovery reconciliation

`recovery_invalid` and consumed canary scopes are reconciled, never cleaned
silently. The bounded state root (`state_dir`) carries: requests, approvals
(including `.consumed` markers), the recovery journal (operations, spawns,
fence), and `canary/` (arms, scopes, evidence, detail). Reconciliation rules:

1. Every pending/prepared request needs an explicit operator disposition:
   expire it (wait past the deadline), or complete it through the fixed
   client after fresh approval. Never delete request or approval records.
2. A retained operation/fence row (uncertain outcome) means an interrupted
   mutation: inspect the last durable phase and reconcile the primary
   manually. The fence is cleared only by a completed `finish` or a
   documented manual recovery; never by unlinking `mutation.json`.
3. A consumed canary scope without coherent terminal evidence (omitted
   detail, mismatched boot/generation, sibling evidence) blocks a new arm
   until the scope is reconciled: capture the diagnostic, decide disposition
   per the canary boundaries above, and archive the scope with the operator's
   recorded decision. No automatic root-switch or silent archive.
4. Spawn/child rows: a closed row is history; an owned row means a live child
   claimed by the journal — reconcile by exact kernel identity
   (pid + microsecond birth + uid), never by name or port.

## Predecessor-state audit and activation gates

Before selecting a fresh state root or arming the first canary of a new
generation, produce the bounded hash-pinned predecessor-state inventory
(plan T026): every pending/prepared request, consumed approval, operation/
fence row, spawn/child record and consumed canary scope, each with a
disposition and the operator's reconciliation decision. Unknown, truncated or
unreconciled state blocks packet readiness — no automatic root-switch.

Activation is a sequence of separate owner gates, in order: (1) offline
acceptance (T025) — complete suite, mutation battery, reviews, all green;
(2) the predecessor-state audit above; (3) the immutable candidate-generation
activation packet (reviewed digests, bounded arm lifetime, derived scope,
expected observations) — preparation evidence only, never execution; (4) the
owner's explicit authorization of the real isolated-canary lane; (5) only
then, the owner's authorization of the intended controller generation.
Skipping a gate or bundling authorizations is not permitted; a refusal at
any gate stops the sequence without a fallback mechanism.


## Verification boundary

Run the complete pytest suite with `TMPDIR` pointing to the approved scratch
root. Tests label simulated launchctl results as fixtures while using genuine
HTTP children, kernel process observations and Unix sockets. Installed-layout
status tests execute the copied isolated Python entrypoint without launchctl.
These prove implementation behaviour, not live launchd qualification or handoff.
Production restart and bridge shutdown require separate fresh owner approval.
