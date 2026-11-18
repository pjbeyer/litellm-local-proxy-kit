# the deployment-root defect Remediation V&V — 2026-11-17 (spec-reconciliation session)

Independent of the implementation session. Every claim checked against live state and replicated by direct computation/execution.

## Verdict

**REMEDIATION VERIFIED IN SUBSTANCE — with ONE CRITICAL DISCREPANCY: the A1 fix is not installed on the serving daemon. The issue closure is premature on the deployment axis.**

## Verified

- Close reason's items 1–2: policy `deployment_root` → work repo (live file confirmed); digest `c6b3ec0f…` bound in the live qualification record. Stuck row `8ca36e1a` dispositioned (phase `blocked`, outcome `blocked`, pcc false) — journal reconciled.
- **A1 fix (commit `e833376`)**: exactly the V&V prescription — fail-closed probe of the freeze() fact closure inside `qualify()`, before record acceptance, `qualification_invalid` on any miss, read-only. Pushed (`116aaba`), remote in sync.
- **6 new regression tests** present (`test_controller_qualification.py:303,327` + variants); **my independent suite run: 1138 passed + 14 subtests, 0 failed** (guard-lane excluded) — exactly the prior 1132 + 6 new, consistent with the claimed 1187 total.
- Controller restarted under corrected policy (epoch `d8566aa4`, PID 2357, runs=4); proxy on :4000 untouched (PID 47263 throughout).
- Old qualification archived, not deleted (`archived-qualification-v2-oldpolicy-20261116.json`).

## CRITICAL FINDING (V1) — the fix is not live

- The serving daemon still executes `restart-controller-f7cf523` with **pre-fix `runtime.py`** (`ae25e913…` — the G6 bytes). Only the kit worktree carries the fix (`4ac18a04…`). No new bundle directory exists (only `f7cf523` and the retired `aee69ab`).
- The reissued qualification record binds `controller_digest: dd74e980…` — which I reproduced as **the kit-worktree closure**, NOT the installed-bundle closure (`cd6fdbe9…`, also reproduced). The record therefore qualifies a generation that is not installed.
- Consequence chain (code-verified): the installed runtime's `freeze()` compares installed-copy facts against kit facts and raises `controller_copy_changed` on mismatch — the kit has advanced past the bundle, so **live prepare still cannot pass**. The controller remains "qualified" yet structurally unable to restart: the exact latent-trap class A1 was written to close, now on the bundle axis (the qualify() probe binds kit bytes, and qualification was issued against kit bytes, while the serving generation lags).

## Required follow-up (owner disposition)

1. Install a new controller bundle generation from the fixed kit (the established install flow), retire/`f7cf523` as `aee69ab` was retired, kickstart the controller.
2. Reissue qualification bound to the **installed** generation's closure digest (and fresh epoch/identity).
3. Consider hardening (same latent-trap class): qualify()/freeze() should compare the record's `controller_digest` against the **installed bundle's** closure, not the kit's, so a kit-bundle skew can never be qualified again. This is the bundle-axis twin of A1.
4. Reopen `the deployment-root defect` (or file the follow-up under it) until the installed generation matches the qualified generation — the close reason's "requalification" claim is true of the policy axis only.
