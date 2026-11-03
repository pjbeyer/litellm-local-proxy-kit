# Cap-aware Controller Shutdown Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task after owner approval of this plan and execution method.

**Goal:** Implement and prove the owner-approved [shutdown amendment](../restart-controller-shutdown-contract.md) without changing a running controller or proxy.

**Architecture:** Share one monotonic shutdown object and a durable recovery journal across the existing wire, engine and runtime. Preserve exact supervisor verification; add a fixed-scope, operator-armed startup canary without expanding production RPC. Reuse existing atomic/private JSON and process-identity primitives rather than a second deployment framework.

**Tech Stack:** Python stdlib, pytest/unittest, Darwin libproc, Unix sockets, disposable loopback children; launchd only in a separately permitted live-canary lane.

**Status:** Written design, implementation plan and subagent-driven execution approved. Offline implementation is in progress in an isolated worktree; per-task spec/quality reviews remain mandatory. No completed new behavior, live canary or production qualification is claimed. This is the repository's established documentation-contract workflow, not a full Spec Kit CLI run.

## Scope, source and execution gates

- Base design: `docs/restart-controller-shutdown-contract.md`, requirements SC-001, SC-002, SC-003, SC-004, SC-005, SC-006, SC-007, SC-008, SC-009 and SC-010.
- Existing sources were inspected at design commit `c68de75`. Engine currently has a long mutation lock, durable intent consumption and phase records, but no cross-request recovery barrier. Runtime creates separate-session children; old cleanup shells out to `ps`. Wire drains for 1000 seconds. CLI signal handlers only set an Event.
- Preserve the pre-existing dirty `core/render_config.py`; never stage, inspect for this work, revert or copy its contents into a commit. Record its initial byte hash privately for after-run comparison. Use a dedicated implementation worktree or isolated child worktrees; never broad-stage concurrent work.
- No active controller bundle, policy, state, installed wrapper, plist or lifecycle guard is modified in the implementation lane. All installer tests redirect HOME and targets to private fixture roots.
- Gate A: owner approves this plan and selects execution method. Recommended: fresh implementation subagent per task, then separate spec-compliance and code-quality review; parent verifies outputs and commits coherent units.
- Gate B: implementation/fixture verification and independent review. This is not live launchd qualification.
- Gate C: separately reviewed real disposable-canary permission and operator activation packet. A tool/OS/EDR refusal ends that operation; no retry via another mechanism.
- Gate D: activation of a fresh intended production controller generation and operator-issued qualification. Never overwrite the running bundle. Any predecessor state must be reconciled explicitly before selecting a new state root; changing roots cannot hide uncertainty.
- Gate E: separately issued exact-request production restart/handoff approval. Nothing in this plan grants it.

## Fixed interfaces and schemas

### Shutdown primitive: `core/restart_control/shutdown.py`

No dependency on engine/runtime; no credentials, environment knobs or I/O. Export immutable `SHUTDOWN_CONTRACT`, `ShutdownRequested`, and `Shutdown`.

```python
SHUTDOWN_CONTRACT = {
    "version": 1,
    "supervisor_exit_seconds": 60,
    "shutdown_deadline_seconds": 50,
    "logging_environment": {"OSLogRateLimit": "64"},
}
```

`Shutdown(clock=time.monotonic)` exposes `request()`, `requested`, `deadline`, `checkpoint()`, `commit(purpose)`, `claim(permit)`, `remaining(limit, cleanup=False)` and `wait(interval, cleanup=False)`. `request()` never takes the operation/submission lock and never extends the first deadline. `commit` takes a short submission lock: its successful shutdown-open read is the linearization point admitting one action, distinct from eventual OS submission. It returns an immutable one-use permit; `claim(permit)` consumes only a permit issued by the same instance, returns its bounded purpose tag, refuses reuse, and does not readmit an action or execute one. If the latch wins, commitment raises `ShutdownRequested`; if commitment wins, that action may still have an effect after the latch. Prefer safe cancellation if not submitted, but never infer cancellation from loss of its result. No permit authorizes another action, and a nested call must not accidentally consume a second permit for the same spawn.

Use Event-based wakeups and bounded polling, not a long lock held during helper I/O. The signal handler is restricted to requesting shutdown. A race where a second signal interrupts request itself must not move the deadline later. Normal-operation limits remain separate from the shared shutdown deadline; cleanup may continue inside the remaining shutdown budget, but normal action admission is closed.

**Owner-approved T001 API clarification:** `checkpoint()` is a normal-work admission guard: return normally before the latch and raise `ShutdownRequested("controller_stopping")` afterward; it is not an elapsed-time counter. Cleanup `wait(interval, cleanup=True)` genuinely sleeps and is capped by both its requested real-time interval and the remaining shared deadline. It returns false when the interval ends with budget remaining, true when the shared deadline expires; a newly observed latch may wake a pre-latch cleanup wait for recheck but is not itself deadline expiry. A frozen injected clock cannot make an interval wait unbounded or cause busy-spin on an already-set Event. Existing normal-wait admission/refusal semantics remain unchanged. Clock readings may have a finite negative origin; booleans/nonfinite values and observed backward progression fail closed. Limits/intervals remain finite, strictly typed and nonnegative (wait intervals positive). True same-thread reentry while state/wakeup is changing must be tested, not just sequential post-latch calls.

### Recovery journal: `core/restart_control/recovery.py`

Keep engine's existing public `Fault`, `_read_json`, `atomic_json`, private-path and sync functions as the authoritative implementations. Avoid an engine/recovery import cycle: `RecoveryStore` takes those trusted read/write/private/sync callbacks from its constructor and raises its own closed `RecoveryError`, mapped to `Fault` by the caller. No duplicate JSON parser or atomic writer.

