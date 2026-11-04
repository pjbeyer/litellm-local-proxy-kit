# Restart controller implementation contract

Addendum to [safe restart requirements](safe-restart-contract.md), not a new
Spec Kit workspace or a replacement for the existing deployment source of truth.
This is a same-user operational controller under the explicitly selected bounded
command guard, not an OS isolation or credential-confidentiality boundary.

## Scope and current authorization

Implement and exercise the controller, candidate acceptance and rollback now.
Do not perform a production restart or stop the session-owned bridge. Production
handoff requires a fresh explicit owner approval after reviewing the exact plan.
A controller must refuse a session-owned/unmanaged listener; the first bridge-to-
managed-service handoff is operator-owned, not permission to signal that bridge.
There is no raw-signal primary shutdown path, SIGKILL fallback, automatic bridge,
or policy-disable operation. An independently supervised service is required for
production use; a Hermes-owned background process does not satisfy that gate.

## Closed interface

- CT-001: A local Unix-domain JSON-line service exposes only `status`, `prepare`
  and `restart`. Clients never supply shell, executable, label, port, file path,
  environment, credentials or an approval boolean. The service loads a fixed,
  operator-owned local policy with the deployment root, kit root, installed plist,
  state directory, candidate port and approved direct/router model names.
  Parse standard JSON only: reject malformed input, duplicate object keys,
  NaN/Infinity, non-object envelopes, unknown fields and wrong types before any
  state change. Exact requests are `{"op":"status"}`, `{"op":"prepare"}` and
  `{"op":"restart","request_id":"<32 lowercase hex>","asset_digest":"<64 lowercase hex>"}`.
  No extra fields or coercion are accepted; all other identifiers come from
  controller-owned state. Return a fixed `invalid_request` result on refusal.
- CT-002: Private state/socket directories are owned by the current UID and 0700;
  sockets are 0600. Reject foreign peers when the platform supplies peer-UID
  inspection. This is not protection from other same-user clients. Refuse an
  existing socket rather than unlinking unknown state. Bound request bytes,
  connection reads, operation time and response size. No TCP control listener.
- CT-003: Only one mutating operation runs at a time (nonblocking cross-process
  file lock). The controller generates request IDs. Persist phase transitions
  durably before mutations: flush and fsync the new record, atomically replace,
  and fsync the containing directory (use macOS full-sync when available).
  Intent consumption is durably committed too. A flush/sync error blocks service
  control; uncertain recovery is manual, never a guessed earlier safe phase.
  A duplicate/replayed request never repeats service
  control; interrupted/unknown outcomes require operator reconciliation, not retry.
  Status must not resolve secrets, start a candidate or mutate the proxy.

## Prepare and identity binding

- CT-004: Derive UID dynamically. Read listener PID plus birth identity; compare
  it with the exact installed launchd label's current PID, and require loaded
  plist source/environment/argv and KeepAlive/ThrottleInterval parity with the
  generated installed plist. Since human-readable launchctl output does not
  expose every loaded supervision field, require a matching bootstrap receipt:
  UID/label/listener birth, exact plist digest and supervision dictionary, issued
  by the trusted operator for initial activation or by this controller after its
  own verified bootstrap. Compare available live fields and reject absent,
  mismatched or unverifiable receipt/policy; do not infer loaded policy from a
  mutable file path alone. A bridge may be inspected and rehearsed without
  disruption, but cannot produce a restart-ready plan. Wrong UID,
  absent/unknown job or ambiguous identity blocks.
- CT-005: Reuse installer `--verify`, drift, installed wrapper `--preflight`, pin
  and patch-sentinel gates. Construct a clean environment from the installed
  generated plist plus HOME/PATH/TMPDIR; never inherit caller credentials,
  PYTHONPATH/PYTHONHOME or OP_SERVICE_ACCOUNT_TOKEN. Capture helper output in
  memory and return only closed error codes, not exception text or subprocess
  output. Resolve the HTTP acceptance master key from its manifest source into
  memory only; never read a whole process environment or persist resolved keys.
  Manifest validation must reject repeated keys (including top-level keys) and
  duplicate credential `env_var` rows before any resolution. Each credential has
  one unambiguous source and is resolved once per wrapper/resolver invocation.
- CT-006: Hash the exact managed assets, installed/auto-load plist copies,
  deployment inputs, relevant kit renderer/helper sources and pinned runtime
  identity and the canonical controller-policy digest. Use the closed input
  registry below; a changed, added, missing or unregistered executable helper
  invalidates the plan rather than silently falling outside the digest. Use
  structured canonical encoding, not delimiter joins. Runtime identity includes
  the resolved interpreter
  hash, imported package root and distribution metadata, verified Git HEAD, and
  actual tracked package-file blob hashes compared with that HEAD. Refuse any
  untracked or ignored Python source/native extension under that package; every
  active-tag bytecode file must map to a verified tracked source and its code
  object must equal a fresh same-interpreter compilation (unknown cache forms
  block). Reject unverifiable caches rather than clearing them automatically.
  A clean `git diff` or existing version/tip check alone is insufficient.
  Runtime identity is anchored to the existing deployed `pin.lock`: imported
  LiteLLM distribution version must equal `base_version`, full Git HEAD must
  match its unchanged `patch_tip` prefix, and the imported package path must map
  into that pinned tree. Compare package blobs with that verified revision, not
  an arbitrary locally chosen HEAD; unverifiable source/distribution mapping
  blocks. Runtime identity covers the pinned LiteLLM package, interpreter and
  distribution metadata, not a new claim to attest every transitive module or
  process memory.
  The identical runtime digest binds candidate, LKG, approval and rollback;
  unverifiable/mutated runtime files block. Recheck all
  digests and listener birth identity after preflight/candidate work and immediately
  before service control. Policy or asset changes invalidate plan and approval.
