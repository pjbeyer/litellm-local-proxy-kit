# Manual-recovery disposition record — operation 9bfb768e

**Date:** 2026-11-18 (EST)
**Operator:** Phil Beyer (owner, uid 502)
**Authorized by:** owner instruction "proceed as you propose" in the
Hermes Flex session, this date.
**Disposition:** operation reconciled to `manual_recovery_required` /
`manual_recovery_required` (closed failure record), fence released as
reconciled-uncertain-resolved. State root `~/.local/state/litellm/rc-v2`.

## Justification (SC-006 proven-safely-cancelled)

The restart request `9bfb768ea8a84652ae64c125e1748e83` (production)
failed at phase `stopping` with `recovery_invalid`. Root cause
(diagnosed offline, commit `becbd44` in
pjbeyer/litellm-local-proxy-kit): the engine's `_restart()` wrote journal
phase entries without re-binding the backend's in-memory operation
handle; the first lifecycle-aware admission (the service-stop control
call) refused the stale handle by identity and aborted **before any
control effect spawned**.

Evidence the committed service-stop never executed:

1. Primary proxy pid 47263 is alive on its exact original birth
   (2026-11-15 21:35:23 EST = plan `original_identity` birth
   [1794796523, 241985]); verified live 2026-11-18.
2. Port 127.0.0.1:4000 still LISTEN by pid 47263 (lsof, 2026-11-18).
3. The fault path (`_admit_helper` -> `require_clear`) precedes the
   control spawn in the call order; bootout never ran.
4. Controller daemon (pid 79585, epoch ae2774d9) itself was never
   restarted during the window.

The durable `primary_control_committed: true` therefore records a
commitment whose external action is proven never to have occurred. Per
shutdown contract SC-006, the outcome is treated as uncertain unless
proven safely cancelled; this record is that proof.

## Disposition actions (operator-side, no RPC exists)

- `recovery/operations/9bfb768e….json`: phase
  `manual_recovery_required` (unchanged), outcome set
  `manual_recovery_required` (closed failure record; schema-permitted
  per plan T003: `manual_recovery_required` -> `None` or
  `manual_recovery_required`).
- `recovery/mutation.json` (fence, state `uncertain`): archived to
  `archived-mutation-manual-recovery-9bfb768e-20261118.json` at the
  state root (NOT inside `recovery/` — the bounded inventory refuses
  foreign entries there), then removed — per operations doc
  reconciliation rule 2, the fence is cleared by a documented manual
  recovery, never silently; this file is that documented recovery.
- `recovery/archived-operation-pre-manual-recovery-9bfb768e…json`
  likewise moved to the state root (pre-edit snapshot of the row).
- Approval remains consumed; request record retained unchanged; all 19
  spawn rows bound to this request remain `closed`. Nothing deleted.

## Required code change (validator gap, diagnosed during disposition)

The kit's view validator had NO branch accepting a closed
`manual_recovery_required` row (schema line 97 permits the pair; the
plan schema at `_matching_approval` lines 900-903 enumerates the
`code`/`failed_phase` suffix; but `_require_clear_view` only branches
for blocked/prepared/restarted/rolled_back). Without it, NO operator
disposition could ever clear the barrier — the closed manual state was
unrepresentable. Fixed in the kit alongside `becbd44`:
`_validate_manual_history` + closed-manual branch in
`_require_clear_view`. Verified offline: `require_clear()` on a scratch
copy of the reconciled live state PASSES with the fix and REFUSED
without it.

## Follow-up

Controller deployment must still be refreshed to carry the fix
(`becbd44`) before the next prepare; the serving daemon
(restart-controller-54060da8, epoch ae2774d9) predates it.