Fixed directories under the existing controller state root:

- `recovery/operations/<request-id>.json`: one closed operation record.
- `recovery/spawns/<spawn-id>.json`: one closed child intent/identity record.
- `recovery/mutation.json`: at most one primary-mutation fence.
- `canary/`: separately bounded operator intent and canary evidence, described below.

Each record is owned, single-link, regular, non-symlink 0600; parents are owned non-symlink 0700. Validate exact field sets, types, enums and ID/digest formats; booleans are not integers and nonfinite numbers are invalid. Use the existing 65536-byte maximum JSON read. Scan at most 1024 directory entries across operation, approval/request, spawn and all fixed canary views, including root intent, consumed nonce scopes, incarnation/worker receipts, detail/evidence and partial atomic-write files, and at most 64 spawn records per operation. Exceeding a bound, unknown entries, partial atomic-write files, symlinks or inconsistent cross-record bindings is refusal, not truncation. Do not recursively follow snapshot trees or discard old records automatically.

Exact common binding keys:

```json
{"request_id":"<32hex>","kind":"proxy","controller_epoch":"<32hex>","policy_digest":"<64hex>","controller_digest":"<64hex>","controller_plist_digest":"<64hex>","asset_digest":null,"runtime_digest":null}
```

Binding `kind` is exactly `proxy` or `platform-canary`; the generated controller plist digest is always required. Proxy binding permits paired null asset/runtime values only for pre-freeze non-control purposes. Platform-canary binding uses paired null asset/runtime values deliberately: it binds exact controller source, policy and generated plist instead of a proxy runtime proof. A platform-canary handle can admit only canary-query/bootstrap/bootout or their bounded cleanup, never candidate, credential or primary-control purposes. Fill both digests once freeze succeeds; never reuse null bindings for candidate execution or primary mutation. Source/policy binding can be derived from already-loaded source/file facts without spawning the runtime proof. All updates are checked against the exact active operation handle, not merely its epoch.

Closed operation record keys: `version` (1), `binding`, `scope` (`fixture` or `production`), `phase`, `outcome`, `primary_control_committed` (bool). `phase` is the closed set `preparing`, `prepared`, `blocked`, `restart_pending`, `intent_consumed`, `stopping`, `starting`, `postchecking`, `restarted`, `rollback_stopping`, `rollback_restoring`, `rollback_starting`, `rollback_postchecking`, `rolled_back`, `shutdown_pending`, `manual_recovery_required`, `canary_pending`, `canary_running`, `canary_passed`, `canary_blocked`. `outcome` is null or one of `prepared`, `blocked`, `restarted`, `rolled_back`, `manual_recovery_required`, `canary_passed`, `canary_blocked`. Prepared closes the prepare stage, not the request lifetime. Under the mutation lock, reopen the exact prepared operation durably as restart_pending/outcome=null before consuming its still-unconsumed matching approval. Validate current plan/approval/frozen bindings first; sync the mutation fence and approval consumption before any primary commitment. Crashing anywhere in reopening/consumption is recovery-required, even if no control occurred; prepared must never mask consumed intent or restart_pending. No consumed/manual/accepted restart record reopens. The validator permits only explicitly enumerated active write-order pairs between the index and request record; all other disagreement refuses. Preserve the original detailed plan/acceptance in the existing request record; this journal is a recovery index, not a second acceptance authority. The cross-record validator requires agreement with that record and consumed approval state.

Closed spawn record keys: `version` (1), `spawn_id`, `binding`, `purpose`, `state`, `identity`. Purpose is one of `runtime-proof`, `preflight`, `credential-resolver`, `candidate`, `diagnostic`, `service-stop`, `service-start`, `cleanup-diagnostic`, `canary-query`, `canary-bootstrap`, `canary-bootout`. State is `intent`, `owned`, `closed` or `uncertain`. Identity is null only in intent/uncertain, otherwise exact `{pid,uid,pgid,birth}` with strict integer/birth validation. Do not persist argv, raw output, keys, credential values or complete environments.

Closed fence keys: `version` (1), `binding`, `phase`, `state` (`armed` or `uncertain`). Sync the fence before the first primary-control commitment. Sync consumed intent before control too. Any uncertainty in either write blocks action; a crash does not grant another attempt. A safely cancelled pre-commit operation can leave a conservative fence requiring operator reconciliation.

`RecoveryStore` exposes `begin`, `require_clear(active=None)`, `bind_frozen`, `spawn_intent`, `spawn_owned`, `spawn_closed`, `mark_uncertain`, `arm_mutation`, `phase`, and `finish`. On every prepare/restart and before each new helper/action, require a complete bounded recovery view. A precisely owned current operation may be in progress; current epoch alone is never an exclusion. Terminal success does not erase retained child uncertainty. An unresolved fence, partial write, orphan spawn intent, consumed nonterminal request or cross-record inconsistency blocks all new mutations in the same process and after restart. Missing required evidence cannot be interpreted as no uncertainty.

Clear a mutation fence only after durable full accepted terminal result, matching journal/request/approval bindings and proven child/group cleanup. Fsync the directory after removal. A crash around clearing may conservatively require recovery but cannot authorize replay. Status remains read-only/secret-free and reports closed recovery facts; its bounded recent list does not authorize mutations. No RPC or automatically run repair clears recovery state.

### Runtime lifecycle attachment

