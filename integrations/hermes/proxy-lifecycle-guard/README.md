# Practical proxy lifecycle guard for Hermes

This optional adapter reuses the kit's existing deployment ownership and adds a
`pre_tool_call` veto. It does not install a service, expose a restart tool, resolve
credentials, modify the model prompt, or change proxy configuration. Installing
it never requires restarting either the proxy or the Hermes gateway.

## What is protected (in scope)

Recognizable lifecycle signals against the current port owner/process group,
proxy-wide selectors, port-based kill pipelines, protected launchd labels, and
protected background-process handles are blocked before the tool executes.
Compound commands, ordinary shell quoting and command substitutions, common
command wrappers, control-flow-wrapped bodies, inline shell/Python commands,
and nested deferred-tool calls are inspected. Read-only process/service/health
diagnostics pass.

In-scope tools: `terminal`, `execute_code`, `browser_exec`, `tool_call`
(envelopes unwrapped recursively), and mutating `process_manage` actions.
Unresolved kill substitutions, xargs kill pipelines, and regex process
selectors are deliberately refused rather than assuming that a different port
or an opaque stdin producer proves unrelated ownership.

## Refusal reference (rev-3 vocabulary)

Every block is one decision with a category and a stable log code. The plugin
emits its own log line per decision (category + code) so distinguishability
does not depend on runtime surfacing.

- **VETO — LGV-001**: recognized lifecycle threat. Fixed sanitized message
  naming the owner-reviewed restart workflow.
- **BOUND_SIZE — LGV-010 / BOUND_DEPTH — LGV-011 / BOUND_COUNT — LGV-012**:
  input too large / nesting too deep / batch too large to prove safe. Each
  carries corrective guidance (reduce and resubmit); large-but-innocent inputs
  that the bounded scan proves safe are allowed.
- **SHAPE_ENVELOPE — LGV-020**: deferred-call envelope that could not be
  proven safe.
- **POLICY_INVALID — LGV-030**: policy missing, unreadable, schema-invalid,
  tampered (digest mismatch), or deployed source diverged from the
  owner-recorded digests. Read-only tools still pass (fail-closed split).
- **HOOK_MISMATCH — LGV-031**: call could not be attributed to a known tool
  (unknown or nameless tool name carrying executable-looking arguments).
- **INTEGRITY_REF_INVALID — LGV-032**: the owner-established
  `integrity.sha256` reference is missing/unreadable — must be re-established
  by the owner.

## Honest boundaries (FR-012 — read this)

This is **bounded accident prevention, not process isolation**, and not an
authorization boundary. Bypass classes, by design:

- Imported scripts, encoded or generated programs, and Python primitive
  aliasing (binding `os.kill` to another name) are outside static inspection.
- Other execution tools not listed above, and arbitrary same-user code, can
  signal processes directly.
- Another agent/profile, or any terminal outside this enabled Hermes profile,
  is not protected.
- Unsupported shell grammar is NOT a blanket bypass: grammar the matcher
  cannot classify is refused conservatively, not allowed.
- **Discovery-to-execution race**: PID discovery is a read-only snapshot; the
  port owner can change between discovery and signal execution. For deferred
  `tool_call` envelopes the race window extends from envelope submission to
  the inner call's actual execution.
- **Restart-transition gap**: protection exists only while the configured
  listener is bound; while a restart unbinds the listener, discovery answers
  empty and port-targeted protection lapses.

## Profile-local installation

1. Review and test this directory from the kit checkout. Run
   `hermes plugins validate <plugin-directory> --json`, then the test suite
   (`python3 -m unittest tests.test_lifecycle_guard` from the repo root —
   includes the loopback listener fixture and mutation checks; repo bytes are
   never modified).
2. Copy `__init__.py`, `guard.py`, and `plugin.yaml` byte-for-byte to
   `$HERMES_HOME/plugins/proxy-lifecycle-guard/` in the authorized profile
   only. Parity with the reviewed kit source is the evidence of deployment;
   a divergent deployed copy is treated as uninstalled (fail-closed at the
   next policy load).
3. Author `config.json` **in the installed directory** (never the checkout)
   using the rev-3 schema — every key is required, no extras:

   ```json
   {
     "schema_version": 1,
     "protected_port": 4000,
     "protected_labels": ["example.litellm"],
     "protected_session_handles": ["owner-authorized-bridge-handle"],
     "bounds": {
       "input_text_bytes": 262144,
       "max_chunks": 512,
       "max_depth": 8,
       "discovery_timeout_ms": 2000,
       "max_snapshot_pids": 256
     },
     "refusal_vocabulary_version": 1
   }
   ```

   Derive the real label from the deployment's installed plist; obtain any
   bridge handle from the owning tool process. Bounds are capped by the
   schema: a valid policy cannot configure the guard past its hook budget.
   These are environment-specific, non-secret values — never commit them.
