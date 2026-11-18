# Manual proxy restart runbook (owner-run, 2026-11-16)

## Precondition (all verified live, 2026-11-16 22:5x)
- Canary config already staged at the live target: ~/.config/litellm/config.rendered.yaml,
  sha256 c173d24aed976527... — COMPLEX tier = [glm-5.3, openai/gpt-6.1-sol] (dd9a243 committed).
- Managed support files byte-match kit HEAD (installer --verify PASS; drift_check 15/0 PASS).
- Running proxy pid 47263 (booted Nov 15 21:35) predates the apply -> holds the old config.
- No in-flight controller operation (all journal rows terminal, primary_control_committed=false).

## The restart (one command, at your terminal)
    launchctl kickstart -k gui/$UID/ai.litellm.local-proxy

SIGTERMs the current job; launchd relaunches it under the same plist; the wrapper re-resolves
credentials (pure API via service-account token, no 1Password desktop contact) and boots with
config.rendered.yaml = the canary. Expect ~10-20 s of :4000 downtime.

## When
Any time you're at the desk. Safe checkpoint: nothing depends on a mid-flight restart — the
session's only pending task IS this restart. Do NOT restart while a long completion is streaming
through :4000 (kills in-flight requests; num_retries=0 means no client retry).

## What I verify right after you run it
1. :4000 health 200, NEW pid (not 47263).
2. Loaded config = c173d24aed97 (canary active).
3. COMPLEX-tier routing: probes confirm the 2-model pool + session-sticky pinning.
4. Report + record in flex-kl8e8.9.

## Rollback (if canary misbehaves)
One-line pool edit in deployments/work/overlay.yaml (drop openai/gpt-6.1-sol) -> installer.sh
apply -> the same kickstart command. ~1 minute end to end.

## Note on the controller track
This manual restart is independent of lpkit-976 (controller bundle refresh). The canary goes
live either way; the controller work continues separately whenever you pick it up.
