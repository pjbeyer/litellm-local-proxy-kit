# Safe local proxy restart contract

This is an additive implementation contract for the existing managed-deployment
Contract 1 (credential manifest) and installer. It does not initialize or repoint
a separate Spec Kit workspace. The primary deployment specification remains
spec 015 in its owning workspace. Tracking remains in the external tracker.

## Requirements

- CR-001: `source = "cli-token"` is a closed credential source, not an
  arbitrary command specification. It executes `<resolver> auth print-token` with a
  30-second timeout, accepts only a successful nonempty single-line credential,
  and never prints the resolved value. Required resolution failure exits 78.
  `op_ref` is forbidden for this source. The scoped relay key is not replaced
  with the ordinary gateway key.
- CR-002: `run-proxy-wrapper.sh --preflight` runs the actual credential,
  coverage, master-key, patch-sentinel and pinned-build checks without creating
  a listener or stopping an existing service. Unknown arguments fail closed.
- CR-003: Installer staging validates every referenced credential against the
  manifest before applying any files. A missing credential source/row refuses
  installation, rather than producing a crash-looping service.
- CR-004: Managed (label-bearing) wrapper startup refuses an occupied or
  indeterminate loopback port before credential/MCP/DB initialization. This
  preserves an owner-authorized bridge during managed-file repair; it is a
  pre-check, not an atomic socket reservation. The final HTTP bind remains the
  operating system's arbitration point. Preflight does not bind the port.
- RS-001: A restart request is serialized, bound to exact installed asset
  digests, and fails without touching the current listener on preflight error,
  drift, unavailable service control, unknown listener ownership or stale
  authorization. Discover UID dynamically.
- RS-002: Before disruptive service control, exercise the replacement's real
  startup and bounded readiness/auth/model acceptance on a candidate port.
  The cold-start budget is at least 180 seconds. Parallel candidate use of the
  production DB can create connection/cache/spend state; it is not a zero-write
  probe and must not be called state-isolated. Do not replay business writes.
- RS-003: Graceful, bounded handoff and a tested last-known-good rollback are
  required. Never implement restart as raw SIGKILL or depend on KeepAlive as
  evidence of successful boot. A launchctl refusal ends the operation; never
  switch execution paths to evade it. A temporary owner-authorized bridge is
  distinct from managed restoration.
- RS-004: A Hermes tool policy can block recognized dangerous commands, but
  cannot provide process isolation against arbitrary same-UID Python/shell.
  Do not describe a regex policy as an unbypassable control. A hard isolation
  guarantee requires an OS-enforced execution boundary or a differently owned
  service, plus a narrow independently supervised controller. Installation of
  a new privilege boundary requires owner review of its concrete design.

## Acceptance

Offline tests cover each credential-source failure, installed-layout parity,
missing-row installer refusal and preflight with no launcher invocation.
Controller tests must prove original-service survival on failed preflight and
exercise success, failed candidate, failed post-check, rollback, duplicate
requests, wrong UID, stale digest and denied service control. Production
restart is not accepted by mock results alone.

## Selected bounded command-guard contract

The owner selected practical command-pattern protection, accepting that it is
not OS-enforced isolation. That choice permits the profile-local guard below;
it does not waive controller, candidate, handoff or rollback acceptance.

- CG-001: Register a `pre_tool_call` veto; never auto-approve or use fail-open
  execution middleware as a security gate. Ordinary file reads and read-only
  service diagnostics must remain available.
- CG-002: Refuse recognizable process signals targeting the current listener or
  its process group, broad proxy/interpreter selectors, protected port kill
  pipelines, launchd mutations of protected labels/domains, and mutations of
  configured session-owned bridge handles. Evaluate hazards across compound
  commands and supported inline/deferred wrappers.
- CG-003: Discover listener ownership read-only with bounded subprocesses; do
  not cache a startup PID. Invalid/incomplete discovery is not an empty result.
  Missing/invalid policy fails closed for executable calls. Return only fixed
  sanitized diagnostics; never include command text, credentials or process
  environment in a denial.
- CG-004: Install only in the explicitly authorized Hermes profile. Reuse the
  kit's source and native plugin enable/reload path; verify live activation,
  source/deployed byte parity, safe denied-call probes, permitted diagnostics,
  and survival of the original listener. Do not test by signaling production.
- CG-005: Explicit residual risk includes arbitrary same-user code, imported or
  encoded programs, unsupported shell grammar/aliases/tools, mutable policy or
  plugin files, other profiles/agents and discovery-to-execution races. Plugin
  disabled/import failure means the guard is absent. Do not describe this as
  sandboxing, a capability boundary, or a complete restart controller.

The optional implementation and installation/rollback guide live in
[`integrations/hermes/proxy-lifecycle-guard`](../integrations/hermes/proxy-lifecycle-guard/README.md),
including the rev-3 refusal-category reference (one category + stable
LGV code per decision, plugin-emitted log lines) and the full honest-boundary
enumeration. Production restart remains separately gated; guard installation
must not stop or replace the currently serving bridge.

## Delivery status

Credential parity/preflight and controller isolation are separate gates. Do not
close the combined issue or mark managed service restored while either the
production handoff or the enforcement boundary remains unverified.
