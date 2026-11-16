# Restart enablement — owner actions (2026-11-16, sol-canary restart)

## Context
Config (COMPLEX-tier 50% session-sticky gpt-6.1-sol canary) is rendered, verified on a
throwaway instance (8/8 session stickiness), applied to the live target
(~/.config/litellm/config.rendered.yaml, sha256 c173d24aed97...), and committed
(flex-ai-litellm-local-proxy dd9a243). Installer --verify: PASS. Drift-check vs work repo:
PASS (15 checks, 0 divergent). Production proxy is healthy and unaffected until restart.

## Root cause of the prepare refusals (blocked requests preserved: dc35af49, 9c05e052,
e4b530bc, 3dc014af, 8ca36e1a)
1. Managed-file drift: deployed check_pin.py + runtime_identity.py were Nov-2 bytes; the kit
   gained the observer-containment change (f999378, Nov 12). FIXED session-side via
   installer.sh apply (atomic per-file, no launchd contact); --verify now passes.
2. Remaining blocker — POLICY LAYOUT BUG: policy.json deployment_root=~/.config/litellm, but
   the controller bundle (runtime.py freeze + check_gates) requires a full deployment root
   there: deploy.conf.sh (parses), deployments/<name>/{overlay.yaml,credentials.toml,pin.lock,
   native-build.json}, and a drift_check pass against that root. The r1 evidence doc
   (2026-11-11, line 135) already required "deployment_root → work repo" before any
   qualification v2; the current policy predates the remediation. The existing qualification
   passed because qualify() exercises the canary lane, not freeze/gates — prepare (which does)
   has never run live until today.
   Verified offline: with deployment_root=work repo, ALL freeze fact collections resolve and
   drift_check returns PASS. Mirror experiment at ~/.config/litellm/deployments/ was removed.

## Owner actions (in order)
1. Edit policy.json (owner-domain file):
     deployment_root: /Users/pjbeyer/.config/litellm
       → /Users/pjbeyer/Projects/work/flex-ai-litellm-local-proxy
   New policy_digest (canonical json): c6b3ec0f8b8b1ea833b47a5702718979e28a3e5d55e1ba5accfd8c40965dff92
2. Restart the controller daemon (boot-only policy load):
     launchctl kickstart -k gui/$UID/ai.litellm.restart-controller
3. Read the NEW controller epoch + identity via status RPC (session can print them for you).
4. Reissue qualification.json per gate-5 handoff one-liner with:
     policy_digest = c6b3ec0f8b8b1ea833b47a5702718979e28a3e5d55e1ba5accfd8c40965dff92
     controller_digest, controller_plist_digest, supervision, shutdown_contract,
     canary_evidence_digest = UNCHANGED from the current record
     controller_epoch, controller_identity = the NEW daemon's values (step 3)
     fresh issued/expires lifetimes (6h)
5. Tell the session — it will prepare, then you approve in Discord (request_id + asset digest
   posted at that point), then controlled restart + verification.

## Post-restart expectations
New proxy pid on :4000; config c173d24aed97 live; COMPLEX tier routes to the 2-model pool
(glm-5.3 / openai/gpt-6.1-sol) with per-(session,tier) affinity pins; revert = single-line
pool edit + one more controlled restart.

## Recovery journal disposition (needed before/with step 2)
The last prepare (8ca36e1a) died mid-freeze: journal row `preparing`, no terminal disposition —
this is why status now returns recovery_invalid. The other four prepares reached terminal
`blocked`. No operation ever committed primary control (all primary_control_committed=false).
Per restart-controller-operations.md "Recovery reconciliation": reconcile the preparing row with
an explicit operator disposition (retain as evidence, never delete) during/after the controller
restart — the fresh daemon boot should clear the latch once the disposition is recorded per the
contract's reconciliation procedure.
