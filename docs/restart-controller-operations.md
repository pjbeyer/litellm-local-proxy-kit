# Restart controller operations (macOS)

**Release posture: validation draft, not production-qualified.** A follow-up
review found an unresolved release blocker: qualification does not yet compare
the loaded controller job's arguments/plist/supervision with the generated
controller definition. PID/birth agreement alone is insufficient for release.
Until fixed, do not activate this draft or issue production qualification/approval
records. The correction must be authorized, implemented and independently verified.
The executable contract is [restart-controller.md](restart-controller.md). Installation does not
activate a job, qualify a platform, grant approval, or take over a bridge.
This is accident prevention for trusted same-user automation, not OS isolation.

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
controller execution context**. This release does not provide a canary runner or
an automatic qualification issuer. Fixture tests and a successful external-shell
bootstrap cannot substitute for that evidence.

The operator-issued `qualification.json` is a 0600 regular, non-symlink file under
`state_dir`, with exactly the fields checked by `Runtime.qualify`:

- `version`, `operator_uid`, `controller_label`, `controller_identity`.
- `controller_digest`, `policy_digest`, `plist_digest`, `controller_epoch`.
- `issued_at`, `expires_at`, `issued_monotonic`, `expires_monotonic`.
- `canary_evidence_digest`: SHA-256 of the reviewed real canary evidence.

Bind the current daemon epoch, exact source/policy/proxy-plist digests, dynamic
UID and kernel PID/microsecond birth identity. Both lifetimes are at most 24
hours. The controller must have PPID 1 and match its loaded launchd label's
PID/birth. Missing or unverifiable qualification keeps restart disabled.
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

## Failure handling

- `runtime_invalid`: preserve runtime/cache evidence. Verify the deployed pin,
  tracked blobs, distribution mapping and active-tag bytecode. Do not clear or
  rebuild caches automatically to make the gate green.
- `unmanaged_listener`/`primary_changed`: preserve the listener. Reconcile birth,
  label, loaded policy and trusted bootstrap receipt; never kill by name or port.
- `candidate_cleanup_failed`: stop work and reconcile the retained owned child
  and process group. There is no SIGKILL escalation or arbitrary cleanup command.
- `service_control_denied`/`service_control_timeout`: no further control calls;
  reconcile partial state manually. Normal launchd supervision may still retry.
- `manual_recovery_required`: inspect the last durable phase and reported failed
  phase. Never assume the original process still serves or repeat consumed intent.
- `rolled_back`: current-generation LKG configuration was restored and accepted;
  this is not recovery of a previous software release or DB/cache/spend state.

## Verification boundary

Run the complete pytest suite with `TMPDIR` pointing to the approved scratch
root. Tests label simulated launchctl results as fixtures while using genuine
HTTP children, kernel process observations and Unix sockets. Installed-layout
status tests execute the copied isolated Python entrypoint without launchctl.
These prove implementation behaviour, not live launchd qualification or handoff.
Production restart and bridge shutdown require separate fresh owner approval.
