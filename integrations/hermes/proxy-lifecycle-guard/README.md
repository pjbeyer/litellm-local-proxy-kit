# Proxy lifecycle guard for Hermes

This profile-local plugin blocks recognizable lifecycle operations against the
protected LiteLLM proxy before Hermes runs them. It is accident prevention,
not process isolation or restart authorization. Read-only diagnostics can pass.
A refusal is not permission to retry through another tool. Use the
[owner-reviewed restart workflow](../../../docs/restart-controller-operations.md)
when the proxy actually needs a restart.

## Routine maintenance: respond to alerts, not a checklist

The existing Hermes cron job runs `guard_probe_watchdog.py` without an agent.
It is silent when healthy. It checks installed files, permissions, integrity,
and allow/refuse behavior by loading the installed evaluator in a **separate
process**. It also checks that this process delivered its own fresh sanitized
refusal record. This does **not** verify the running gateway's hook, callback
identity, or sink worker, and does not prove storage durability.

On alert, check the cron job's last run and failing check. Resolve the actual
script and target profile from the job configuration; the script defaults to
`~/.hermes/plugins/proxy-lifecycle-guard`, not `$HERMES_HOME`. Set
`GUARD_PROBE_DEPLOY_DIR` explicitly for a different target. Compare the
scheduled script with the reviewed copy using `cmp`. The scheduled copy lives
beside the Hermes cron helpers `cron_output_sanitizer` and `network_health`.
A mismatch at the scheduled path is a runtime failure; an unused copy beside
the plugin is distribution drift. Reconcile either only through the owner’s
installation workflow. If the scheduler path or workflow is unclear, record
the drift and stop before activation.

For a refusal investigation, use the category and code to triage, then recover
exact tool arguments from the authorized session record if needed. The sink
contains no arguments. Reproduce unexpected decisions only in isolated tests
with the policy, environment and discovery inputs pinned; don't replay a
refused operation live or add raw arguments to shared logs.

## Verify a source change