The normal CLI creates the single Shutdown object and installs signal handlers before constructing Runtime or performing any startup work. It passes that same object to Runtime, Controller, wire and canary orchestration. Controller constructs the journal from private state and attaches it using `backend.bind_lifecycle(shutdown, recovery)`; no later component creates a replacement latch. A production Runtime must be attached before it can spawn helpers or admit work; no permissive default. Update fixture backends through one fixture adapter, not ad hoc bypasses. `Runtime.begin_operation(handle)` selects the exact active operation; `release_credentials()` still clears ephemeral credentials and ends only that operation's in-memory context, never clears uncertainty.

Every controlled step follows: recovery check; durable spawn/action intent; short admission commitment; possible child creation; identity acquisition/persistence; bounded wait; bounded ownership-checked cleanup; journal close or sticky uncertainty. Diagnostic `launchctl print`/lsof children must also be tracked and cancellable. A candidate and a bounded diagnostic may coexist; retain each identity separately rather than making diagnostic queries fail just because a candidate exists. No hidden recursive child creation during cleanup.

Replace cleanup's `ps` subprocess with bounded Darwin `proc_listpids(PROC_PGRP_ONLY=2, pgid, ...)` enumeration in `process_identity.py`. Installed SDK headers establish the signature `int proc_listpids(uint32_t,uint32_t,void*,int)` and selector 2; bind/restype/argtypes explicitly and independently test size/race/error handling. Truncation, oversized populations, permission failure or unknown group state refuses cleanup. Use genuine kernel identities; do not replace this with `killpg(...,0)` or an unowned signal probe.

### Runtime-proof observer routing

The existing proof is a process tree and its final step imports LiteLLM after verification. `_git` uses subprocess.run (whose timeout kills), and `_tool_output` explicitly calls proc.kill. T010 must contain observer ownership without moving that import into the long-lived controller or merely tracking its outer PID.

**Preserve a fresh isolated pinned-interpreter proof worker for every proof.** Create registered `core/restart_control/proof_worker.py`, invoked as a fixed script with `-I -B`; it imports no controller package before compiling the exact adjacent verified runtime_identity source bytes. The parent verifies worker/helper source bytes against frozen facts and owns the worker's Popen/PID/birth/group. The worker performs the existing source/cache/metadata/native checks and final fresh LiteLLM import; the long-lived controller never imports LiteLLM for proof. Keep the final proof object/digest identical. The worker uses a context-local tool callback instead of spawning Git/nm itself.

Reuse Runtime's tracked runner and strict JSON/bounded socket utilities with one inherited anonymous socketpair, not a public endpoint or new RPC. The child asks the parent to execute one of the exact read-only observation operations already used by the proof: package Git root, HEAD, bounded HEAD ls-tree for a relative path, native-source ls-tree, native-tree revision, native exports. No executable, shell, environment, cwd, option flags or general argv is accepted over this channel. The verified worker may nominate its canonical absolute package path for the first Git-root lookup; the parent admits only a real litellm package directory, locks the returned canonical Git root for that worker and checks its HEAD against the unchanged pin before further requests. Subsequent paths must remain under that root; native exports is exactly the pinned native path. Unknown operations/options, path escapes or request reordering refuse. This private capability trusts the exact source-bound child; it is not an OS boundary against same-user forgery.

Handshake binds version/nonce/worker identity and source digest. Requests are closed standard JSON with monotonic sequence numbers, no pipelining, <=4096 bytes and at most64 requests per worker. Responses use a fixed status/length header and <=4MiB data streamed with the same absolute deadline; no raw observer data is persisted. Worker stdout remains only the existing bounded proof result. Parent executes each observation through its own journal-attached action gate, so no observer commits after parent shutdown and every observer has its own durable intent/identity/cleanup. The worker and its single observer can coexist; do not apply a candidate-only exclusivity guard to this case. Disconnection, unknown observer state or worker cleanup failure is sticky recovery uncertainty, not a success digest.

Before any controller-package import in the installed normal entrypoint, install a fixed-root cache-free loader that compiles adjacent controller source bytes directly rather than loading .pyc. Keep normal trusted stdlib/external dependency resolution unchanged; no caller path or environment selects controller sources. This must precede use of runtime_identity registry/ABI functions, not happen after a possible poisoned-cache import. Test the real installed entrypoint with deliberately matching poisoned caches for runtime_identity and its early controller import chain. The proof worker's source-as-script bootstrap likewise compiles its helper before use. Disable bytecode writes before these imports.

The public flat standalone checker remains self-contained: move the existing kernel identity ABI primitive into the already-shipped runtime_identity.py and re-export it from process_identity.py (including compatibility aliases). Its default observer runner retains Popen/identity, bounds reads and cleanup, and sends identity-checked SIGTERM only; no proc.kill, subprocess.run timeout escalation or duplicate ABI. check_pin.py --python must use that same bounded ownership primitive for its selected-interpreter child, rather than its existing implicit-kill subprocess.run timeout. An uncertain surviving standalone observer is a failed proof, never successful cleanup. Controller proof uses the parent-journalled observation channel, not the standalone fallback. No native pin expectation/package allowlist/receipt requirement is relaxed, and no new installed flat module is required. Stage changed shared helpers only; do not apply them to serving paths in this lane.

The inherited observation channel is justified only because the existing proof must stay in a fresh process while all observer commitments must see the parent's shutdown latch. An in-process proof would import/cache LiteLLM in the controller; merely terminating an outer helper would leave unjournalled observer uncertainty. Reuse stdlib socketpair and the existing tracked runner; add no listener, generic command API or standalone service.

The observation request schema is exactly `{version,seq,op}` with op `git_head`, `native_sources`, `native_tree` or `native_exports`; `git_root` additionally requires `path`, and `git_entries` additionally requires `relative`. Version is integer 1, sequence is an increasing positive integer, and no extra key/type coercion is accepted. Initial handshake has exact version, nonce, worker identity, source digest and pin digest bindings. Status/length response framing is one status byte (0 data or 1 refusal) followed by a big-endian unsigned 32-bit length and the bounded bytes. No additional message or tool is accepted after shutdown/refusal. Parent validates all message bounds and the worker's real identity independently of its claimed identity.