- CT-007: Candidate startup uses the actual installed wrapper, clean launch
  environment and installed configuration on a different loopback port. Run
  liveness/readiness, deliberately invalid-key denial (401, never DB-error 400),
  authenticated nonempty models, and small real direct/router completions.
  Cold-start budget is at least 180 seconds; model calls are bounded separately.
  Disable HTTP redirects and ambient proxy settings. Construct each request URL
  from the fixed literal loopback address and approved port; refuse redirects or
  any destination mismatch without forwarding authentication. Report MCP/embedding
  parity separately, not as established by model acceptance.
  Candidate DB/cache/spend state is shared, not isolated or reverted.
- CT-008: Only an owned candidate child may be shut down with bounded graceful
  SIGTERM, using its retained Popen/process-group identity. No SIGKILL escalation
  or name/port-based cleanup. If owned cleanup cannot finish, block further work
  and require operator cleanup. Prove the primary listener survived every failed
  prepare. Keep no raw candidate/request logs or credential-bearing environment.
- CT-009: Freeze an exact last-known-good managed-asset snapshot and build
  identity, only after the running primary passes real auth/model acceptance.
  Candidate and LKG acceptance have separately recorded asset/runtime digest
  bindings. This first controller restarts the current installed generation; it
  does not apply upgrades. Both bindings must therefore equal that same frozen
  generation; differing generations are unsupported and block. LKG means the
  healthy pre-restart configuration, not a claimed previous software release.
  Approval includes both binding records, and rollback verifies the LKG binding.
  Never claim a previous build can be recovered from a mutable/shared worktree;
  rollback must refuse runtime drift. A rollback restores owned configuration
  bytes, not DB/cache/spend state or external credential-service state.

## Separate owner gate and disruption

- CT-010: A prepared result contains request ID, asset digest, listener birth
  identity, validation outcomes, preparation time and an expiry at most five
  minutes after preparation completes. Restart rejects expired plans; approval
  must be issued after preparation, no later than current time, have a maximum
  five-minute lifetime, and expire no later than the plan. Time values must be
  finite numbers, not booleans; expiry is strictly later than issuance. Bind
  plan, approval and qualification to a fresh controller-start epoch (which
  invalidates prior outstanding intent after controller restart or OS reboot)
  and same-boot monotonic issuance/deadlines. Enforce monotonic lifetime caps as
  well as wall-time checks; detected wall-clock regression invalidates outstanding
  plans and approvals. An expired approval cannot become fresh after a clock
  rollback. The service must be independently supervised and operator-qualified
  for launchd control before restart is enabled.
  An operator-only approval record, created after separate explicit owner consent,
  binds all of request ID, UID, label, asset/runtime/policy digests, listener
  identity, controller source digest, issued time and bounded expiry. RPC cannot create approvals. No approval
  is issued by this implementation session. Qualification is a separate
  operator-issued 0600 regular, non-symlink record under the private state root:
  version, operator UID, controller label/PID/birth, controller/policy/proxy-plist
  digests, issued/expiry times (at most 24 hours), and a digest of the operator's
  real controller-context canary bootstrap/KeepAlive/bootout evidence. Require
  current UID, exact bindings, nonfuture issuance, unexpired record, controller
  PPID 1 and a launchd controller-label PID/birth match. Missing/stale/unverifiable
  qualification blocks; no RPC can create it. The operator is the trusted issuer
  of that operational evidence, not a new cryptographic attestation service.
  Qualification never substitutes for per-request approval. These records prevent
  accidents and stale/replayed intent; same-user forgery remains accepted risk.
- CT-011: Before any disruption: verify approval, freshness, supervision/platform
  qualification, full frozen digest, unchanged listener/job identity, LKG snapshot
  and readiness/auth of the current primary. Consume intent and persist the
  operation phase before calling launchctl. A crash never silently retries it.
