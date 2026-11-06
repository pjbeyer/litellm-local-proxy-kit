# Decision-sink hardening contract

Status: source-only candidate under verification; independent review remains required.
No live activation is approved by this document.
Scope: the existing optional decision sink, not guard policy or restart authority.
This refines the guard's existing FR-007/SC-004 bounded-evaluation guarantee.

## Invariants

- D1. `evaluate` returns the same allow/block decision and fixed refusal vocabulary
  regardless of telemetry failure. No telemetry operation grants an exception.
- D2. Hook-path logging never performs new state-filesystem I/O or waits for a
  writer, storage lock, queue space or flush. A slow filesystem cannot hold the
  guard's enforcement result. Existing bounded discovery is unchanged.
- D3. Every record has exactly `ts`, `category`, `code`, `tool`. Category and code
  are drawn from the existing enum; invalid pairs are dropped, not stringified.
  Check exact-string types and character lengths before normalization, encoding
  or copying. Invalid category/code types or lengths over 128 drop the record;
  invalid tool types or tool length over 128 characters map immediately to
  fixed `other` without
  examining its contents. Never invoke arbitrary object conversion methods.
  Apply the same checks at direct `sink_write` and `log_decision` entry points.
  Tool values are bounded canonical names or a fixed `other` value; namespaces,
  paths, operands, PIDs, arbitrary objects and oversized metadata are not stored.
  The closed tool set is `terminal`, `execute_code`, `browser_exec`, `tool_call`,
  `process_manage`, `other`. For an exact string at most 128 characters, take the
  lowercased last dot-separated component and keep it only if in that set;
  otherwise use `other`. Timestamp is ASCII ISO-8601 `YYYY-MM-DDTHH:MM:SS+HHMM`
  or its negative-offset form, at most 32 characters; new records use UTC +0000.
  Encoded records have a 512-byte cap, also capped by the file cap.
- D4. Only block decisions enqueue telemetry. Allow decisions do not create state.
  No source/module import starts a worker or touches the filesystem.
- D5. Enqueue is bounded: at most 64 pending capped records and one active worker
  per loaded module generation. The adapter starts its worker during `register`,
  outside `evaluate`, and registers bounded cleanup through the verified
  `ctx.on_unload` API. No callback creates/restarts a worker. The worker remains
  available while registered; no racy idle-exit/restart protocol is introduced.
  Admission uses a nonwaiting try-lock; if unavailable, drop immediately instead
  of waiting for an ordinary queue mutex. Queue-full, startup failure or a wedged writer
  drops telemetry without blocking enforcement or spawning replacement workers.
  Worker state is in-process only; no subprocess, service or signal is introduced.
- D6. Jobs capture the state-home value and record at submission. A later
  environment change cannot redirect pending records. Path resolution/validation
  occurs only in the writer. Effective state-home is captured from XDG_STATE_HOME
  or the current HOME fallback using string operations only; missing HOME drops
  telemetry, not a potentially blocking account lookup. Reject non-exact-string
  inputs and character length over 4096 before encoding or copying. Only immutable
  capped values may enter the queue. Paths over 4096 encoded
  bytes, over 64 components, relative/NUL/traversal paths are rejected.
- D7. Managed state directories are owner-owned directories, opened without
  following links. Filesystem traversal uses retained directory descriptors;
  unsafe directory or entry types are rejected without touching their targets.
  Validated managed directories MUST be 0700 before entry operations, including
  newly created ones. Ancestors outside
  the managed sink are never chmodded.
- D8. Active, rotated and lock objects must be owner-owned, regular, single-link
  files, opened without following links and with nonblocking flags. Existing
  linked, FIFO, device, socket, directory or unowned objects are rejected.
  Every new or existing active/rotated/lock file must be verified 0600, using
  fchmod on a validated descriptor when necessary, BEFORE any content write.
  Close every pending/validated descriptor on every error, failed lock or return
  path; failed ownership/type/mode validation must not leak a resource.
  Unsupported OS primitives drop telemetry rather than falling back unsafely.
- D9. Cooperating writers use a validated lock file and nonblocking exclusive
  lock. Contention drops the record; no retry loop or unbounded lock wait.
  Directory and lock-file open/validation/acquisition necessarily precede the
  critical section. Active/rotated file operations, validation, size checks,
  rotation and append are inside the acquired lock.
- D10. A successful append requires active and single rotated generation to be
  within the configured file cap. Oversized pre-existing state is rejected
  unchanged, not silently deleted/repaired or claimed healthy.
  Validate an existing rotation destination before replacing it. Rotation failure,
  oversized existing state or an unmaintainable bound drops the append rather
  than growing state or deleting an unsafe object. Revalidate created/reopened
  descriptors. Account using encoded bytes, not characters.