4. Set owner-only permissions (no group/world write) on `config.json`.
5. Establish the integrity reference (owner action): compute SHA-256 of the
   policy and of each deployed source file (`guard.py`, `__init__.py`) and
   write them to `integrity.sha256` beside the policy:

   ```json
   {
     "policy_digest": "<sha256 of config.json>",
     "source_digests": {"guard.py": "<sha256>", "__init__.py": "<sha256>"}
   }
   ```

   Set owner-only permissions on it too. The guard re-reads and re-verifies
   both on EVERY evaluation: tampering with the policy or the deployed
   source fails closed on the next call.

6. Enable with `hermes plugins enable proxy-lifecycle-guard`. This supported
   command reloads plugins in a responding gateway; confirm the plugin status
   shows enabled AND loaded — the saved enable flag alone does not prove
   live activation. Do not restart the proxy.
7. Verify a live denial safely: a marker followed by `false &&` and a
   recognized protected lifecycle command, e.g.
   `echo guard-probe-marker && false && pkill -f litellm`. The guard must
   veto the entire tool call (no marker execution, LGV-001 in the logs);
   the `false &&` branch keeps the probe non-destructive even if the hook
   is missing. Also verify read-only diagnostics pass and the listener and
   readiness are unchanged. Never test by actually signaling the proxy.

### Upgrading an existing installation (order matters)

A running gateway holds the guard module in memory and re-reads
`config.json` on every call. Swapping a config whose schema the LOADED
module cannot parse fail-closes every execution tool until the module is
reloaded — the correct order is:

1. `hermes plugins disable proxy-lifecycle-guard` — unloads the module
   from the running gateway (this is the reload mechanism; the saved
   enable flag alone is not). **Run this from an owner shell OUTSIDE the
   running gateway.** F-017 deliberately vetoes agent-mediated
   disable/uninstall of a live guard from inside the gateway (that is
   accident/persuasion protection working as specified) — an agent
   session that attempts step 1 will be refused and must not treat the
   refusal as a defect (see lpkit-vfz).
2. Copy the new source files and author the new `config.json` +
   `integrity.sha256` (owner actions) while the guard is unloaded. If the
   file swap is performed while the old module is still live (e.g. from an
   agent session, before step 1), every execution tool fail-closes with
   POLICY_INVALID/LGV-030 until bytes and reference match again — a
   2026-11-04-class window blocked 5 unrelated sessions for ~2 minutes.
3. `hermes plugins enable proxy-lifecycle-guard` — loads the new module
   against the new config.
4. Run the verification probe (step 7 above) to confirm the new module
   is live.

**Agent-session path (no owner shell available):** the documented
disable→swap→enable order is not executable from inside the gateway —
`enable` only flips the saved flag and does not reload an already-loaded
module, and `update` requires a git origin this standalone deployment
lacks. The only in-gateway activation path is an **owner gateway
restart** (`hermes gateway restart` from a separate shell — this
restarts Hermes, not the port-4000 proxy). Until that restart, swap
bytes + re-materialize `integrity.sha256` to match so the on-disk state
is consistent (avoiding the POLICY_INVALID window in step 2), then ask
the owner to restart.

## Probe watchdog (deployment monitor)

The kit ships `guard_probe_watchdog.py` in this directory. The runtime
copy lives at `~/.hermes/scripts/guard_probe_watchdog.py` (mirrored
byte-for-byte; parity is the evidence the reviewed repo copy matches
what the cron job executes) and is run by Hermes cron job
`8928a8340f33` every 4h as a no-agent script job.

It verifies the deployed installation read-only on every run:

- plugin directory presence and FR-009 owner-only permissions,
- FR-015 integrity digests (recomputed against `integrity.sha256`),
- live VETO of a non-executing lifecycle probe command (dead-branch
  construction, FR-010),
- allow of a read-only diagnostic (false-positive tripwire),
- shape-class routing (lpkit-9df): decode-fail envelopes
  → SHAPE_ENVELOPE/LGV-020 (lpkit-1cz class), non-canonical `tool`/
  `args` key shapes → SHAPE_ENVELOPE (amendment 3a, lpkit-6u6 class),
  and a canonical relay envelope → ALLOW (over-blocking tripwire).

Silent on health (exit 0); any probe that cannot be proven healthy is
an alert (exit 1) with the failing classes named. Per-run decisions are
appended to `probe_history.jsonl` under
`$XDG_STATE_HOME/hermes/guard-probe-watchdog/` (all five decision
categories + integrity/perms verdicts). Run the self-test with
`python3 guard_probe_watchdog.py --self-test` (pure-logic assertions,
no guard import, no state writes). Mirroring rule: change the repo copy
and the `~/.hermes/scripts/` copy in the same change — byte parity is
the deployment contract.

## Operating, disabling, rollback

A denial is not an outage or permission to retry through a different tool.
Use the owner-reviewed restart workflow; until a real supervised controller,
handoff, and rollback are delivered, they remain separate outstanding
requirements.

An owner can disable this plugin with `hermes plugins disable
proxy-lifecycle-guard`; verify live unload and record that accident
protection is then ABSENT. This removes only the guard and must not stop
the proxy. **Disabling the guard to make a blocked lifecycle command pass is
forbidden** — if a command was vetoed, the answer is the owner-reviewed
workflow, not removing the guard.
