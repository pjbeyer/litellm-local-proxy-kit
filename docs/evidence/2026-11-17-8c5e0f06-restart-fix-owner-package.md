# Owner package: controller generation refresh `8c5e0f06` — restart freeze fix (2026-11-17)

Audience: Phil (owner). Everything below is owner-side; the session prepares,
verifies, and runs the RPC lane only. Proxy `ai.litellm.local-proxy` (:4000)
is never touched by steps 1–6.

## Why

Every controlled restart on generation `ad0bbcb0` refused
`{"code": "runtime_invalid"}`. Root cause (verified live, twice): the
restart path's pre-intent `freeze()` spawns the runtime-proof worker, but
the recovery journal admits a child intent only under a nonterminal
operation — and the restart stage deliberately has none open yet
(read-only until validation holds). The spawn refused, latched
`_cleanup_blocked` (poisoning the daemon until a kickstart), and freeze's
`except Exception` masked the whole thing as `runtime_invalid`.

Fix (kit commits `745507d` + `2514596`, pushed): restart revalidation now
calls `freeze_restart()`, which re-verifies every byte-level input (kit
facts, proxy receipts, deployment closure, policy digest, controller
copies) and reuses the prepare-stage worker-proven runtime digest only when
those inputs are byte-identical. Any drift falls back to the worker path
and fails closed. Full suite green: 30 + 19 + 462 + 680 passed.

## Live facts (computed this session from kit HEAD `2514596`)

- New controller closure digest (canonical JSON of CONTROLLER_INPUTS facts):
  `8c5e0f06ebf7ae8359b8b358a30b8231072127d63ba5e566cc0a62b49c6a4cba`
- New bundle dir: `~/.local/share/litellm/restart-controller-8c5e0f06`
- `engine.py`/`runtime.py` are CONTROLLER_INPUTS sources, so bundle
  `ad0bbcb0` now mismatches kit HEAD and cannot serve this fix.

## Owner sequence (in order)

1. Deactivate the controller (proxy keeps serving):

```sh
launchctl bootout gui/502/ai.litellm.restart-controller
```

2. Retire the old bundle + plist (archive, never delete):

```sh
mv ~/.local/share/litellm/restart-controller-ad0bbcb0 ~/.local/share/litellm/restart-controller-ad0bbcb0.retired-20261117
mv ~/Library/LaunchAgents/ai.litellm.restart-controller.plist ~/Library/LaunchAgents/ai.litellm.restart-controller.plist.retired-20261117d
```

3. Edit `~/.config/litellm-restart-controller/policy.json`:
   `controller_root` → `~/.local/share/litellm/restart-controller-8c5e0f06`
   (only that one field). Tell the session — it verifies the policy loads and
   gives the exact qualification values for step 8.

4. Fresh install from the kit (fresh-install-only; creates bundle + plist,
   verifies bytes):

```sh
~/Projects/work/flex-ai-litellm-local-proxy/.venv-tests/bin/python -B <worktree>/core/install_restart_controller.py --policy ~/.config/litellm-restart-controller/policy.json
```

   Expect `{"status": "installed", ...}`, then verify:

```sh
~/Projects/work/flex-ai-litellm-local-proxy/.venv-tests/bin/python -B <worktree>/core/install_restart_controller.py --policy ~/.config/litellm-restart-controller/policy.json --verify
```

   Expect `{"status": "verified", ...}`.

5. Bootstrap the new daemon:

```sh
launchctl bootstrap gui/502 ~/Library/LaunchAgents/ai.litellm.restart-controller.plist
```

6. Arm + kickstart as ONE paste (the 5-minute arm window does not survive a
   chat round-trip; the lane runs at serve boot):

```sh
~/Projects/work/flex-ai-litellm-local-proxy/.venv-tests/bin/python -B <worktree>/core/restart_controller.py --policy ~/.config/litellm-restart-controller/policy.json arm-canary && launchctl kickstart -k gui/502/ai.litellm.restart-controller && echo ARM-AND-KICKSTART-DONE
```

7. `bootstrap.json` already exists in the state root (issued 2026-11-17,
   binds the live proxy pid 47263). It survives the controller refresh
   unchanged — no re-issuance needed unless the proxy's pid changed.

8. Reissue qualification against the FINAL daemon incarnation: the session
   hands you a patched self-locating reissue script (same as
   `2026-11-17-ad0bbcb0-reissue.sh`, unique archive name, self-locating
   identity/epoch/evidence). You run it once after telling the session the
   kickstart landed.

Then: session runs prepare → posts request ID + digests → owner approves in
Discord (the session issues the approval file under that explicit consent,
window capped at the plan) → restart → session verifies :4000 under a new
pid, same controller epoch, status ok.

## Notes

- Steps reuse the proven sequence from the `ad0bbcb0` refresh verbatim;
  only the digest/dir suffix changed. The installer path in steps 4/6 is the
  KIT repo path (`core/install_restart_controller.py`, `core/restart_controller.py`).
- The retired `ad0bbcb0` bundle stays archived alongside `5d93309.retired-20261117`.
- If prepare refuses `ambiguous_listener` (two-pid transient on :4000), that
  is by-name retryable; wait for the drain to settle and re-prepare.
