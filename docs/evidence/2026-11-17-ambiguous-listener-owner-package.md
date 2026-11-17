# Owner package: controller bundle refresh (ambiguous_listener taxonomy fix)

Status: PREPARED (agent) — all agent-side verification below is live.
Audience: Phil (operator). Every launchctl/policy-edit/file-issuance step below is yours.

## Why a second refresh

The `lpkit-976` prepare failures surfaced as `internal_error`, which masked
their real name: the daemon's `_listener` probe observed a transient
two-pid lsof reading on :4000 (the established proxy connection plus a
second short-lived pid, e.g. the diagnostic sampler itself) and raised
`ambiguous_listener` — a deliberate runtime refusal whose code was never
added to the engine's `CODES` vocabulary. `Fault.__init__` coerces unknown
codes to `internal_error`, so the operator saw an anonymous internal error
instead of a diagnosable listener ambiguity.

Fix shipped to kit HEAD (pushed, `feat/controller-shutdown`):

- `7f77e8e` `fix(engine)`: `ambiguous_listener` added to `CODES`, plus a
  structural taxonomy test (`tests/test_controller_fault_taxonomy.py`)
  that sweeps every `Fault('name')` literal raised in `core/restart_control/`
  against `CODES` — an undeclared code now fails the suite, not the operator.
- `10be9c0` `docs(operations)`: `ambiguous_listener` listed with the
  listener-preservation refusals.

Full suite: 1191 passed / 14 subtests (with the 2 new taxonomy tests).
Note: `test_scope_directory_is_derived_and_private` fails when TMPDIR is the
macOS system default (`/var/...` — `/var` is a symlink and the privacy
validator refuses symlinked ancestors). It is environmental; run the suite
with a non-symlinked TMPDIR (e.g. the Hermes scratch dir).

This is a new controller generation: the installed bundle
(`restart-controller-44ac311`) now mismatches kit HEAD (`engine.py` is a
CONTROLLER_INPUTS source), so every future prepare would refuse
`controller_copy_changed` → `runtime_invalid` until refreshed.

Live facts (verified 2026-11-17, this session):

- Post-fix controller closure: `5d93309b2c06a01e470d99369e93ec3200d5038dfb55d66b78ecf288e08b57bc`
- New bundle dir: `~/.local/share/litellm/restart-controller-5d93309`
- Policy digest AFTER the `controller_root` edit: `f288e6640feae3bfdcb9b451f5a92c21304b7153cf3acaa975ca990bdc2b9591`
- Installer is fresh-install-only (refuses existing destination/plist).

## Owner sequence (in order; step 3 before step 4 — install validates the final policy)

1. Deactivate the controller (leaves the proxy on :4000 serving):

```sh
launchctl bootout gui/502/ai.litellm.restart-controller
```

2. Retire the old bundle + plist (archive, never delete — naming precedent:
   `restart-controller-f7cf523.retired-20261117`):

```sh
mv ~/.local/share/litellm/restart-controller-44ac311 ~/.local/share/litellm/restart-controller-44ac311.retired-20261117
mv ~/Library/LaunchAgents/ai.litellm.restart-controller.plist ~/Library/LaunchAgents/ai.litellm.restart-controller.plist.retired-20261117b
```

3. Edit `~/.config/litellm-restart-controller/policy.json`:
   `controller_root` → `/Users/pjbeyer/.local/share/litellm/restart-controller-5d93309`
   (only that one field; digest becomes `f288e664…`)

4. Fresh install from the kit (creates bundle + supervisor plist, verifies bytes):

```sh
/Users/pjbeyer/Projects/work/flex-ai-litellm-local-proxy/.venv-tests/bin/python -B /Users/pjbeyer/Projects/pjbeyer/litellm-local-proxy-shutdown/core/install_restart_controller.py --policy ~/.config/litellm-restart-controller/policy.json
```

   Expect `{"status": "installed", ...}`. Then verify:

```sh
/Users/pjbeyer/Projects/work/flex-ai-litellm-local-proxy/.venv-tests/bin/python -B /Users/pjbeyer/Projects/pjbeyer/litellm-local-proxy-shutdown/core/install_restart_controller.py --policy ~/.config/litellm-restart-controller/policy.json --verify
```

   Expect `{"status": "verified", ...}`.

5. Bootstrap the new daemon (first boot of the new generation):

```sh
launchctl bootstrap gui/502 ~/Library/LaunchAgents/ai.litellm.restart-controller.plist
```

6. Arm + kickstart as ONE paste (the 5-minute arm window does not survive a
   chat round-trip; the lane runs at serve boot):

```sh
/Users/pjbeyer/Projects/work/flex-ai-litellm-local-proxy/.venv-tests/bin/python -B /Users/pjbeyer/Projects/pjbeyer/litellm-local-proxy-shutdown/core/restart_controller.py --policy ~/.config/litellm-restart-controller/policy.json arm-canary && launchctl kickstart -k gui/502/ai.litellm.restart-controller && echo ARM-AND-KICKSTART-DONE
```

Then tell the session: it polls the lane to `canary_passed`, reads the new
epoch/identity, and prepares the reissue snippet (you issue the file, per
the gate-5 boundary). After the record verifies, the session runs prepare →
you approve in Discord → first live controlled restart → verify.

## What the reissued record will bind

- policy_digest `f288e664…` (after step 3)
- controller_digest `5d93309b…` (post-taxonomy-fix closure — matches
  installed bytes, enforced by the A2 probe)
- new controller_epoch + controller_identity from the FINAL incarnation
- fresh canary evidence digest from the new lane

## Notes

- The proxy (`ai.litellm.local-proxy`, :4000, pid 47263) is never touched by
  any step above; it keeps serving through the whole refresh.
- The current qualification.json binds the previous generation — expected to
  refuse under the new policy; the session archives it (never delete) before
  you issue the fresh record.
- After the refresh, a prepare that hits the transient two-pid listener
  window will refuse `ambiguous_listener` BY NAME (retryable once the window
  closes) instead of `internal_error` — that is the fix working as designed.