### Qualification v2

Keep the current closed qualification key set and add exactly `shutdown_contract`, equal by canonical structured comparison to `SHUTDOWN_CONTRACT`. Set top-level `version` to integer 2; version 1 and missing/extra fields refuse. Policy schema, RPC envelopes and per-request approval schema do not expand. Exact generated plist has timeout 60 and declared logging string 64. Loaded explicit environment parser rejects duplicate keys before equality checks. Require the current unique top-level loaded timeout comparison unchanged in strictness. Do not normalize away OSLogRateLimit.

Require the referenced canary evidence file, not just a digest-shaped string: fixed `canary/evidence/<digest>.json`, owned regular 0600, hash matching the record and a validated closed structure binding the current PID/birth, epoch, source/policy/generated-plist, supervision and shutdown contract. Evidence alone cannot create qualification or per-request approval. An isolated sibling controller's evidence never qualifies another identity.

## Operator-armed in-context canary design

This is a new fixed startup behavior, not a general-purpose RPC endpoint. The plan authorizes no live execution.

1. Add operator CLI `arm-canary` under the existing fixed `--policy`. It performs no service control and issues no qualification. It refuses any existing pending/consumed canary uncertainty. Its source-hash/file work resolves no credentials. No arbitrary executable, target label, port or environment arguments are accepted.
2. The arm envelope is a private, exclusive-created `canary/intent.json`: exact keys `version` (1), `operator_uid`, `nonce` (32hex), `policy_digest`, `controller_digest`, `controller_plist_digest`, `boot_identity`, `issued_at`, `expires_at`, `issued_monotonic`, `expires_monotonic`. Both finite lifetimes are positive and at most 300 seconds; use current boot identity from a bounded kernel read, not ambient environment. The exact generation hashes bind the next startup; an epoch cannot be known before startup and is bound in the resulting evidence instead.
3. The canary nonce is a synthetic operation ID, never an RPC restart request. Before consuming the arm, create a durable platform-canary operation with exact source/policy/generated-plist binding and paired null proxy asset/runtime digests; carry it through pending/running/passed or blocked, and journal every canary control/spawn before commitment. A consumed intent without matching terminal attempt blocks startup/mutating admission on the same process and future starts. Canary control never sets a primary-mutation fence or reopens a proxy request. Normal `serve` first creates the shared Shutdown object and installs handlers, then constructs the actual Runtime/Controller with that object. Before opening RPC admission, it may consume one valid arm envelope, with a synced rename, into the nonce-specific private canary scope. An invalid, expired, replayed or ambiguous arm blocks canary/mutating admission without silently deleting it. No arm means no canary service-control activity. The exact normal serve argv, PPID 1, kernel identity and loaded full generated definition must be verified first; an external-shell serve cannot qualify itself.
4. Derive a fixed disposable label `local.restart-canary.<nonce>` and files only under `state/canary/<nonce>/`. Never take them from an RPC or arbitrary user field. Refuse equality with proxy/controller labels, any existing job/file, indeterminate absence, symlink or ownership mismatch. Classify permission/unsupported-query failures as refusal, not job absence.
5. The canary generator runs the same pinned Python/installed entrypoint with operator-only `canary-worker <nonce>` mode and the existing policy path. That worker never constructs Runtime, resolves credentials or contacts the proxy. It only validates its generated private scope, records its own bounded identity/heartbeat and cooperatively obeys a one-time private exit marker. No shell or TCP listener is involved. The CLI mode does not add an RPC operation.
6. The actual serving controller process uses fixed `/bin/launchctl` operations only against that derived canary. It records bootstrap result, loaded fields and first incarnation, requests the worker's cooperative exit, observes KeepAlive's second distinct birth/incarnation, then bootouts the canary and proves absence/cleanup. No subsequent control follows a refusal, timeout, ambiguous result or shutdown latch. Never bootstrap again to hide a failed run.
7. Each live canary step has an explicit deadline and retained action/child identities. The whole lane is bounded by the arm envelope's remaining lifetime. Query helpers are capped at 10 seconds, bootstrap at 30 seconds, bootout at 75 seconds so the requested 60-second grace can be observed, and KeepAlive observation at 75 seconds. They are caps, not a summed promise; all waits use the earlier global expiry. Canary-worker shutdown uses the shared 50-second deadline, cooperative exit and no signal escalation. Capture actual loaded timeout 60; do not claim a natural SIGKILL deadline was physically exercised merely by reading that field.
8. Separate evidence classes: `fixture` (no real launchctl), `isolated-launchd` (sibling test context), `intended-controller` (actual exact normal serve identity/context). Only the last is eligible for operator qualification. Live worker shutdown evidence does not replace offline interruption tests of the real engine. No canary transcript establishes model/MCP acceptance or production handoff.
9. Evidence envelope exact keys: `version` (1), `scope`, `nonce`, `boot_identity`, `operator_uid`, `controller_identity`, `controller_epoch`, `controller_digest`, `policy_digest`, `controller_plist_digest`, `controller_supervision`, `shutdown_contract`, `canary_label`, `canary_plist_digest`, `incarnations`, `checks`, `detail_digest`, `issued_at`, `issued_monotonic`. `incarnations` is exactly two ordered strict identities with distinct births and observed run counts 1 then 2. `checks` has exact booleans `loaded_definition`, `bootstrap`, `keepalive`, `bootout`, `cleanup`, `shutdown_budget`; qualification requires all true and scope intended-controller. Keep per-step timing/outcome facts in a separate hashed bounded detail record referenced by the required `detail_digest` field. No raw logs/output or secret values. Producer verifies facts and writes evidence only after successful cleanup; failure remains a blocked journal outcome, not success evidence.
10. Before startup admission, including when canary/intent.json is absent, scan the complete bounded fixed canary namespace and cross-check every consumed nonce scope against its synthetic operation, spawned worker and terminal evidence/detail. Atomic rename is not proof of completion. A crash before/after intent consumption, missing attempt, unrecognized file, partial write, absent required terminal evidence or unresolved child blocks startup mutation in the same process and later starts; status may remain diagnostic. Pending valid intent is admissible only to the exact startup canary, never to RPC mutation. Persist intent consumption/control uncertainty before a live attempt. Crash/incomplete canary records require operator reconciliation; KeepAlive of the controller must never rerun the canary automatically. A successful record may be reviewed by the operator but cannot issue qualification itself. There is no automatic legacy-generation update or re-bootstrap of the existing controller.