From the repository root, use the [kit's offline test setup](../../../README.md#validation-boundary).
The focused loop includes the evaluator, its hook and the watchdog/sink tests;
run the complete kit suite before staging a release. Use a short, resolved
scratch path for `TMPDIR` (Darwin socket paths are limited), and a separate
`XDG_STATE_HOME` under that disposable path. Keep the scratch directory
private and use the normal test umask (`022`), not `077`.

```sh
# In the isolated test environment, with TMPDIR and XDG_STATE_HOME set as above:
.venv-tests/bin/python -m pytest -q tests/test_lifecycle_guard.py tests/test_guard_*.py tests/test_sink_*.py
.venv-tests/bin/python -m pytest -q tests
hermes plugins validate integrations/hermes/proxy-lifecycle-guard --json
```

Run plugin validation in a disposable Hermes profile. For a release, record
its full Git commit and SHA-256 digests of `guard.py`, `__init__.py`, and
`plugin.yaml`; include the watchdog digest if it changes. Compare the live
sink before and after the isolated release gate; investigate any unexplained
delta instead of claiming isolation. Review behavior changes independently.
A passing source gate does not authorize activation.

## Install, upgrade, or roll back (owner only)

1. In an owner shell outside the gateway, stage only the reviewed files from
   the pinned commit and resolve the intended profile's actual plugin
   directory (`$HERMES_HOME/plugins/proxy-lifecycle-guard/` for that profile).
   For upgrades, verify the current tuple against the expected predecessor;
   stop on drift. Back up the installed files, policy, integrity reference,
   and enable setting privately. Only include the watchdog in the backup and
   cutover if its scheduled copy changes.
2. At the approved Hermes maintenance window, stop the **Hermes gateway** and
   keep it stopped until installation is consistent. Do not stop or restart
   LiteLLM. Copy `guard.py`, `__init__.py`, and `plugin.yaml` byte-for-byte.
   Preserve `config.json` unless a policy change was separately reviewed; for
   a new install, use the example below. The owner records matching digests
   in `integrity.sha256`. Keep both files owner-owned with no group/world
   write permission. Verify installed file parity, policy and reference
   before starting Hermes. Don't swap files while the old hook is serving.
3. For a new install, enable with `hermes plugins enable proxy-lifecycle-guard`
   for the intended profile. Preserve the enable setting for upgrades. Start
   Hermes and verify the plugin is loaded in the serving context; a saved
   enable flag or a CLI discovery result isn't enough. Verify a harmless
   allow and an owner-approved, **non-executing** refusal through the live
   hook. Confirm the refusal reaches the live sink, proxy readiness is
   unchanged, and the scheduled watchdog is healthy. Never actually signal
   the proxy as a probe. Record disk parity and observed live behavior
   separately; neither establishes the exact serving callback digest.
4. If verification fails, stop Hermes again and restore the backed-up files,
   policy and matching integrity reference together. Restore the prior enable
   setting and scheduled script if changed. Verify consistency before
   starting Hermes, then repeat the applicable checks. Don't disable a live
   guard to make a refused action pass.

### New-install policy and integrity reference

Keep both files in the installed directory, never in Git. Replace the example
port and label with the actual listener and service label; populate the
session handle list only with handles owned by the protected deployment.

`config.json` (all keys required; no extras):

```json
{
  "schema_version": 1,
  "protected_port": 4000,
  "protected_labels": ["example.litellm"],
  "protected_session_handles": [],
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

`integrity.sha256` is JSON, not `shasum` output. From the installed directory,
use `shasum -a 256 config.json guard.py __init__.py` to get the digests:

```json
{
  "policy_digest": "<SHA-256 of config.json>",
  "source_digests": {"guard.py": "<SHA-256>", "__init__.py": "<SHA-256>"}
}
```

The guard rechecks policy and source digests on evaluation. The manifest is
checked separately by comparing it with the pinned release.

## Refusals and evidence

- `VETO` / `LGV-001`: a protected operation or an unresolved execution
  operand. It does not prove intent or a confirmed protected-target hit.
- `BOUND_SIZE` / `LGV-010`, `BOUND_DEPTH` / `LGV-011`, `BOUND_COUNT` /
  `LGV-012`: the input exceeded the corresponding analysis limit and could
  not be proven safe.
- `SHAPE_ENVELOPE` / `LGV-020`: an ambiguous or malformed call or program
  shape. Check the original input rather than inferring it from the log.
- `POLICY_INVALID` / `LGV-030` and `INTEGRITY_REF_INVALID` / `LGV-032`:
  investigate installed policy, permissions, integrity reference, and file
  parity. Only the owner repairs those files.
- `HOOK_MISMATCH` / `LGV-031`: the executable-looking call could not be
  attributed to a known tool.

The guard covers `terminal`, `execute_code`, `browser_exec`, `tool_call`, and
mutating `process_manage` actions. It does not restrict other profiles or
arbitrary same-user code, inspect every runtime-generated program, or guarantee
listener identity between discovery and execution. Read-only access is not a
confidentiality control.

Sanitized block records use the fixed `{ts, category, code, tool}` schema in
`${XDG_STATE_HOME:-$HOME/.local/state}/hermes/lifecycle-guard/decisions.jsonl`.
The [decision-sink contract](../../../docs/contracts/decision-sink.md) owns
queue, rotation, and file safety: one worker per loaded generation when cleanup
registration succeeds; bounded, nonblocking admission; no enforcement change
if telemetry drops. Watchdog history is in
`hermes/guard-probe-watchdog/probe_history.jsonl` under the same state base.

See the [incidence-reduction amendment](../../../docs/contracts/guard-incidence-reduction.md)
for requirements and the [release verification record](../../../docs/handoffs/ir007-synthetic-regression-evidence.md)
for historical evidence. Use this README, not a dated handoff, for the next
maintenance window.
