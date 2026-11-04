# Managed macOS service installation

This guide covers the separately specified shared deployment core (spec 015).
The older routing experiment in the root README is not an activation procedure.
Use a reviewed deployment repository; do not install directly from a template or
use this guide to bypass a workstation's operator/EDR approval boundary.

## Source and runtime contract

- The kit's `core/` and `launchd/` files are shared source.
- A separate deployment repo supplies root `deploy.conf.sh` and
  `deployments/<name>/{overlay.yaml,credentials.toml,pin.lock}`.
- `deploy.conf.sh` sets the deployment name, config/log apply targets, unique
  label/port, pinned Python interpreter and Keychain service/account names.
- `credentials.toml` contains **credential sources and references only**, never
  values. Sources are `op` (1Password `op_ref`), `aws-sso-shim` (the documented
  embedding-filter degrade path), and `cli-token` (the closed, 30-second
  `<resolver> auth print-token` form for a scoped MCP relay key). Non-`op`
  sources forbid `op_ref`; no arbitrary command source is accepted.
  A login-Keychain item holds the bootstrap service-account token. The wrapper
  resolves credentials in memory, fails closed when required credentials are
  unavailable and asserts the installed patch sentinel before launching.
  The installer checks rendered credential coverage **before changing any
  apply target**, so an undeclared relay key cannot become an installed job.
- `overlay.yaml` may have a `runtime_environment` mapping. Only `DATABASE_URL`,
  `LITELLM_MCP_STDIO_EXTRA_COMMANDS`, and `LITELLM_ENABLE_MCP_STDIO` are
  accepted. The stdio flag must be quoted literal `'true'` or `'false'`; enabling
  it is the reviewed opt-in that lets the proxy start configured stdio children.
  must be password-free PostgreSQL without query/fragment; use the credential
  manifest to resolve a credential-bearing URL into `DATABASE_URL` instead.
- List additional MCP stdio executable **basenames** as a comma-separated value.
  This controls executable admission, not server or tool authorization. Preserve
  only commands required by the reviewed deployment.
- The renderer never reads live state. Check old/new runtime environment parity
  separately from YAML parity: omitting the database disables persistent MCP
  OAuth state, virtual-key lookup and usage accounting; omitting the allowlist
  can leave stdio servers forbidden even when the proxy health endpoint is green.

Example overlay fragment (non-secret, illustrative identity):

```yaml
runtime_environment:
  DATABASE_URL: postgresql://localuser@127.0.0.1:5432/local_proxy
  LITELLM_MCP_STDIO_EXTRA_COMMANDS: example-mcp-command
  LITELLM_ENABLE_MCP_STDIO: 'true'
```

## Prepare and rehearse

Use an isolated `uv` environment for the pinned build. Install the deployment's
reviewed extras (the proxy commonly needs `[proxy]`). DB-backed installations
also require the compatible Prisma package and generated client for the pinned
schema. Run code generation with that venv's `bin` directory on PATH.

Before replacing a service, back up its plist and all entry points you will
retire. Keep credential backups in secure storage, not plaintext scratch copies.
A rollback must restore both the old plist and every script it calls; an old
venv alone is not a rollback when its editable source checkout has changed.

Rehearse **the new managed wrapper and generated plist environment**, not merely
the old wrapper with the new interpreter, on an unused loopback port. Explicitly
provide HOME, PATH and the user runtime-directory variables when building a
minimal manual environment. Do not treat one working environment as proof that
a particular variable caused an earlier authentication failure.

Require health 200, invalid-key **401**, a real authenticated completion and a
streamed tool call with exactly-once reconstructed arguments. A 400 response
saying `No connected db` is a runtime fault, **not** an auth rejection. Use the
normal reviewed local database only for an authorized deployment rehearsal;
use isolated mutable state for unrelated experiments. Clean up only the known
rehearsal process group, never a broad process-name match.

## Stage files (does not reload launchd)

Set these variables to the reviewed repositories; the installer argument is the
**deployment repo root**, not its `deployments/<name>` subdirectory:

```bash
KIT=/path/to/shared-kit
DEPLOYMENT_REPO=/path/to/deployment-repo
bash "$KIT/core/installer.sh" "$DEPLOYMENT_REPO" --dry-run
bash "$KIT/core/installer.sh" "$DEPLOYMENT_REPO" apply
bash "$KIT/core/installer.sh" "$DEPLOYMENT_REPO" --verify
```

Apply stages the rendered YAML, both plists, wrapper, parser, sentinel checker,
rotation helper, **credentials.toml**, **pin.lock** and the coverage checker
into the declared config directory. `run-proxy-wrapper.sh [PORT] --preflight`
resolves the actual manifest and verifies coverage, master-key evidence,
sentinel and pin provenance without starting a listener or stopping a process.
Run it with the clean generated plist environment, not inherited agent keys.
See [the restart contract](safe-restart-contract.md) for the handoff and
security-boundary gates; a successful preflight is not authority to kill the
current service.
It retires the legacy `run-local-proxy.sh` and `launchd-wrapper.sh` into the log
directory. Dry-run must not create apply-target directories; verify must detect
a missing or changed manifest. Neither apply nor verify loads launchd jobs.

Install both generated plists into the user's LaunchAgents directory, then lint
and compare their bytes. Values below come from `deploy.conf.sh`; do not guess
the label or config directory:

```bash
CONFIG_DIR=/path/to/declared/config-dir
LABEL=example.local-proxy
install -m 644 "$CONFIG_DIR/$LABEL.plist" "$HOME/Library/LaunchAgents/$LABEL.plist"
install -m 644 "$CONFIG_DIR/$LABEL.rotate.plist" "$HOME/Library/LaunchAgents/$LABEL.rotate.plist"
plutil -lint "$HOME/Library/LaunchAgents/$LABEL.plist"
cmp "$CONFIG_DIR/$LABEL.plist" "$HOME/Library/LaunchAgents/$LABEL.plist"
```

Coordinate retirement and reload: a still-loaded legacy job retains its old
ProgramArguments, even after its plist on disk changes. It can therefore try to
restart a retired script. Do not leave this transition unattended.

## Operator reload and verification

On an operator-owned workstation, the **operator** runs:

```bash
launchctl bootout "gui/$(id -u)/$LABEL" &&
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$LABEL.plist"
```

Stop if bootout fails; inspect registration rather than falling back to legacy
`load`/`unload`. Reload the separate rotation job if its loaded definition also
changed. A file replacement or `kickstart` does not reload plist environment.

Check `launchctl print`, the actual process command line, fresh wrapper logs,
health, bad-key 401 and real inference again on the primary port. Confirm DB
connectivity and each required MCP server's access separately. A green sentinel
only proves symbol presence in an importable module; it does **not** verify every
commit in `pin.lock` or the absence of all streaming regressions.

`KeepAlive.SuccessfulExit=false` restarts nonzero exits **including EX_CONFIG
(78)**. `ThrottleInterval=30` bounds the retry rate, not the retry count. The
reported last exit code may remain 78 after a healthy process starts; inspect
current state, PID and readiness rather than interpreting that field alone.

## Completion and rollback

Do not declare the swap complete on liveness alone. Preserve DB/OAuth state and
salt-key continuity; verify required MCP access and consuming agents, and update
operational references to the rendered config and managed wrapper only when
those are the active files. Keep unrelated routing experiments disabled.

A true rollback restores the **previous build**, not just the previous wrapper:
re-point `LITELLM_PROXY_PYTHON` at a venv built from a real checkout of the old
version (matching `pin.lock` provenance, which `check_pin.py` enforces), then
bootstrap the backed-up plist. A venv whose editable install points at the
*current* worktree will pass the old sentinel (it checks a symbol, not
provenance) while silently running the new code with stale metadata — the
installer now rejects exactly this shape at Gate 2a.

If verification fails, the operator stops the new job, restores the captured
old plist **and its referenced scripts**, selects the matching source checkout
and dependency environment, and bootstraps the restored plist. Re-run the same
readiness, authentication and inference checks. Never claim rollback succeeded
without reading back the loaded job and testing its serving endpoint.