### Closed canary detail and independently derived checks

The detail file is exclusively created at `canary/detail/<detail_digest>.json` (digest of canonical bytes), owned regular single-link 0600. Exact keys: `version` (1), `nonce`, `boot_identity`, `controller_identity`, `controller_epoch`, `steps`, `shutdown`. All identity/nonce/boot/epoch bindings equal the envelope; arrays and file sizes use the same explicit bounds as canary evidence.

`steps` contains exactly, in order, `bootstrap`, `first_loaded`, `exit_once`, `keepalive`, `bootout`, `cleanup`. Each entry has exactly `name`, `started_monotonic`, `completed_monotonic`, `outcome`, `identity`, `runs`, `loaded_exit_seconds`, `loaded_environment_digest`. Times are finite ordered same-boot numbers. Success evidence requires outcome `passed` for every step. Loaded observations have strict identity/runs/timeout/environment-digest values; fields not applicable to a step are explicit null, not omitted. First-loaded requires runs 1, KeepAlive requires runs 2 and a distinct birth. Freeze this per-step applicability in T016 schema tests: bootstrap has null identity/runs/loaded fields; first_loaded has first identity, runs 1, timeout 60 and expected environment digest; exit_once has first identity/runs 1 and null loaded fields; keepalive has second identity, runs 2, timeout 60 and expected environment digest; bootout has the last verified second identity/runs 2 and null loaded fields; cleanup has null identity, runs 0 and null loaded fields. Add required step key `exit_code`: exactly 0 for bootstrap/bootout, null for the other steps. Derive success from these values, verified generated hashes and ordered worker/kernel observations, not the outcome word alone.

`shutdown` has exactly `identity`, `requested_monotonic`, `exit_observed_monotonic`, `cleanup_completed_monotonic`, `deadline_seconds`. Identity is the second worker incarnation; requested time comes from its private first-SIGTERM latch receipt, not from caller intent. Exit time is the parent's actual kernel-observed disappearance of that identity, and cleanup time is its verified final group/job cleanup. All share a boot and satisfy requested <= exit-observed <= cleanup-completed, with cleanup-completed <= requested + 50 and deadline_seconds exactly 50. The process must remain genuinely observed, not inferred from bootout returning zero.

Qualification revalidates both envelope/detail hashes and all underlying values, then derives the checks rather than trusting producer booleans. `shutdown_budget` refers to the shared primitive exercised by the canary worker, not a claim that a live primary restart was interrupted. Require existing source-matched offline engine interruption acceptance separately at the owner qualification gate. Missing receipt, unverified disappearance, unknown group state or over-budget cleanup refuses rather than producing success evidence.

The trigger is same-user accident prevention, not protection against a same-user program forging the operator's private file. No lifecycle-guard exemption is added. Any tool refusal is surfaced; only the trusted external operator may perform a separately approved activation.

## Test commands and baseline discipline

Run from the implementation checkout. Set `PYTHON` to its approved test-venv interpreter and `TMPDIR` to the approved private scratch root before every invocation; do not use system temp or guess a fixed user's home path. First run:

```sh
"$PYTHON" -B -m pytest -q tests/test_restart_controller.py tests/test_controller_qualification.py tests/test_restart_runtime.py
```

Expected pre-change result: exit 0. Actual counts must be read from that run, not copied from this plan. Each task below uses the common five-step TDD loop: add the named behavioral test; run its exact file/selection and require the intended assertion failure (not collection/import failure); implement the listed minimum; rerun targeted tests plus touched existing suites; commit only its coherent intended paths. Test snippets below describe future tests, not executed evidence.

### T001 — Shared shutdown latch/deadline

Depends: none. Covers SC-002. Create `core/restart_control/shutdown.py`, `tests/test_controller_shutdown.py`; register the source in `runtime_identity.py` and CT-006 registry in `docs/restart-controller.md` in the same commit. Expose the interface above. Test first request fixes deadline, repeated/reentrant requests cannot extend it, and production constants are fixed with no env override. Run `"$PYTHON" -B -m pytest -q tests/test_controller_shutdown.py -k deadline`. Commit `feat(controller): add shared shutdown deadline`.

Complete test seed:

```python
import importlib.util

def test_first_shutdown_deadline_cannot_extend():
    assert importlib.util.find_spec("core.restart_control.shutdown") is not None, "shutdown API missing"
    from core.restart_control.shutdown import Shutdown
    now = [100.0]
    shutdown = Shutdown(clock=lambda: now[0])
    shutdown.request()
    first = shutdown.deadline
    assert first == 150.0
    now[0] = 120.0
    shutdown.request()
    assert shutdown.deadline == first
```

