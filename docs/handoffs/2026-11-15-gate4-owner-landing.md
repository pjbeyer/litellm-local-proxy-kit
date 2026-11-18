# Gate 4 owner landing — isolated-canary lane activation (Option A)

**Session worktree:** `<worktree>`
(branch `feat/controller-shutdown`, HEAD `354e07f`, clean, pushed).
**Owner approval on record:** "A" (full staged transition) + "Approved to
proceed" + "Commit and push your work / Authorized to proceed with all".
**Boundary hit:** agent-side `launchctl bootout` is vetoed by the LiteLLM
lifecycle guard (LGV-001, sink records 2026-11-15T16:02:32/35/38+0000 UTC) —
per the operations guide, controller activation is an owner-side operation
outside the model tool context. This file is the exact command sequence.

## What is already prepared (agent-side, verified)

- Staged v2 policy at
  `~/.hermes/cache/scratch/policy-v2-final.json` (0600):
  `kit_root` → the reviewed worktree (HEAD `354e07f`, git-clean),
  `controller_root` → `~/.local/share/litellm/restart-controller-f7cf523`
  (fresh bundle dir; legacy `restart-controller-aee69ab` untouched until
  step 2 renames it), `state_dir` → `~/.local/state/litellm/rc-v2`
  (fresh; agent verified EMPTY-of-prior-state by absence), `scratch_dir`
  → `~/.hermes/cache/scratch/litellm-rc-v2` (created 0700),
  `candidate_port` 4401, label unchanged.
- Dry-run verified with the runtime python
  (`.../runtimes/verified-7897e75e79cf-20261103T022910Z/.../bin/python`,
  3.11.15): all 13 `CONTROLLER_INPUTS` import clean under `-I`;
  `controller_job` generates the expected plist (ExitTimeOut 60,
  logging env, `-I` argv); `arm_bindings` returns
  `policy_digest=10e191f5…86b78`,
  `controller_digest=231f2b82…86c38`,
  `controller_plist_digest=34fa5e34…91ce7`.
- Expected observations at serve: arm consumed at startup, canary lane
  bootstraps `local.restart-canary.<nonce>` (runs 1 → cooperative exit →
  runs 2 → bootout), then evidence (`intended-controller` scope) +
  detail written under `~/.local/state/litellm/rc-v2/canary/`, terminal
  journal row `canary_passed`. Restart authority stays DISABLED
  (no `qualification.json` anywhere) — the proxy on :4000 is untouched
  by every step below.

## Owner commands (in order; stop on any nonzero exit)

```sh
# 1. Bootout the LEGACY controller job (proxy label untouched).
/bin/launchctl bootout gui/502/ai.litellm.restart-controller
launchctl print gui/502/ai.litellm.restart-controller   # expect: not found (113)

# 2. Archive legacy bundle + plist (renames, not deletes).
mv ~/.local/share/litellm/restart-controller-aee69ab ~/.local/share/litellm/restart-controller-aee69ab.retired-20261115
mv ~/Library/LaunchAgents/ai.litellm.restart-controller.plist ~/Library/LaunchAgents/ai.litellm.restart-controller.plist.retired-20261115

# 3. Install the new generation (copies reviewed sources; NEVER activates).
env PYTHONDONTWRITEBYTECODE=1 \
  ~/.local/share/litellm/runtimes/verified-7897e75e79cf-20261103T022910Z/python-env/bin/python \
  <worktree>/core/install_restart_controller.py \
  --policy ~/.hermes/cache/scratch/policy-v2-final.json
#   expect: {"status": "installed", "activation": "not_performed", "qualification": "operator_required"}

# 4. Verify installed bytes match the kit (fail-closed comparison).
env PYTHONDONTWRITEBYTECODE=1 \
  ~/.local/share/litellm/runtimes/verified-7897e75e79cf-20261103T022910Z/python-env/bin/python \
  <worktree>/core/install_restart_controller.py \
  --policy ~/.hermes/cache/scratch/policy-v2-final.json --verify
#   expect: {"status": "verified", "activation": "not_performed"}

# 5. Swap the LIVE policy to v2 (atomic replace, keep 0600).
cp ~/.config/litellm-restart-controller/policy.json ~/.config/litellm-restart-controller/policy.json.retired-20261115
cp ~/.hermes/cache/scratch/policy-v2-final.json ~/.config/litellm-restart-controller/policy.json
chmod 600 ~/.config/litellm-restart-controller/policy.json

# 6. ARM the canary (300 s window starts NOW — run step 7 immediately).
env PYTHONDONTWRITEBYTECODE=1 \
  ~/.local/share/litellm/runtimes/verified-7897e75e79cf-20261103T022910Z/python-env/bin/python \
  ~/.local/share/litellm/restart-controller-f7cf523/restart_controller.py \
  --policy ~/.config/litellm-restart-controller/policy.json arm-canary

# 7. Activate the new generation (launchd; serve consumes the arm and runs the lane).
/bin/launchctl bootstrap gui/502 ~/Library/LaunchAgents/ai.litellm.restart-controller.plist

# 8. Observe (canary lane takes up to ~3.5 min worst case; typically < 1 min).
sleep 45
launchctl print gui/502/ai.litellm.restart-controller | grep -E 'state =|pid =|runs ='
ls ~/.local/state/litellm/rc-v2/canary/ 2>/dev/null
ls ~/.local/state/litellm/rc-v2/canary/evidence/ 2>/dev/null
```

**On any refusal code** (`canary_*`, `recovery_invalid`, anything else):
STOP, do not re-arm (a consumed scope blocks a new arm until reconciled),
and paste the terminal output back to the session — the retained scope
plus diagnostic is reconciliation evidence, never something to delete.

## What the agent resumes after step 8

Verification only (read-only): evidence/detail digests, terminal journal
row, `status` RPC, fresh predecessor delta recording, evidence file, and
the Beads update. Gate 5 (intended-generation restart authority) remains a
SEPARATE later decision requiring a freshly prepared plan + fresh explicit
owner consent for that exact plan + operator-issued approval + an operator-
issued `qualification.json` v2 binding the canary evidence.
