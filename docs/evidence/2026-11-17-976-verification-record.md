# the stale-bundle defect V&V Record — 2026-11-17 (spec-reconciliation session)

Independent of the filing session. Every claim checked against live state (journal, qualification record, launchd, digests, installer source).

## Verdict

**VALID in substance — all operative claims verified live. One direction correction (D1) and one confirmation note (N1). Remediation shape is code-consistent and supersedes the generic items in my the deployment-root defect reopen with a concrete install sequence.**

## Verified against live state

- **Bundle stale vs kit HEAD**: installed `restart-controller-f7cf523/restart_control/runtime.py` = `ae25e913…` (pre-A1), kit = `4ac18a04…` (post-`e833376`). Exactly ONE CONTROLLER_INPUTS file differs (my own digest sweep, prior turn + reconfirmed) — matching 976's offline isolation claim.
- **Prepare refusal `b232826…`**: present in requests (`runtime_invalid`) and journal (`blocked`/`blocked`, pcc false), binding policy `c6b3ec0f` (corrected policy). Live refusal under a *valid* policy and *valid* qualification — confirmed.
- **Current qualification record**: epoch `a565bda2`, pid 9952, issued 11-16 22:46:58, expires 11-17 04:46, binds policy `c6b3ec0f` + controller_digest `dd74e980`. Controller now runs=5, PID 9952. (This is a second reissue — see N1.)
- **Installer is install-only, cannot update in place**: `install_restart_controller.py` L59 raises `state_io` if destination or plist exists; only verification of an existing install (L53–56) is supported. So the refresh must change `controller_root`, which changes `policy_digest`, and the policy edit must precede the fresh install — 976's sequencing note is code-verified and is a real operational constraint my reopen record did not carry.
- Consequence claim (every prepare refuses `controller_copy_changed`; A1 probe cannot run because the serving bundle predates it) — matches my independent finding and reproduction from the the deployment-root defect reopen V&V.

## D1 — direction correction (narrative only, same trap)

976 says the reissued qualification "binds controller_digest dd74e980 — the STALE bundle." That is backwards: `dd74e980` is the **kit-HEAD (post-A1) closure**, not the stale bundle — the installed/stale closure is `cd6fdbe9` (both digests independently reproduced in the 2026-11-17 fhz-remediation V&V record). The record binds a generation that is *ahead* of what is installed, not behind. Operative consequence unchanged — the qualified generation is not the serving generation, prepare refuses either way — but the remediation session should not "fix" this by installing *older* bytes or by re-binding the record to the stale bundle; the fix is install-the-fixed-kit-then-bind-the-installed-closure, as 976's own remediation shape correctly lays out.

## N1 — double reissue observed

Two qualification reissues 8 minutes apart: 22:38 (epoch `d8566aa4`, pid 2357 — cited in the the deployment-root defect remediation comment) and 22:46 (epoch `a565bda2`, pid 9952 — current, cited by 976 as "22:44"). Both bind `dd74e980` + `c6b3ec0f`, so neither changes the picture; noted so the record count is honest (the archived-qualification dir should hold the superseded one).

## Cross-reference disposition (owner decision, not taken by V&V)

`the stale-bundle defect` and the reopened `the deployment-root defect` (deployment-axis follow-up items 1–3 in the 2026-11-17 fhz-remediation V&V record) cover the same defect. 976 is the more operative tracker: it carries the concrete install sequence and the policy-before-install constraint. Recommend: work 976 as the remediation tracker; close `the deployment-root defect` when 976 lands (its code-axis A1 fix is already verified; only the shared deployment axis remains), rather than carrying both in parallel. The hardening item (qualify() should compare `controller_digest` against the INSTALLED bundle closure — the bundle-axis twin of A1) belongs to whichever tracker survives and should not be dropped in the consolidation.

## G7 posture (unchanged from reopen)

First controlled restart remains blocked until: fresh install from fixed kit → new policy_digest → reissue bound to installed closure → prepare green. Qualification window expires 04:46 EST; machinery re-runs in minutes, no rework needed if it lapses.
