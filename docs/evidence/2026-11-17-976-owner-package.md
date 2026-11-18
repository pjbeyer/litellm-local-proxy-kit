# Owner package: controller bundle refresh (the stale-bundle defect / reopened the deployment-root defect)

Status: PREPARED (agent) — all agent-side verification below is live.
Audience: Phil (operator). Every launchctl/edit/file-issuance step below is yours.

## What happened

The the deployment-root defect remediation committed the A1 fix (`e833376`) to kit HEAD but
left the DEPLOYED bundle (`restart-controller-f7cf523`) at pre-A1 bytes.
`freeze()` byte-compares installed copies against kit HEAD, so every prepare
refuses `controller_copy_changed` → `runtime_invalid`. The reissued
qualification binds kit-HEAD closure `dd74e980` for a bundle serving
`ae25e913` — a record qualified ahead of what is installed (the stale-bundle defect).

Agent-side hardening now in kit HEAD (this session, `qualify()` A2 probe):
qualification ALSO byte-compares every installed CONTROLLER_INPUTS copy
against the loaded sources and refuses `qualification_invalid` — the same
fail-closed discipline freeze() enforces, applied at qualification time.
A record can no longer run ahead of the installed bundle.

Live facts (verified 2026-11-17):
- Bundle vs kit HEAD: exactly 1 diff — `restart_control/runtime.py`
  (bundle `ae25e913`, kit `4ac18a04`).
- Kit-HEAD controller closure (with A1+A2): `44ac3115…`
- New bundle dir (naming precedent: first 7 of closure):
  `~/.local/share/litellm/restart-controller-44ac311`
- Policy digest AFTER the controller_root edit: `7e9fe0ed…f779f6`
- Installer is fresh-install-only (refuses existing destination/plist),
  must run from the kit, install validates against the FINAL policy.

## Owner sequence (in order; step 3 before step 4 — install validates the final policy)

1. Deactivate the controller (leaves the proxy on :4000 running):
```sh
launchctl bootout gui/502/ai.litellm.restart-controller
```

2. Retire the old bundle + plist (archive, never delete — precedent:
   `restart-controller-aee69ab.retired-20261115`):
```sh
mv ~/.local/share/litellm/restart-controller-f7cf523 ~/.local/share/litellm/restart-controller-f7cf523.retired-20261117
mv ~/Library/LaunchAgents/ai.litellm.restart-controller.plist ~/Library/LaunchAgents/ai.litellm.restart-controller.plist.retired-20261117
```

3. Edit `~/.config/litellm-restart-controller/policy.json`:
   `controller_root` → `~/.local/share/litellm/restart-controller-44ac311`
   (only that one field; digest becomes `7e9fe0ed…`)

4. Fresh install from the kit (creates bundle + supervisor plist, verifies bytes):
```sh
~/Projects/work/flex-ai-litellm-local-proxy/.venv-tests/bin/python -B <worktree>/core/install_restart_controller.py --policy ~/.config/litellm-restart-controller/policy.json
```
   Expect `{"status": "installed", ...}`. Then verify:
```sh
~/Projects/work/flex-ai-litellm-local-proxy/.venv-tests/bin/python -B <worktree>/core/install_restart_controller.py --policy ~/.config/litellm-restart-controller/policy.json --verify
```
   Expect `{"status": "verified", ...}`.

5. Bootstrap the new daemon (first boot of the new generation):
```sh
launchctl bootstrap gui/502 ~/Library/LaunchAgents/ai.litellm.restart-controller.plist
```

6. Arm + kickstart as ONE paste (the 5-minute arm window does not survive a
   chat round-trip; lane runs at serve boot):
```sh
~/Projects/work/flex-ai-litellm-local-proxy/.venv-tests/bin/python -B <worktree>/core/restart_controller.py --policy ~/.config/litellm-restart-controller/policy.json arm-canary && launchctl kickstart -k gui/502/ai.litellm.restart-controller && echo ARM-AND-KICKSTART-DONE
```

Then tell the session: it polls the lane to `canary_passed`, reads the new
epoch/identity, and prepares the reissue snippet (you issue the file, per the
gate-5 boundary). After the record verifies, the session runs prepare → you
approve in Discord → first live controlled restart → verify.

## What the reissued record will bind

- policy_digest `7e9fe0ed…` (after step 3)
- controller_digest `44ac3115…` (post-A1+A2 closure — matches installed bytes)
- new controller_epoch + controller_identity from the FINAL incarnation
- plist digests unchanged: proxy `8c92053a…`, supervisor `d0a132c5…`
- fresh canary evidence digest from the new lane (all other evidence fields
  re-bound by the lane itself)

## Notes

- The proxy (`ai.litellm.local-proxy`, :4000) is never touched by any step
  above; it keeps serving through the whole refresh.
- The current qualification.json binds the STALE generation — it is expected
  to refuse under the new policy; the session will archive it (never delete)
  before you issue the fresh record.
