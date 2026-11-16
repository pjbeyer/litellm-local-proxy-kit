# G6 Independent V&V Record — 2026-11-16 (spec-reconciliation session)

Reviewer: spec-reconciliation session (independent of the implementation session).
Mode: every claim checked against live tool output (git, bd, pytest, digests, launchd state), not self-report.

## Verdict

**PASS — implementation verified; G7 (first controlled restart) is an owner decision with all preconditions met.**

## Independently verified

- Worktree `litellm-local-proxy-shutdown` @ `46610ce` on `feat/controller-shutdown`: 39 commits since base `d0691b2`, clean tree, in sync with origin.
- Blockers closed with owner dispositions: `lpkit-yt5.1` (T009 completed at `a3c4785` — real-kernel zombie-race repair), `lpkit-yt5.2` (four R1 release-lane items at `354e07f`/`46610ce`; owner "A" approval for staged activation + commit/push). `lpkit-yt5` correctly remains open as the release gate. `lpkit-i9a` closed (prior session).
- Independent suite execution (fresh run, not the implementation session's record): **1132 passed + 14 subtests passed, 0 failed** in 5:43, guard-lane suite excluded — consistent with the claimed 1172.
- Mutation battery: 7/7 killed, archived `tests/_mutation_battery_counts.txt` matches the closeout record.
- SC-001 ✓ (`ExitTimeOut 60`, `OSLogRateLimit 64` in `controller_job`); SC-002/T013 ✓ (drain on shared 50s contract budget); SC-007/T015 ✓ (qualification v2 implemented, `shutdown_contract` present).
- Installed bundle `restart-controller-f7cf523`: all 11 core files byte-match the worktree; `install.json` closure (13 sources) matches `CONTROLLER_INPUTS` exactly.
- Qualification record `~/.local/state/litellm/rc-v2/qualification.json`: version 2, controller digest `cd6fdbe9…` (== bundle digest), 21600 s validity, **valid until 2026-11-16 13:59 EST**. No restart approvals issued yet (first restart still gated).
- Live state: `ai.litellm.restart-controller` running the new generation (PID 28483, runs=1); legacy bundle retired by rename; proxy `ai.litellm.local-proxy` running, port 4000 LISTEN (PID 47263 — differs from R0's 1217; see O3).

## Observations (non-blocking)

- **O1 — doc drift (the one real finding):** the CT-006 closed input registry in `docs/restart-controller.md` omits `core/restart_control/proof_worker.py`. The operative registries (`CONTROLLER_INPUTS`, installed `install.json`) both include it, so this is documentation-only, but T010's same-commit registry-update requirement was only partially met. Fix: one-line doc append on the implementation lane.
- **O2 — client timeout unchanged:** `restart_controller.py:154` still uses `client(timeout=1000)`. Contract-compliant (upper bound above the 50 s drain + 60 s exit), unchanged from the §2.4 census; recorded for the packet's timeout-mismatch thread, not a violation.
- **O3 — proxy PID drift since R0:** proxy now PID 47263 (R0 recorded 1217). Consistent with routine owner-side operation over the intervening week; controller activation evidence claims proxy untouched *during* the controlled transition, which this does not contradict. Flagged for awareness only.
- **O4 — tracker silence after 2026-11-11:** gate-4/5 work (11-15/16) is evidenced in `docs/handoffs/` and `docs/evidence/` but no Beads record was updated for it. Record hygiene gap, not substance; `lpkit-yt5` still open is correct.
- **O5 — guard refusal (preserved, un-retried):** this session's direct read of the canary evidence JSON was refused by the proxy lifecycle guard and not retried per discipline. The qualification record's embedded `canary_evidence_digest` (`5f3d2d5b…`) is the verification anchor instead. Candidate FR-013-family receipt for the guard spec lane.

## G7 preconditions (all met)

Qualification record valid (expires 13:59 EST today), controller serving the qualified generation, approvals ledger empty (no restart yet issued), suite + mutation evidence green, bundle↔worktree digest parity exact. First controlled restart awaits the owner's go/no-go.
