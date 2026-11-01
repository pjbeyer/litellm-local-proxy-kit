# Safe local proxy restart contract

This is an additive implementation contract for the existing managed-deployment
Contract 1 (credential manifest) and installer. It does not initialize or repoint
a separate Spec Kit workspace. The primary deployment specification remains
spec 015 in its owning workspace. Tracking remains in the external tracker.

## Requirements

- CR-001: `source = "flexai-token"` is a closed credential source, not an
  arbitrary command specification. It executes `flexai auth print-token` with a
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

## Open owner decision

A same-user command guard prevents recognizable accidental lifecycle commands,
but arbitrary same-user Python or shell can bypass it. Choose explicitly between
that bounded protection (with a recorded residual risk) and a new OS-enforced
service identity/execution sandbox. Neither a weaker guarantee nor a new
privilege boundary is approved by silence. Until this choice is recorded,
the safe restart controller and its policy installation remain gated.
No controller or enforcement hook is installed by the credential repair.

## Delivery status

Credential parity/preflight and controller isolation are separate gates. Do not
close the combined issue or mark managed service restored while either the
production handoff or the enforcement boundary remains unverified.