### T002 — Ordered one-action commitment

Depends: T001. Covers SC-003. Modify `shutdown.py` and its test file. Add deterministic barriers around the shutdown-open read and OS-submission stand-in. Assert latch-before-commit produces zero actions; commit-before-latch can produce only that one action; no second action commits; reused permit refuses. Run the same file with `-k commitment`. Commit `feat(controller): order action commitment against shutdown`.

### T003 — Closed journal schemas and bounded scan

Depends: T001. Covers SC-004, SC-005. Create `core/restart_control/recovery.py`, `tests/test_controller_recovery.py`; update source registries in the same commit. Freeze the exact enums/validation, common bindings and callback API above. Test every missing/extra/wrong-type key, null-binding restriction, invalid IDs, duplicate JSON, oversized entries/populations, directory/symlink/link/mode/owner mismatch and incomplete atomic-write names. Run `"$PYTHON" -B -m pytest -q tests/test_controller_recovery.py -k schema`. Commit `feat(controller): validate bounded recovery journal`.

### T004 — Durable spawn-intent transitions

Depends: T003. Covers SC-004. Modify `recovery.py` and its tests. Prove intent is synced before a test spawn callback; identity is persisted afterward; every interrupted write/order point retains uncertainty; an intent with no identity grants no signal permission. Use existing atomic JSON rather than a second writer. Run recovery tests with `-k spawn`. Commit `feat(controller): journal owned child intent`.

### T005 — Mutation fence ordering

Depends: T003. Covers SC-005, SC-006. Modify recovery tests/implementation. Cover fence-before-control commitment, consumed-intent write failures, directory-sync failure, accepted-terminal-before-clear, and crash around fence removal. A manually recoverable pre-commit fence is permitted; zero controls still required when latch beats commitment. Run recovery tests with `-k fence`. Commit `feat(controller): persist primary mutation fence`.

### T006 — Complete recovery barrier and active handle

Depends: T004, T005. Covers SC-005. Enforce full bounded cross-record consistency before any new operation; exact active handle exclusion only. Cover same-process uncertainty, new epoch with unresolved prior work, fresh request, new qualification, orphan temp record, consumed request without final acceptance, and child uncertainty after terminal acceptance. Run recovery tests with `-k barrier`. Commit `feat(controller): block work on unresolved recovery state`.

### T007 — Nonspawning process-group census

Depends: T001. Covers SC-004. Modify `process_identity.py`; add `tests/test_controller_group_identity.py`. Bind the verified libproc ABI and bounded growing group buffer, checking return size, truncation/races and cap exhaustion. Test genuine retained disposable groups plus mock ABI failure/reuse cases. Also add the bounded kernel boot-identity read required by canary arming; validate its ABI/errors rather than reading an environment token. No signals or `ps` fallback. Run `"$PYTHON" -B -m pytest -q tests/test_controller_group_identity.py`. Commit `feat(controller): inspect owned groups without child queries`.

### T008 — Engine lifecycle attachment and admission

Depends: T002, T006. Covers SC-002, SC-003, SC-005. Modify `restart_controller.py`, `engine.py`, `runtime.py` attachment only, and shared fixture adapters in `tests/test_restart_controller.py`, `tests/test_restart_runtime.py`, `tests/test_restart_lifecycle_fixtures.py`. The CLI constructs the shared shutdown/latch before Runtime; Controller receives it, creates the journal and binds production backend, and requires clear recovery/shutdown under mutation admission. Keep existing public constructors usable via explicit optional test injection, never a production bypass. Run the targeted baseline suite. Commit `feat(controller): gate engine admission on lifecycle state`.

### T009 — Track helper spawn/identity/cleanup

Depends: T004, T007, T008. Covers SC-003, SC-004. Modify `_capture`, diagnostic helper paths and ownership cleanup in `runtime.py`; add `tests/test_controller_runtime_shutdown.py`. Journal every owned helper, acquire/persist identity within cleanup guard, use nonspawning group census, poll shutdown promptly and cap remaining cleanup globally. Permit cleanup-only diagnostics if necessary, not recursive untracked subprocesses. Test fast exit, unknown birth, lost group leader, descendants, timeout and signal refusal. Run runtime-shutdown tests with `-k helper` plus existing runtime tests. Commit `feat(controller): interrupt and track owned helpers`.

### T010 — Contain runtime-proof observers without escalation

Depends: T009. Covers SC-003, SC-004. Modify `runtime_identity.py`, `process_identity.py`, `runtime.py`, `restart_controller.py`, `check_pin.py`, `tests/test_controller_runtime_shutdown.py` and `tests/test_native_runtime_identity.py`; create and register `core/restart_control/proof_worker.py` with dedicated private-channel fixture tests. Update the CT-006 registry and installed closure in the same source-introduction commit. The old proof helper runs Git/nm children and `_tool_output` calls proc.kill on timeout; subprocess.run timeout can also kill implicitly. Remove both controller-path escalation mechanisms. Preserve a fresh isolated proof worker and route its exact read-only tool requests through the parent's attached tracked runner via the inherited bounded observation channel specified above; never import LiteLLM into the controller or call its prove_runtime in-process. Preserve flat standalone CLI digest/schema and bounded no-SIGKILL cleanup. Test observer timeout/output cap, shutdown while Git/nm runs, unknown identity/group cleanup and semantic proof-output parity and a poisoned matching helper-cache control. Run runtime-shutdown proof cases plus `tests/test_native_pin_cli.py`, `tests/test_native_controller_closure.py` and `tests/test_native_runtime_identity.py`. Commit `fix(controller): contain runtime proof observers`.

