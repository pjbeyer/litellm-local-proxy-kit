# the deployment-root defect V&V Record — 2026-11-16 (spec-reconciliation session)

Reviewer: independent of the filing session. Every claim checked against live state (policy file, journal rows, source code, offline digest/drift_check replication) — not the filer's self-report.

## Verdict

**VALID — all primary claims verified. Remediation sketch is correct and complete; two narrative corrections and one addition for the remediation session.**

## Primary finding — CONFIRMED

Live policy `~/.config/litellm-restart-controller/policy.json` sets `deployment_root: ~/.config/litellm`. Source-verified consequences:

- `runtime.py freeze()` (L1114) requires under `deployment_root`: `deploy.conf.sh`, `deployments/<name>/{overlay.yaml, credentials.toml, pin.lock}`. `~/.config/litellm` has **no `deployments/` dir** (live-verified) → prepare cannot ever pass → refusals `runtime_invalid` are structural, not flaky.
- `check_gates()` (L1155) runs `drift_check.py --deployment-root <deployment_root>` → same root failure.
- **qualify() gap CONFIRMED:** `qualify()` (L1521–1639) exercises only canary/policy/plist/supervision/contract binding — zero freeze/check_gates/deployment validation. So qualification v2 (my G6 anchor) passed while prepare was structurally broken. `the deployment-root defect` is a genuine escaped defect of the qualification scope, not a filer error.

## Evidence claims — all verified

- Five refused requests `dc35af49 / 9c05e052 / e4b530bc / 3dc014af / 8ca36e1a` present in `rc-v2/requests/`; all `primary_control_committed: false` (four terminal `blocked`, one `preparing` — the stuck row).
- Proposed new policy digest **reproduced exactly**: `c6b3ec0f…965dff92` (canonical-JSON sha256 of the live policy with `deployment_root` swapped to the work repo). Current live digest `10e191f5…` (matches the stuck row's `policy_digest`).
- Offline replication confirmed by my own independent runs: work-repo `deploy.conf.sh` is clean `KEY=VALUE` (0 `:=` lines); **my drift_check against the work-repo root: PASS** (15 checks, 0 divergent) — matches the filer's claim.
- Work repo has the full deployment closure: `deployments/work/{overlay.yaml, credentials.toml, pin.lock, native-build.json}`, plists, `deploy-env.sh`; no `drift-exemptions.yaml` (correct — freeze() treats absence as `None`).
- r1 evidence doc line ~135 confirms the deployment-root→work-repo requirement was known on 2026-11-11.

## Corrections to the issue narrative

- **C1 — provenance, not "predates":** the 11-11 policy inventory (SHA `e9420462…`) already had `deployment_root → work repo`. The **gate-5 policy** (`10e191f5…`, 11-16) *regressed* it to `~/.config/litellm` — created after the requirement was documented, with the wrong value. The qualification was issued against an already-broken policy. Same defect, sharper provenance; gate-5 reissue procedure is implicated, not just "stale policy".
- **C2 — secondary finding RESOLVED, not open:** the "parser contradiction" was never real. The two roots have **different** `deploy.conf.sh` files: `~/.config/litellm` uses the `: "${VAR:=...}"` form (21 lines) which drift_check cannot parse; the work repo's is plain `KEY=VALUE` (0 `:=`). The filer hit the unparseable root and PASS ran against the parseable root — same file shape is wrong; they are different files. No source-level reconciliation needed; drop this sub-finding.

## Addition for remediation (new, from this V&V)

- **A1:** `qualify()` should bind `policy_digest` against a validated deployment root — or run a prepare-gate dry probe — before issuing/renewing a qualification record. The gap that let this escape (qualify passes while freeze is broken) should close with this remediation, not just the policy edit. File as part of the remediation, not a separate issue.
- **A2:** The owner package's "step 1 root-cause: managed-file drift" (check_pin/runtime_identity Nov-2 bytes fixed session-side via installer apply) is in the owner package but **not in the the deployment-root defect body** — carry it into the remediation record so the fix is documented in the durable tracker.

## Remediation sketch — validated end-to-end

Owner steps 1–5 verified coherent: policy edit (digest reproduces), controller kickstart (boot-only policy load — code-verified), new epoch/identity via status RPC, qualification reissue with unchanged controller digest/contract and fresh 6h lifetimes, then prepare → approval → restart. Precondition note: the stuck `preparing` row (8ca36e1a) needs operator disposition per restart-controller-operations.md recovery **before/with step 2** — it currently makes status return `recovery_invalid` (owner package already says this; confirmed live).
