# Restart Controller Implementation Plan

> **For Hermes:** Execute the source split with parent-owned credential/runtime work and independent spec/code review. A coding lane may write only the non-credential engine/wire and fixture tests; it cannot operate production.

**Goal:** Deliver the reviewed current-generation restart controller without any production interruption.

**Architecture:** A closed Unix-socket protocol drives a durable, single-use state machine. A fixed macOS backend reuses installer/drift/wrapper/pin gates and performs only approved launchd operations. Operator qualification, bootstrap evidence and fresh per-request approval remain external always-gated inputs; there is no production approval in this session.

**Tech Stack:** Python stdlib, existing pinned proxy Python/PyYAML, Unix sockets/flock, Apple libproc/getpeereid and fixed launchctl argv.

**Design gate:** `restart-controller.md` SHA-256 `0b76bc483186e49b246e761350811a262f3934aa5336b469857d71b6c53e4e84`; trust/operations/release lenses completed. The remaining release-lens item is the explicit CT-005 implementation obligation to fix manifest ambiguity, not a new contract gap. Prior budget overruns remain disclosed in the review evidence.

## Task A — unique manifest before resolution

Files: `core/parse_credentials.py`, `core/run-proxy-wrapper.sh`, `tests/test_manifest_uniqueness.py`.

1. Write duplicate-row/key/top-level-key and no-partial-output tests.
2. Run `python -m pytest tests/test_manifest_uniqueness.py -q`; establish RED.
3. Reject duplicates in the shared parser and move wrapper manifest validation before Keychain/resolvers.
4. Run new tests plus the complete suite; preserve the pre-existing renderer edit.
5. Review and commit this coherent input-gate fix separately.

## Task B — durable non-credential engine

Files: `core/restart_control/__init__.py`, `core/restart_control/engine.py`, `tests/test_restart_controller.py`.

Implement `Fault(code)`, strict standard JSON, private 0700 directories/0600 records, fsync/full-sync atomic state, controller epoch and wall/monotonic freshness, thread plus cross-process nonblocking lock, exact request schemas, approval consumption and interrupted-outcome fencing. No real service/credential calls in this module.

`Controller(state_dir, backend, uid=None, clock=time.time, monotonic=time.monotonic)` exposes `handle(request)->dict`. `backend.label` is fixed. Backend methods are `inspect()`, `freeze()`, `check_gates()`, `accept_primary(identity)`, `rehearse()`, `snapshot_lkg(path)`, `validate_lkg(path,binding)`, `qualify(plan)`, `stop()`, `start()`, `accept_started()`, `restore_lkg(path,binding)`, `release_credentials()`.

- `inspect`: `{identity:{pid,uid,birth:[sec,usec]}, managed:bool}`.
- `freeze`: `{asset_digest,runtime_digest,policy_digest,controller_digest}`.
- Acceptance methods return sanitized outcome dictionaries; failures raise `Fault` with a closed diagnostic code, never input-bearing text.
- Candidate and LKG bindings each contain asset/runtime digest and must equal the frozen generation.
- `prepare`: retain identity, freeze, gates, primary acceptance, owned candidate rehearsal/cleanup, LKG snapshot, recheck identity/digests; block an unmanaged listener. Store an unexpired prepared plan only after success.
- `restart`: reject schema/duplicate/epoch/expiry/stale bindings, require qualification and exact fresh out-of-band approval, recheck gates/current acceptance/LKG/identity, durably consume approval and phase, then fixed stop/start/post-accept. Refusal means no further control calls. Post-check failure after successful control permits exactly one stop/restore/start/accept rollback. Never auto-retry a consumed operation.
- Release in-memory acceptance credentials after each operation. Status is read-only to the proxy and never invokes gates/secrets.

Use failing fixture tests, implement, then execute green tests. Cover service refusal at every phase, stale/expired/replayed input, crash cuts, PID birth mismatch, failed cleanup, failed post-check and both rollback outcomes. The fake backend may control real disposable loopback children; its results must say fixture, never live launchd/LiteLLM.

## Task C — wire and CLI

Files: `core/restart_control/wire.py`, `core/restart_controller.py`.

Serve only a private Unix socket, bounded JSON lines, peer UID check and exact schema. Refuse a pre-existing endpoint; unlink only the owned socket inode on shutdown. Provide a client using the same closed requests. CLI selects an operator-owned fixed policy, never request-supplied commands/paths. Test genuine socket dispatch with fixture backends, malformed/duplicate/nonfinite JSON, foreign UID, unavailable controller, busy operations and readonly status. Never add an RPC that creates approval or qualification.

## Task D — fixed runtime and provenance backend

Files: `core/restart_control/runtime.py`, `core/restart_control/runtime_identity.py`, `core/restart_control/process_identity.py`.

Implement the reviewed closed source registry, pin/actual tracked blobs/active-cache checks, clean plist environment, existing verification/preflight, in-memory manifest-master resolution, redirect-free loopback acceptance, owned candidate lifecycle, LKG bytes/modes manifest, fresh bootstrap/qualification evidence, and fixed launchctl operations. Use Apple proc_bsdinfo microsecond birth identity and getpeereid on macOS, not ps second-resolution guesses. No process-env dump, raw logs, arbitrary command backend or raw primary signal path.

First test runtime helpers with isolated temporary package/metadata fixtures and a real ephemeral HTTP listener. Then run the real installed preflight and real bounded candidate rehearsal while proving the original listener survives; current bridge ownership must block restart readiness. Production restart remains unauthorized.

## Task E — installer and portable operator guide

Files: `core/install_restart_controller.py`, `docs/restart-controller-operations.md`.

Write only the declared controller files/plist/private metadata directories; no bootstrap/bootout or permission elevation. Verify installed byte parity, fixed policy and generated supervisor plist. Explain external activation, trusted bootstrap/qualification evidence and separate owner approval; do not label a Hermes-owned daemon as independently supervised. Do not weaken the practical command guard or re-open OS-isolation selection.

## Task F — validation and closeout

1. Execute full tests, installed-layout fixture and controlled guard/state-machine mutations with green controls and byte-identical source preservation.
2. Independent local Codex review; validate each finding and add regression coverage for accepted fixes.
3. Re-run actual non-disruptive preflight/candidate acceptance; report MCP separately.
4. Commit only intended files, attempt push (preserve/report absent remote), record exact tool-backed results in the canonical tracker and a secret-free durable receipt.
5. Leave the combined issue open until separately approved live supervision/platform qualification, production handoff and rollback acceptance have actually occurred.