### T011 — Candidate and credential/HTTP admission

Depends: T010. Covers SC-003, SC-004. Modify `Runtime.rehearse`, `check_gates`, `_request`, `_accept`, `_listener`, `_job_state`; no manifest/resolver contract change. Check the shared latch before each next child/resolver/request. Bound HTTP with an absolute deadline and wake polling; cleanup owned candidates inside remaining shutdown budget. Candidate and tracked read-only diagnostic may coexist. Fake sentinels must prove no next resolver/candidate/request runs after shutdown. Run runtime-shutdown tests with `-k 'candidate or credential or http'` and preflight suites. Commit `feat(controller): stop candidate and acceptance admission`.

### T012 — Engine commitment/phase interruption

Depends: T005, T008, T011. Covers SC-003, SC-005, SC-006. Modify `_prepare`, `_restart`, rollback and phase persistence in `engine.py`. Arm fence before first primary-control commitment, integrate exact active journal context, check shutdown between all backend steps, and never issue a next control on shutdown/unknown helper outcome. Distinguish before-commit zero-control interruption from after-commit manual recovery. Avoid wrapping away `controller_stopping` as `runtime_invalid`/candidate failure. Run shutdown tests with `-k engine` and `tests/test_restart_controller.py`. Commit `feat(controller): preserve uncertain interrupted operations`.

### T013 — Wire/CLI bounded drain

Depends: T012. Covers SC-002, SC-003, SC-006. Modify `wire.py`, `restart_controller.py` and shutdown socket tests. Install the shared latch before startup work; stop listening/queued mutation admission promptly and join using one remaining deadline, not 50 seconds per worker. Preserve inode-safe socket cleanup and diagnostic recovery state. No abrupt process exit may be presented as verified child cleanup. Test real Unix sockets, queued connections, repeated signals and lost clients. Run `tests/test_controller_shutdown.py` plus `tests/test_restart_controller.py`. Commit `feat(controller): drain within shared shutdown budget`.

### T014 — Exact 60/log64 generated generation

Depends: T013. Covers SC-001. Modify `controller_job`, strict loaded-environment parser, installer/qualification tests. Declare the logging key and timeout explicitly. Reject wrong/missing/duplicate logging fields, arbitrary extras, clamped/missing/duplicate/nested loaded timeout and wrongly matching digest-only records. Do not modify installed assets. Run `tests/test_controller_qualification.py`, `tests/test_restart_runtime.py`, `tests/test_native_controller_closure.py`. Commit `feat(controller): declare supported supervision generation`.

### T015 — Qualification v2 and evidence bindings

Depends: T014. Covers SC-007. Extend exact Runtime qualification schema and fixtures. Require new structured shutdown contract and validated fixed-path evidence, not merely a shaped digest; retain current epochs/identity/time/plist/permissions gates. Producer will arrive in later tasks, so fixture records must remain explicitly fixture-scope and not become accepted real production qualifications. Run qualification tests with each independent field downgraded in turn. Commit `feat(controller): bind shutdown contract in qualification`.

### T016 — Canary envelopes and private scope validation

Depends: T006, T007, T015. Covers SC-009. Create `core/restart_control/canary.py`, `tests/test_controller_canary.py`; register source in both registries in the same commit. Freeze exact arm/evidence/detail schemas, freshness/boot checks and derived scope. Implement the exact detail schema/applicability and derived checks above; no raw subprocess output. Include every consumed canary scope and partial rename/write in the bounded no-arm startup recovery scan. Deny arbitrary keys, existing jobs/files, indeterminate absence and production-label equality. Live query/absence classification must be proven against the actual platform before a permitted bootstrap; offline tests label responses as fake. Run canary tests with `-k schema`. Commit `feat(controller): validate fixed canary scope`.

### T017 — Operator arm command and one-time consumption

Depends: T016. Covers SC-009. Add `arm-canary` CLI mode without expanding RPC; use private exclusive creation, generation/boot bindings, dual-clock expiry and synced consumption. Refuse pending/consumed uncertainty. CLI performs no service control/credential resolution. Tests invoke the real installed-layout entrypoint with temporary HOME and fake policy/input facts. Run canary tests with `-k arm`. Commit `feat(controller): arm one startup canary`.

### T018 — Disposable worker mode

Depends: T013, T016. Covers SC-009. Add `canary-worker <nonce>` operator CLI mode handled before Runtime creation. It validates fixed scope, emits bounded incarnation records, supports one cooperative exit marker, handles shutdown using shared primitives, and never starts HTTP or credential/model code. Test genuine retained worker children, invalid scopes and shutdown deadlines; no launchctl. Run canary tests with `-k worker`. Commit `feat(controller): add disposable canary worker`.

### T019 — Exact-context startup orchestration

Depends: T009, T017, T018. Covers SC-009. Integrate canary run before production RPC admission in normal `serve`, after source/policy/self-supervision/PPID/identity verification. Add fixed canary generator, tracked helper calls, normal KeepAlive second-incarnation observation, bounded bootout and success evidence only after cleanup. Mark uncertain consumed attempts before live calls. No arm gives zero calls; invalid/incomplete arm leaves status diagnostic and mutations blocked. Run canary tests with `-k startup` against an explicit fake platform adapter and real worker fixtures, asserting exact derived argv/labels and no retry. Commit `feat(controller): orchestrate armed startup canary`.

### T020 — Request/control boundary interruption matrix