- CT-012: Service control uses only the fixed `/bin/launchctl bootout gui/UID/LABEL`
  then `/bin/launchctl bootstrap gui/UID INSTALLED_PLIST` sequence in the qualified
  controller's own execution context, with bounded graceful exit and a cooldown
  of at least six seconds. No shell, alternate verb, retry through a different
  interpreter, signal to the primary or evasion of a Hermes/OS/EDR refusal.
  Any launchctl refusal stops all further service-control calls immediately and
  reports blocked/manual recovery, including partial-state facts. The no-retry
  rule applies to controller service-control sequences, not an assertion that
  launchd KeepAlive cannot relaunch a job. KeepAlive/ThrottleInterval are part of
  the frozen plist generation and are never changed by this controller. During
  acceptance, inspect job PID/birth and launchd `runs` on each readiness poll and
  before/after every HTTP request (each request at most 15 seconds). Require the
  first bootstrap incarnation (`runs == 1`) and one stable identity
  once observed. Initially, `runs == 0` with no PID is pending only until the
  bounded cold-start deadline; after first observation, a missing PID or zero
  run count is failure. An exit, respawn or second run fails acceptance and enters
  the single rollback path. Its bootout removes the retrying job; a bootout refusal stops
  control and reports that normal supervision may still retry until the operator
  reconciles it. Verify loaded normal-supervision policy unchanged after success
  or rollback. Do not claim prevention of all launchd relaunches.
- CT-013: After successful bootstrap, bound readiness/auth/model acceptance and
  verify the new launchd/listener identity agrees. Success means managed service
  acceptance, not merely a zero bootstrap exit status or an HTTP health endpoint.
- CT-014: If service control succeeded but post-start acceptance fails, attempt
  exactly one tested rollback through the same fixed service-control path with
  the verified LKG configuration/runtime identity. Any refusal ends the sequence;
  do not try another verb. Return `rolled_back` only after real acceptance passes;
  otherwise `manual_recovery_required`. Never automatically retry a failed restart.

## Delivery and verification

- CT-015: Keep local runtime metadata/paths out of shared sources and receipts
  free of credentials, subprocess output, prompts, response bodies and tool args.
  Record controller digest, phase, request identity, asset digest, check results,
  before/after process identities, and explicit fixture versus production scope.
- CT-016: Use genuine loopback child processes/Unix sockets for offline lifecycle
  tests. A clearly labelled fake launchctl backend may inject platform failures;
  never present that as live launchd or LiteLLM acceptance. Cover missing relay/
  vault, wrong UID, absent daemon, duplicate request, stale digest/approval,
  unknown listener, failed candidate, refusal at each service-control phase,
  failed post-check, verified rollback, failed rollback and successful restart.
  Run the real installed preflight/candidate path without primary disruption.
- CT-017: Supply a portable reviewed launchd guide and an installer that writes
  only controller-owned files; it must never load/unload jobs. Activation and
  platform qualification are explicit operator gates. The production service
  remains protected by the existing command guard; never add an exemption that
  permits general-purpose model tools to restart/stop it directly.

## Closed CT-006 input registry

All entries are required unless explicitly marked optional. Hash logical names
plus bytes/modes in one canonical map; never silently skip missing files.

Kit source paths:

- `core/installer.sh`
- `core/drift_check.py`
- `core/render_config.py`
- `core/install_launchagent.py`
- `core/parse_credentials.py`
- `core/check_credential_coverage.py`
- `core/check_patch_sentinel.py`
- `core/check_pin.py`
- `core/run-proxy-wrapper.sh`
- `core/aws-sso-cred-shim.sh`
- `core/rotate-logs.sh`
- `core/harvest-filter-metrics.sh`
- `core/config.template.header.txt`
- `launchd/proxy.plist.tmpl`
- `launchd/proxy.rotate.plist.tmpl`
- `core/restart_controller.py`
- `core/install_restart_controller.py`
- `core/restart_control/__init__.py`
- `core/restart_control/engine.py`
- `core/restart_control/wire.py`
- `core/restart_control/runtime.py`
- `core/restart_control/runtime_identity.py`
- `core/restart_control/process_identity.py`

Reject additional executable/source helpers under `core/` (excluding bytecode
cache directories); adding one requires updating this registry and review.
The installed controller entrypoint/package must byte-match those source paths.

Installed proxy paths, relative to the verified config directory:

- `config.rendered.yaml`
- `run-proxy-wrapper.sh`
- `parse_credentials.py`
- `credentials.toml`
- `pin.lock`
- `check_patch_sentinel.py`
- `check_pin.py`
- `check_credential_coverage.py`
- `aws-sso-cred-shim.sh`
- `rotate-logs.sh`
- `harvest-filter-metrics.sh`
- `<verified-label>.plist`
- `<verified-label>.rotate.plist`

Also hash the separate auto-load plist copy, deployment `deploy.conf.sh`, selected
`deployments/<verified-deployment>/overlay.yaml`, `credentials.toml`, `pin.lock`,
controller policy bytes, and optional deployment `drift-exemptions.yaml` (absence
is an explicit value so later addition invalidates the digest). The runtime
identity inputs are specified by CT-006. Snapshot/qualification/approval records
are separately bound and validated, not recursive members of their own digests.
Fixed platform executables and external Python dependencies are trusted platform
inputs, not a claim of newly verified transitive provenance.

## Gate classifications

Owner approval, production interruption, platform qualification and restoration
are always-gated because they affect availability/security. Read-only inspection,
offline fixture tests and bounded prepare are automated; prepare still records
its real inference and shared-state side effects. A mocked success is not a live
handoff, supervision or platform-qualification receipt.
