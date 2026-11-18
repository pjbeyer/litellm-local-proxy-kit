# T026 — Candidate-generation activation packet — 2026-11-15

**Status: PREPARATION EVIDENCE ONLY.** Nothing in this packet was armed,
installed, activated, executed or qualified. Assembling it authorizes
nothing; each subsequent step is a separate owner gate (operations guide,
"Predecessor-state audit and activation gates"): (4) owner authorization of
the real isolated-canary lane, (5) owner authorization of the intended
controller generation. A packet without execution results is not
qualification.

**Session:** implementation session (Hermes default profile), worktree
`litellm-local-proxy-shutdown`, branch `feat/controller-shutdown`, HEAD
`f7cf523` (local-only; no push authority under the conservative profile).
**Authority:** plan T026 + `docs/restart-controller-operations.md` activation
gates. Private working document — scrub local paths/IDs before any public
derivative.

## 1. Fresh predecessor-state inventory (hash-pinned, read-only)

Observed 2026-11-15 10:27:26 EST. Secret-free; no credentials resolved; no
service operation, bundle, plist, policy or state file modified.

### State root `~/.local/state/litellm/rc/`

| Item | State | Disposition |
|---|---|---|
| `approvals/` | EMPTY | none required |
| `requests/` | EMPTY | none required |
| `snapshots/` | EMPTY | none required |
| `recovery/` | ABSENT (generation predates the recovery journal) | none required |
| `qualification.json` | ABSENT ⇒ restart authority correctly disabled | none required |
| `controller.lock` | 0 B, mode 0600, mtime 2026-11-02 23:07 | preserved |
| `controller.sock` | socket, mode 0600, live (controller PID 4328) | preserved |

**Inventory verdict: zero pending requests, zero consumed approvals, zero
snapshots, zero recovery records, zero consumed canary scopes. No
reconciliation debt exists in the predecessor state.** Unknown/truncated/
unreconciled state: none found → packet readiness is NOT blocked by state.

Delta vs the 2026-11-11 record: state root, legacy bundle, policy and plist
are byte-identical. One benign drift: **controller PID 1199 → 4328**
(the workstation rebooted between observations; `runs = 1`, never exited,
within the current boot). Identity must be re-read at authorization time
(§4) — a pinned PID is never evidence of liveness.

### Operator reconciliation items (unchanged, required before qualification v2)

1. **Legacy installed bundle** `~/.local/share/litellm/restart-controller-aee69ab/`
   — generation WITHOUT `shutdown.py`/`recovery.py` (confirmed absent);
   the running controller executes from it. SHA-256 (first 16 hex) / size:

   ```
   d09d8cdc291327fc   4735  install_restart_controller.py
   7328e3a2005b170b   2348  restart_controller.py
   136f2b100a705baa    173  restart_control/__init__.py
   1553b6b938088c5a  19710  restart_control/engine.py
   8e948626f43238d6   2406  restart_control/process_identity.py
   b0ccf3ef1c8bde1c  36071  restart_control/runtime.py
   c271b47a25e47694  24640  restart_control/runtime_identity.py
   fd4fc795f9013577   5072  restart_control/wire.py
   ```

2. **Stale `kit_root` pointer** in the policy — points at the READINESS
   worktree; must be repointed before any qualification v2.

### Policy and plist (non-secret fields only)

- Policy `~/.config/litellm-restart-controller/policy.json`
  SHA-256 `e94204622209b50477f17ff061a2d9d80f4b94c09123bf6569c0a7e3ca983e56`.
- Controller plist `~/Library/LaunchAgents/ai.litellm.restart-controller.plist`
  SHA-256 `d4c9db703d8256a5e3d99d819d0137dfda72a5688b631419d9c7f85359a98fee`.

## 2. Reviewed source digests (packet lineage)

