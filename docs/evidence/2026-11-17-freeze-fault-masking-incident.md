# Incident: restart refusal `runtime_invalid` masked a probe cleanup failure (2026-11-17 21:06)

## Sequence

1. 21:03 — live prepare #2 (`312053aa…`) succeeded: first-ever `status: prepared`
   on a fully qualified production controller (bundle `ad0bbcb0`, epoch
   `d6d53f79…`, qualification `d4789a09`-policy bound, fresh canary evidence
   `5f4bb07d…`).
2. 21:06 — live restart attempt refused `{"code": "runtime_invalid"}`. Disk state
   verified untouched afterwards: operation row still `prepared/prepared`,
   `primary_control_committed: false`, no fence, approval file intact,
   proxy pid 47263 still serving.
3. 21:12 — a fresh live prepare refused
   `{"code": "candidate_cleanup_failed", "original_preserved": false}`.

## Root cause

`Runtime.freeze()` (core/restart_control/runtime.py) wraps its entire body in
`except Exception: raise Fault('runtime_invalid') from None`. The runtime-proof
probe it invokes (`run_proof_worker`) cleans up its worker child through
`_owned_stop`, which on failure raises `Fault("candidate_cleanup_failed")` —
a `Fault`, which IS an `Exception`. freeze() therefore:

- **masked the real refusal code** (`candidate_cleanup_failed` →
  `runtime_invalid`), exactly the same defect class as the
  `ambiguous_listener` taxonomy bug (`the stale-bundle defect`), and
- left `_cleanup_blocked = True` latched in the daemon process — a sticky
  in-memory flag that is only ever reset at Runtime construction, permanently
  refusing every future prepare/rehearse in that process
  (`candidate_cleanup_failed`), with no operator-visible state on disk
  explaining it.

The underlying trigger at 21:06 was a transient owned-cleanup failure of the
proof worker (strict pgid census + SIGTERM + bounded reap; worker performs git
operations against the kit repo). The exact transient (load-driven reap timeout
vs census race) is undeterminable — the daemon logs to /dev/null by plist
config, and the masking converted the diagnostic into `runtime_invalid`.

## Defect to fix (next controller generation)

`freeze()` must not swallow `Fault`: `except Fault: raise` before the generic
`except Exception: raise Fault('runtime_invalid')`. The same audit should sweep
other broad `except Exception` handlers that can wrap code paths raising
`Fault` (candidate/observer cleanup in `_accept`, `_owned_observer_stop`
finally-blocks). The taxonomy test should gain a structural check that no
`except Exception` block in `restart_control/` converts a `Fault` into a
different code (static sweep of try/except shapes is hard; a targeted test on
freeze's fault-passthrough is the minimum).

## Recovery executed (same evening)

- No disk reconciliation needed (all spawn rows closed, no fence, nothing
  consumed). The blockage is purely the daemon's in-memory sticky flag.
- Sanctioned route: controller kickstart rotates the process (clears the
  flag), which rotates the epoch; the epoch rotation requires a fresh canary
  (arm + kickstart as one paste — the 5-minute arm window) and a qualification
  reissue against the final incarnation, then a fresh prepare.

## Evidence trail

- Approval `312053aa…` remains on disk with a plan-valid window (expiry capped
  at the plan's — fixed from the earlier `f0a647b1` approval whose +300 s
  window exceeded the plan's remaining lifetime and poisoned the journal view
  as tampering; that file was archived as
  `archived-approval-invalid-window-f0a647b1-20261117.json`).
- Proxy `ai.litellm.local-proxy` pid 47263 was never stopped: no control was
  committed before any refusal in this incident.
