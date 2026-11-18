# Owner runbook — generation 761fdf29 deploy + first controlled restart (2026-11-18)

Kit HEAD 23efa05 (fixes becbd44 + 735185d). Proxy pid 47263 on :4000 is
untouched by every step. Stop on any nonzero exit or refusal code; paste
output back to the session.

PY=~/.local/share/litellm/runtimes/verified-7897e75e79cf-20261103T022910Z/python-env/bin/python
KIT=<worktree>
STAGED=~/.hermes/cache/scratch/policy-new-gen.json
POLICY=~/.config/litellm-restart-controller/policy.json

## 1. Bootout the running (stale) controller

    /bin/launchctl bootout gui/502/ai.litellm.restart-controller
    launchctl print gui/502/ai.litellm.restart-controller   # expect not found (113)

## 2. Retire old bundle + plist (renames, never deletes)

    mv ~/.local/share/litellm/restart-controller-54060da8 \
       ~/.local/share/litellm/restart-controller-54060da8.retired-20261118
    mv ~/Library/LaunchAgents/ai.litellm.restart-controller.plist \
       ~/Library/LaunchAgents/ai.litellm.restart-controller.plist.retired-20261118

## 3. Swap the LIVE policy to the new generation

BEFORE install, so the generated plist embeds the stable config path
(scratch is pruned after 24h idle — never let the plist point there).

    cp $POLICY $POLICY.retired-20261118
    cp $STAGED $POLICY
    chmod 600 $POLICY

## 4. Install the new generation (copies kit HEAD bytes; never activates)

    env PYTHONDONTWRITEBYTECODE=1 $PY $KIT/core/install_restart_controller.py --policy $POLICY

expect: {"status":"installed","activation":"not_performed","qualification":"operator_required"}

## 5. Verify installed bytes match the kit (fail-closed)

    env PYTHONDONTWRITEBYTECODE=1 $PY $KIT/core/install_restart_controller.py --policy $POLICY --verify

expect: {"status":"verified","activation":"not_performed"}

## 6. Arm the canary + bootstrap — ONE paste (5-minute arm window)

    env PYTHONDONTWRITEBYTECODE=1 $PY \
      ~/.local/share/litellm/restart-controller-761fdf29/restart_controller.py \
      --policy $POLICY arm-canary \
    && /bin/launchctl bootstrap gui/502 ~/Library/LaunchAgents/ai.litellm.restart-controller.plist

## 7. Observe the canary lane (typically <1 min, worst ~3.5 min)

    sleep 60
    launchctl print gui/502/ai.litellm.restart-controller | grep -E 'state =|pid =|runs ='
    ls ~/.local/state/litellm/rc-v2/canary/evidence/

Then tell the session — the agent verifies evidence + terminal journal
row (`canary_passed`) before qualification reissue.

## 8. Reissue qualification (agent-staged script; run after step 7 verifies)

    bash ~/.hermes/cache/scratch/rc-recon-9bfb768e/2026-11-18-761fdf29-reissue.sh

## 9. Agent resumes

prepare (new request id) -> owner approval paste -> controlled restart.
