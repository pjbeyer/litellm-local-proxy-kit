# Owner package: controller bundle refresh (bind-before-rehearse fix) + initial proxy handoff

Status: PREPARED (agent) — all agent-side verification below is live.
Audience: Phil (operator). Every launchctl/policy-edit/file-issuance step below is yours.

## Why a third refresh

After the `ambiguous_listener` taxonomy fix shipped and the generation was
refreshed to bundle `5d93309`, a live prepare STILL refused `internal_error`.
Offline reproduction on a scratch copy of the live state tree isolated the
real defect (the taxonomy fix was correct but this second latent bug sat
behind it):

`_prepare` ran the candidate rehearsal (`rehearse`) BEFORE `bind_frozen`, so
the recovery store's `_PRE_FREEZE_PURPOSES` guard (candidate spawns require a
non-null asset digest) refused every rehearsal — `RecoveryError` → coerced to
`internal_error`. The store guard predates the rehearsal spawn (`62aa9b7`
Nov 3 vs `52f68f5` Nov 14), and the test backends never exercised the real
store path. **Every live prepare since Nov 14 was structurally doomed.**

Fix shipped to kit HEAD (`feat/controller-shutdown`, commit `1bb3b91`):

- `bind_frozen` moved ahead of `rehearse` (probe children are all closed by
  then, satisfying its closure precondition) — matches the plan's mandate
  "Fill both digests once freeze succeeds; never reuse null bindings for
  candidate execution".
- The refreshed ticket from `bind_frozen` is re-bound into the backend via
  `refresh_operation` (same contract as the canary lane).
- Interruption matrix updated: a shutdown latch inside the rehearse helper now
  leaves `['begin', 'frozen', 'finish']` durably recorded.

Verified offline on a scratch copy of the live state tree (no production
mutation): prepare now returns **`status: prepared`** end-to-end — freeze,
gates, primary acceptance, candidate rehearsal on bound digests, and LKG
snapshot all pass. Full suite: 1191 passed / 14 subtests.

This is a new controller generation (kit `engine.py` is a CONTROLLER_INPUTS
source), so bundle `5d93309` now mismatches kit HEAD.

Live facts (verified 2026-11-17, this session):

- Post-fix controller closure: `ad0bbcb07f269ed2e4d408bffd2a5409ce5d76fb9ae833a9c2090ede44fb91cb`
- New bundle dir: `~/.local/share/litellm/restart-controller-ad0bbcb0`
- Policy digest AFTER the `controller_root` edit: computed by the session
  after you do step 3 (it will be shown to you before step 4 — install
  validates against the final policy).
- Installer is fresh-install-only (refuses existing destination/plist).

## NEW: the `unmanaged_listener` finding (read first)

The offline repro also surfaced why the LIVE prepare was blocked even with
the ordering bug out of the way (it would have hit this next): the proxy on
:4000 (pid 47263) has **no `bootstrap.json` receipt** in the state root,
because it was never started through the controller's managed path. The
ops doc requires an operator-issued initial `bootstrap.json` binding version
1, UID, proxy label, listener PID/UID/birth, exact proxy-plist SHA-256 and
the `KeepAlive`/`ThrottleInterval`/`RunAtLoad` dictionary — the controller
writes this receipt itself only after its own subsequent successful start.

The receipt values are fully derivable; the session computed them and
verified offline that prepare passes once the receipt exists. Issuance is
owner-side (same boundary class as `qualification.json`). It is step 7.

## Owner sequence (in order)

1. Deactivate the controller (leaves the proxy on :4000 serving):

```sh
launchctl bootout gui/502/ai.litellm.restart-controller
```

2. Retire the old bundle + plist (archive, never delete):

```sh
mv ~/.local/share/litellm/restart-controller-5d93309 ~/.local/share/litellm/restart-controller-5d93309.retired-20261117
mv ~/Library/LaunchAgents/ai.litellm.restart-controller.plist ~/Library/LaunchAgents/ai.litellm.restart-controller.plist.retired-20261117c
```

3. Edit `~/.config/litellm-restart-controller/policy.json`:
   `controller_root` → `~/.local/share/litellm/restart-controller-ad0bbcb0`
   (only that one field; tell the session — it computes the new policy digest
   and gives you the exact `qualification.json` field values for step 8).

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
   chat round-trip; the lane runs at serve boot):

```sh
~/Projects/work/flex-ai-litellm-local-proxy/.venv-tests/bin/python -B <worktree>/core/restart_controller.py --policy ~/.config/litellm-restart-controller/policy.json arm-canary && launchctl kickstart -k gui/502/ai.litellm.restart-controller && echo ARM-AND-KICKSTART-DONE
```

7. Initial proxy handoff receipt (NEW step; owner-issued). After the daemon
   is up, tell the session — it computes the exact `bootstrap.json` content
   bound to the CURRENT live proxy identity and hands you a ready-to-paste
   heredoc. You write `~/.local/state/litellm/rc-v2/bootstrap.json` (0600).
   The proxy itself is NOT restarted for this; the receipt binds the
   current listener identity. (If the proxy's pid changed since 47263 the
   session recomputes automatically.)

8. Reissue qualification (owner, gate-5 boundary): the session prepares the
   self-locating reissue one-liner bound to the FINAL daemon incarnation
   (epoch/identity/policy/controller digests); you issue `qualification.json`.
   Archive the current record first (never delete) — the session prepares
   the archive move.

Then: the session runs prepare → posts request ID + digests → you approve in
Discord → first live controlled restart → session verifies (:4000 under a new
pid, same controller epoch).

## Notes

- The proxy (`ai.litellm.local-proxy`, :4000) is never touched by steps 1–6;
  it keeps serving through the whole refresh.
- Suite runs need a non-symlinked TMPDIR (privacy validator refuses symlinked
  ancestors) — environmental, known.
- After this refresh, a prepare that hits a transient two-pid listener window
  refuses `ambiguous_listener` BY NAME (retryable), and the previously
  doom-looping `internal_error` path is gone entirely.