Depends: T012, T013. Covers SC-008. Extend `tests/test_restart_lifecycle_fixtures.py` and add `tests/test_controller_interruption.py`. Use genuine fixture primary/candidate children, kernel observations and sockets. Inject shutdown at every named prepare/restart/rollback boundary, including after commitment before OS submission, inside helper and after an unknown control outcome. Assert exact call trace, original identity/HTTP preservation before commitment, no false terminal acceptance and no subsequent controls. Run `"$PYTHON" -B -m pytest -q tests/test_controller_interruption.py`. Commit `test(controller): exercise shutdown boundary matrix`.

### T021 — Forced-death recovery in new processes

Depends: T006, T020. Covers SC-004, SC-005, SC-008. Add subprocess cases that kill only retained disposable controller test children after instrumented checkpoints; never signals by discovered port/name. Include spawn-before-identity persistence and crash around fence writes/clears. Restart a fresh controller instance over the retained private disk state; assert global recovery refusal, no auto child signal and no next prepare/restart calls. Clean fixture children only with retained identity or cooperative control; uncertain cleanup fails the test and retains evidence. Run interruption tests with `-k crash`. Commit `test(controller): prove durable interrupted recovery`.

### T022 — Canary scope/qualification negatives

Depends: T015, T019. Covers SC-007, SC-009. Test sibling scope, old PID/birth/epoch, changed source/plist, clock/boot mismatch, wrong supervision/logging, omitted detail file, bad detail digest, incomplete cleanup, respawn count other than exactly two, control refusal and replay. No success envelope or qualification may be emitted on failure. Run canary and qualification suites together. Commit `test(controller): reject stale and incomplete canary evidence`.

### T023 — Executed gate mutation battery

Depends: T020, T021, T022. Covers SC-008, SC-010. Add `tests/test_controller_shutdown_mutations.py` or a repository-local test utility outside `core/`. In disposable source clones only, remove one shutdown check, active-handle barrier input, spawn-intent write, fence sync check, or exact qualification binding at a time. Require green control; compiled mutation; intended behavioral assertion failure (not import/setup failure); restored green. Missing source anchor or escaped mutation is a failed verification. Run the mutation file explicitly and archive actual counts. Commit `test(controller): falsify shutdown recovery gates`.

### T024 — Installer closure and operator documentation

Depends: T019, T023. Covers SC-001, SC-007, SC-009, SC-010. Verify new registry/module copies and new generated plist only in private redirected installer targets. Update `docs/restart-controller.md`, amendment delivery status and operations guide together with implementation. Document exact new errors, v2 qualification schema, arm/worker boundaries, recovery reconciliation prerequisites, predecessor-state audit and separate activation gates. Do not claim a shipped live canary or platform qualification. Run installed-layout tests and command help/no-action CLI cases; live examples are labeled operator-gated/not run. Commit `docs(controller): document cap-aware recovery operations`.

### T025 — Final review and offline acceptance

Depends: T024. Covers SC-008, SC-010. Run complete suite, targeted real-process tests, mutation battery, hygiene/registry checks and independent spec-compliance/security/code-quality reviews. Re-run affected tests after every fix. Parent verifies child artifacts/digests and actual tool output, records counts without mental estimates, commits atomic fixes and syncs Beads. Attempt Git push; no configured remote is a reported publication blocker, not permission to invent one. Keep protected renderer bytes unchanged and report residual work. Commit only if this task introduces reviewed documentation/fixes, never an empty synthetic completion commit.

### T026 — Separately gated live-canary packet (not execution)

Depends: T025. Covers SC-009, SC-010. Before selecting a fresh state root, produce a bounded hash-pinned predecessor-state inventory: every pending/prepared request, consumed approval, operation/fence and spawn/child record, plus per-item disposition and operator reconciliation decision. Unknown/truncated/unreconciled state blocks packet readiness; no automatic root-switch or silent archive. Prepare an exact immutable candidate-generation activation/canary packet: reviewed source/plist/policy digests, bounded fresh arm lifetime, derived scope, expected observations, no-retry cleanup/refusal behavior and a retained primary identity/readiness snapshot. Do not arm a soon-expiring intent before the operator is ready, install to a live label, activate any controller, run service control, issue qualification or handoff. Owner may then separately authorize the specific real isolated-canary lane and later the intended controller generation. A packet without actual execution results remains preparation evidence.

## Execution wave DAG and traceability

The dependency-derived ready sets are:

1. T001.
2. T002, T003, T007.
3. T004, T005.
4. T006.
5. T008.
6. T009.
7. T010.
8. T011.
9. T012.
10. T013.
11. T014, T020.
12. T015, T021.
13. T016.
14. T017, T018.
15. T019.
16. T022.
17. T023.
18. T024.
19. T025.
20. T026.

Subagents may execute only nonconflicting independent slices; shared source registries and same-file work are parent-reconciled, not concurrently overwritten. A ready set is a dependency fact, not permission to edit the same file concurrently.

- SC-001: T014, T024.
- SC-002: T001, T008, T013.
- SC-003: T002, T008, T009, T010, T011, T012, T013.
- SC-004: T003, T004, T007, T009, T010, T011, T021.
- SC-005: T003, T005, T006, T008, T012, T021.
- SC-006: T005, T012, T013.
- SC-007: T015, T022, T024.
- SC-008: T020, T021, T023, T025.
- SC-009: T016, T017, T018, T019, T022, T024, T026.
- SC-010: T023, T024, T025, T026.

## Exit criteria and honest remainder

Offline completion requires real executed passing fixtures, meaningful mutation failures, reviewed registry/installer closure, no production mutation and no unresolved material review finding. It does not close live qualification or production handoff. The live-canary/context evidence and intended generation still need separate permission and actual observed results; no artifact or review substitutes for them.

Owner approved this plan and subagent-driven execution. Implementation proceeds task-by-task with spec-compliance then code-quality review; live canary, generation activation and production handoff remain separately gated.