- D11. A short/failed append cannot be reported as a complete JSONL record.
  Restore the previous validated file length where safe under the lock, otherwise
  stop writing that malformed state. No partial line is silently joined to a
  later record. No arbitrary file contents or exception paths are logged.
  Before subsequent appends, validate the bounded existing JSONL contents and
  complete line endings, so a short-write/failed-truncate failure is detected
  even by a fresh process. Existing JSONL is strict UTF-8 without a BOM;
  UTF-16 auto-detection is not accepted. Malformed state is rejected unchanged.
  Every existing line must satisfy the full D3 schema and encoded record cap:
  exactly four unique keys (reject duplicate/extra keys), exact-string values,
  valid category/code pair, closed tool value and timestamp format. Syntactically
  valid but schema-invalid JSONL is also rejected; include a fresh-process case.
- D12. Telemetry degradation is observable through bounded non-secret counters
  or fixed diagnostics, never argument/path/PID material. A dropped record is not
  claimed durable. Logging diagnostics themselves are outside the hook path.

## Implementation boundary

Keep logic in existing `guard.py`; do not introduce an unhashed imported helper
or change the installed source/ref closure. Keep a synchronous, fail-open
`sink_write(category, code, tool)` primitive for isolated writer/tests, but have
`log_decision` submit a capped job to the bounded daemon worker. Actual decision
logging must also use canonical metadata, not forward raw names to Python logs.
Preserve the existing immediate guard decision-log line independently of sink
admission/delivery; new sink diagnostics cannot add callback filesystem I/O.
The adapter may change only to start outside the callback and register cleanup;
both existing source files remain the complete integrity closure.

Provide explicitly bounded `sink_flush(timeout_s=...)` and `sink_shutdown`
helpers for lifecycle/tests/diagnostics, plus `sink_start` outside callbacks and
non-I/O `sink_status` diagnostics. Drain must not be called by `evaluate` or the
plugin callback, or be exposed as a provider tool. Tests that
assert a file exists must drain before inspection and before temporary state
cleanup; the callback is no longer permitted to await persistence. Existing
watchdog documentation must not equate enqueue with durable or live delivery.
Drain accounts for both queued and in-flight work and reports whether processing
completed before its deadline, not whether storage is durable. On timeout a test
must fail and release or isolate its worker before deleting temporary state.
Standalone diagnostics must explicitly start/drain/stop outside `evaluate`;
source watchdog integration and test doubles must honor that lifecycle.

On unload, stop admission and request worker exit; cleanup waits only a bounded
interval and never signals a worker. A genuinely stuck filesystem operation may
retain one daemon worker in its module generation and exhaust that generation's
bounded queue. It must not hold up interpreter shutdown. Owner process restart
is recovery for a wedged writer, not an agent-invoked signal or auto-restart.
No claim of process-global worker bounds across arbitrary owner-triggered module
reload generations is made. Tests must prove no repeated worker spawn within a
wedged generation. No global model, sandbox or profile changes are authorized.

## Required executed verification

1. Retain all five original failing probes, each in a fresh process, and get
   meaningful green assertions, not an earlier setup rejection. For asynchronous
   logging inspection use the bounded test drain; a missing file alone cannot
   stand in for correct sanitization of a healthy submitted record.
2. Add malicious directory/active/rotation/lock kinds, ownership, mode-before-write,
   hard-link and symlink-target-preservation cases using disposable state only.
3. Prove real FIFO handling returns promptly, and inject a stalled writer to
   prove actual `evaluate` still returns its protected refusal, queue memory is
   bounded and repeated calls never create replacement worker threads.
4. Test cross-process contention and rotation using valid bounded JSONL state;
   an earlier malformed-file rejection must not mask the rotation branch.
5. Pin all record values and encoded caps, short-write/error behavior, allow-path
   no-write and metadata capture across environment changes.
6. Preserve quotation/wrapped-intent regressions, original suite and state-home
   isolation. Test workers must finish or be deliberately isolated before fixture
   deletion; synthetic events must never enter the real sink.
7. Execute targeted mutations with pristine positive controls; each repaired
   safety rule must be shown capable of failing. Obtain separate spec PASS and
   then quality PASS, bound to exact candidate hashes.

## Stop and release gates

This is a source-only candidate in a dedicated worktree. Do not edit installed
plugins, policy, owner integrity references, services, model/credential config,
or another worker's source. Do not disable/reload the guard or retry preserved
refused invocations. Stop on any new permission/guard/approval denial; no alternate
executor. Do not commit, push or remotely sync under the conservative profile.
Live activation remains separately gated on verified bytes and the supported
owner workflow. Original shutdown and source-data veto blockers remain distinct.
