# Practical proxy lifecycle guard for Hermes

This optional adapter reuses the kit's existing deployment ownership and adds a
`pre_tool_call` veto. It does not install a service, expose a restart tool, resolve
credentials, modify the model prompt, or change proxy configuration. Installing
it never requires restarting either the proxy or the Hermes gateway.

## Contract and limits

The supported protection level is **bounded accident prevention**, not process
isolation. Recognizable signals against the current port owner/process group,
proxy-wide selectors, port-based kill pipelines, protected launchd labels, and
protected background-process handles are blocked before the tool executes.
Compound commands, ordinary shell quoting and executable substitutions, common
command wrappers, inline shell/Python commands, and nested deferred-tool calls
are inspected. Protected process-handle prefixes and stdin closure are mutations,
not read-only operations. Read-only process/service/health diagnostics pass.
Unresolved kill substitutions, xargs kill pipelines and regex process selectors
are deliberately refused rather than assuming that a different port or an opaque
stdin producer proves unrelated ownership.

Only `terminal`, `execute_code`, `browser_exec`, `tool_call`, and protected
`process_manage` mutations are in scope. Imported scripts, encoded/generated
programs, aliases, unsupported shell grammar, other execution tools, policy/plugin
edits, and arbitrary same-user code can bypass this matcher. Another agent/profile
or a terminal outside this enabled Hermes profile is not protected. PID discovery
is a read-only snapshot with an unavoidable discovery-to-execution race. This is
not an authorization boundary and never grants a raw-kill exception.

The adapter uses the blocking `pre_tool_call` contract, not fail-open execution
middleware. Hook errors/timeouts are governed by the installed Hermes runtime;
missing/invalid local policy blocks executable calls while leaving file reads and
process status available. Import/registration failure or disabled plugin state
means no guard; monitor plugin load status, not just the saved enable flag.

## Profile-local installation

1. Review and test this directory from the kit checkout. Run
   `hermes plugins validate <plugin-directory> --json`.
2. Copy `__init__.py`, `guard.py`, and `plugin.yaml` byte-for-byte to
   `$HERMES_HOME/plugins/proxy-lifecycle-guard/` in the authorized profile only.
3. Create `config.json` **in that installed directory**, not in the checkout:

   ```json
   {
     "version": 1,
     "port": 4000,
     "labels": ["example.litellm"],
     "protected_sessions": ["owner-authorized-bridge-handle"]
   }
   ```

   Derive the real label from the deployment's installed plist; obtain any bridge
   handle from the owning tool process. This is non-secret local runtime metadata.
   The schema is strict and loaded on each callback; PID/PGID discovery uses bounded
   `lsof` and `ps` calls only when needed. Refresh `protected_sessions` when a new
   session-owned bridge is created. Listener PID protection does not need a saved
   PID. Do not put credential values or private deployment metadata in this repo.
4. Enable with `hermes plugins enable proxy-lifecycle-guard`. This supported command
   reloads plugins in a responding gateway; confirm that reload succeeded. Do not
   restart the proxy, and do not assume a saved enable flag proves live activation.
5. Verify a live denial safely: use a marker followed by `false &&` and a recognized
   protected lifecycle command. The guard must veto the entire tool call (no marker
   execution). The shell's false branch makes this probe non-destructive even if
   the hook is missing. Also verify read-only diagnostics pass and the original
   listener/readiness remain unchanged. Never test by actually signaling the proxy.

The tests include a disposable listener whose explicit stdin shutdown protocol
closes it without process signals, matcher matrices, malformed discovery refusal,
and genuine adapter registration. Mutation tests should disable a matching branch
or adapter registration in an isolated copy, demonstrate the expected behavioral
failure, and leave deployed/source bytes unchanged.

## Operating and rollback

A denial is not an outage or permission to retry through a different tool. Use the
owner-reviewed restart workflow; until a real supervised controller, handoff and
rollback are delivered, they remain separate outstanding requirements.

An owner can disable this plugin using `hermes plugins disable
proxy-lifecycle-guard`; verify live unload and record that accident protection is
then absent. This removes only the guard and must not stop the proxy. Never disable
it merely to make a blocked lifecycle command pass.
