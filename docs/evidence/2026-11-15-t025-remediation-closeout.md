# T025 offline acceptance — remediation closeout — 2026-11-15

**Session:** implementation session (Hermes default profile), worktree
`litellm-local-proxy-shutdown`, branch `feat/controller-shutdown`.
**Authority:** `docs/plans/2026-11-03-controller-shutdown.md` T025 + the
two independent reviews dispatched 2026-11-15 07:15 EST (deleg_64a58a40).
Private working document — scrub local paths/IDs before any public derivative.

## Scope

Independent spec-compliance and security/code-quality reviews of commit
`de4d943` (T019–T024 implementation) returned material findings. All
material findings were remediated in three atomic commits; this record
closes T025 offline acceptance. Live qualification and production handoff
remain separately owner-gated (T026 packet, gates 4–5).

## Remediation commits (review findings → fix)

| Commit | Subject | Closes |
|---|---|---|
| `0b2c02b` | fix(restart_control): remediate independent-review findings in the shutdown lane | Review-1 findings 1–14, Review-2 findings 1–14 (material set) |
| `1d46aec` | test(restart_control): align fixtures and battery with remediated gates | Fixture/qualification-suite realignment; battery cases +2 |
| `f7cf523` | docs(controller): correct the canary refusal-code listing | Documentation drift (never-raised `canary_diagnostic_present`) |

### Core gates hardened (commit 0b2c02b)

- **recovery:** the platform-canary operation is rewritten to
  `canary_pending` BEFORE its first validation (review blocker: the
  `preparing` phase was validator-rejected, so the canary journal could
  never begin on a real store); `spawn_intent` admits canary-purpose
  children against the synthetic operation; `_require_clear_view` blocks
  admission while a consumed canary scope lacks coherent terminal
  evidence (SC-005).
- **runtime (qualification v2):** evidence scope binds the runtime class —
  fixture accepts `fixture`/`isolated-launchd` only, production accepts
  `intended-controller` only (review blocker: production `qualify`
  accepted fixture evidence and rejected intended-controller evidence);
  the referenced detail file is opened, hash/mode/owner-validated and
  cross-bound to the evidence envelope; the record's controller identity
  is the full five-key process identity the producer emits.
- **startup:** the refreshed canary ticket from `canary_phase` is
  re-bound via `refresh_operation` (review major: the old ticket was
  discarded, breaking later helper admission); `_job_state` returns the
  observed state and raises `canary_job_absent` on the runtime's
  not-found signature; query caps are bounded by the arm's remaining
  lifetime; evidence/detail bytes are canonical (`canonical_bytes`) so the
  filename digest equals the file-bytes hash; the loaded environment
  digest must equal the derived canary job's; `_write_blob` validates the
  parent directory against symlink redirection.
- **canary:** the scan recomputes evidence/detail digests from file bytes
  and rejects duplicates; `validate_detail` derives success from step
  outcomes and cross-binds both worker incarnations and the shutdown
  identity; an arm from a foreign kernel boot is stale; the signal
  handler requests shutdown before any durable receipt write.
- **engine:** a blocked request is recorded only when its journal
  operation exists (orphan-request leak fix); `_sync_dir` closes directory
  descriptors (fd-leak fix across canary/startup).
- **runtime_identity:** `kernel_process_absent` (ESRCH via `proc_pidinfo`,
  136-byte buffer) is the sole authority for worker disappearance —
  identity read failures are never death evidence.

## Verification (verified venv, TMPDIR → approved scratch root)

- Full pytest suite at HEAD (`f7cf523`):
  **1172 passed, 14 subtests passed, 0 failed** (2 `os.fork()`
  DeprecationWarnings from the intentional crash-driver tests — accepted
  design, pre-existing).
- Mutation battery (`tests/test_controller_shutdown_mutations.py`):
  **7/7 killed**, each with behavioral assertion failure and a verified
  fresh-clone restored-green rerun (archived in
  `tests/_mutation_battery_counts.txt`):
  `shutdown_latch_checkpoint`, `engine_replay_refusal`,
  `recovery_spawn_intent_durable_write`, `recovery_fence_sync_write`,
  `engine_plan_expiry_check`, `recovery_exact_active_ticket`,
  `runtime_qualification_scope_binding`.
- All three remediation commits are on `feat/controller-shutdown` (local
  only; no push authority under the conservative profile).

## Honest remainder

- Live supervision/platform qualification (Task F item 5 / T026 gate 4)
  NOT performed — owner-gated, not claimed.
- No live launchd operation, no production state mutation, no
  credentials resolved, no proxy/controller operation in this session.
- The mutation-battery anchor drift found by Review-2 (a stale anchor in
  the battery harness, later fixed in `1d46aec`) produced one interim
  6/7 result; the archived and committed 7/7 run post-dates the fix.
