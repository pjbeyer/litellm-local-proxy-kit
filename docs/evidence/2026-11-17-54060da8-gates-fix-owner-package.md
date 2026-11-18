# Owner package: controller generation refresh `54060da8` — gate observation fix (2026-11-17 late)

Audience: Phil (owner). Owner-side steps only; the session verifies and runs the RPC lane.

## Why (one step further each time)

Generation `8c5e0f06` fixed restart's freeze (proof worker impossible without an
open journal operation) but refused `recovery_invalid` at the NEXT pre-intent
step: `check_gates` spawns its gate helpers through `_capture`, the journaled
spawn family that also requires an active operation — same structural conflict,
one step later. Fix `e02c5e1` routes gate helpers through `_observe` (the
sanctioned non-journalled read-only observation family; nonzero exit refuses
`gate_failed` by name). Suite: 1191 passed.

This is the third latent restart-path defect surfaced by actually attempting
the live restart; each was invisible to the fixture suite because fixtures
either stubbed the spawn seam or ran under an active operation.

## Live facts (computed from kit HEAD `e02c5e1`)

- Controller closure digest: `54060da81f257f767c0d2511439aaee4f42efaf2ea5e40f1b1337a376bc1df0a`
- New bundle dir: `~/.local/share/litellm/restart-controller-54060da8`

## Owner sequence

1. Deactivate (proxy keeps serving):

```sh
launchctl bootout gui/502/ai.litellm.restart-controller
```

2. Retire `8c5e0f06` (archive):

```sh
mv ~/.local/share/litellm/restart-controller-8c5e0f06 ~/.local/share/litellm/restart-controller-8c5e0f06.retired-20261117 && mv ~/Library/LaunchAgents/ai.litellm.restart-controller.plist ~/Library/LaunchAgents/ai.litellm.restart-controller.plist.retired-20261117e
```

3. Policy edit: `controller_root` →
   `/Users/pjbeyer/.local/share/litellm/restart-controller-54060da8`

4. Install + verify:

```sh
/Users/pjbeyer/Projects/work/flex-ai-litellm-local-proxy/.venv-tests/bin/python -B /Users/pjbeyer/Projects/pjbeyer/litellm-local-proxy-shutdown/core/install_restart_controller.py --policy ~/.config/litellm-restart-controller/policy.json && /Users/pjbeyer/Projects/work/flex-ai-litellm-local-proxy/.venv-tests/bin/python -B /Users/pjbeyer/Projects/pjbeyer/litellm-local-proxy-shutdown/core/install_restart_controller.py --policy ~/.config/litellm-restart-controller/policy.json --verify
```

5. Bootstrap:

```sh
launchctl bootstrap gui/502 ~/Library/LaunchAgents/ai.litellm.restart-controller.plist
```

6. Arm + kickstart ONE paste:

```sh
/Users/pjbeyer/Projects/work/flex-ai-litellm-local-proxy/.venv-tests/bin/python -B /Users/pjbeyer/Projects/pjbeyer/litellm-local-proxy-shutdown/core/restart_controller.py --policy ~/.config/litellm-restart-controller/policy.json arm-canary && launchctl kickstart -k gui/502/ai.litellm.restart-controller && echo ARM-AND-KICKSTART-DONE
```

7. Session verifies canary; hands you the reissue one-liner (archive name
   `archived-qualification-v2-pre-54060da8-20261117.json`); you run it.

8. Session: prepare → owner approves → approval file + restart in one window
   → verify :4000 under a new pid, same epoch.