Reviewed = the three T025 remediation commits (`0b2c02b`, `1d46aec`,
`f7cf523`) on top of reviewed `de4d943`, all under the two independent
reviews (deleg_64a58a40) with material findings remediated and the suite
green at HEAD. SHA-256 (first 16 hex) / size at `f7cf523`:

```
5f26c1b9345cc4b6   7082  core/restart_controller.py
1a028f1b43c654da   3204  core/install_restart_controller.py  (kit copy)
0d8c89dfcfee0dac   7923  core/install_launchagent.py
b22ecc3c5b1ab65d   9471  core/installer.sh
a800dc6ae7544bb5   2860  core/check_pin.py
136f2b100a705baa    173  core/restart_control/__init__.py
598e9b387869f9f1  35647  core/restart_control/canary.py
5dcda75e2e4cce9f  30979  core/restart_control/engine.py
15e7d719455d6fac   4816  core/restart_control/process_identity.py
6be896444cbd41d1   5981  core/restart_control/proof_worker.py
cf371852f2ee63c3  64626  core/restart_control/recovery.py
9aeed5b7b933dc5e  82573  core/restart_control/runtime.py
c1cb9ecebf7ceb61  31932  core/restart_control/runtime_identity.py
9fd22fdac1d9cf7d  12262  core/restart_control/shutdown.py
5e8be43d135f60e8  28615  core/restart_control/startup.py
8e6ffa6a0734aea7   6080  core/restart_control/wire.py
```

## 3. Bounded arm lifetime and derived scope

- Arm lifetime: **300 s dual-clock** (`_ARM_LIFETIME`, canary.py:169) —
  wall `expires_at` + monotonic `expires_monotonic`, both at most 300 s;
  one arm at a time; any pending or consumed scope blocks re-arm.
- Derived canary scope: fresh `~/.local/state/litellm/rc/<generation>/canary/`
  sub-root (operator selects the fresh state root; the current root's
  `canary/` does not exist and predates the journal), label
  `local.restart-canary.<nonce>` under launchd — never the proxy label.
- Scope contents at arm time: `intent.json` (arm digest, nonce, identity
  binding, both lifetimes), receipts, `diagnostic.json` on refusal. A
  refused lane never emits evidence; cleanup on refusal is no-retry.

## 4. Expected observations (what the authorized lane will do — not run here)

On owner authorization (gate 4), the isolated canary lane will:
1. Re-read the primary controller identity live (pid + microsecond birth +
   uid; NOT any pinned value from this packet) and require the loaded
   controller definition to match the current boot.
2. Arm the fresh scope (300 s), spawn the canary worker
   `local.restart-canary.<nonce>` (canary-purpose child admitted against
   the synthetic operation), first-load the candidate generation, keepalive,
   then exit cleanly and qualify against the detail file at its true
   content digest.
3. Fail closed on: binding mismatch, arm expiry, identity invalid,
   first-load/exit/keepalive/cleanup failure, shutdown receipt missing or
   unresolved, job absent. Any refusal leaves arm+receipts+diagnostic in
   the scope for operator reconciliation; no retry, no fallback.
4. Only after the isolated canary evidence qualifies does the intended
   controller generation become authorizable (gate 5) — separate decision.

## 5. Retained primary identity/readiness snapshot

- `ai.litellm.restart-controller`: state running, runs = 1, PID 4328,
  last exit code (never exited) — current boot. Proxy `127.0.0.1:4000`
  LISTEN (connect_ex 0); candidate port 4401 unused (connect_ex 61),
  as designed. Both PRESERVED — untouched by this session.
- The full current boot identity of the controller is re-derived at
  authorization time; this snapshot records only that the primary lane
  was alive and unmodified when the packet was assembled.

## Packet readiness

Ready for owner review as preparation evidence, gated on the two operator
reconciliation items above (legacy bundle replacement is the separately
gated SC-010 staged operation; `kit_root` repoint). No arming, install,
activation, service control, qualification or handoff was performed.
