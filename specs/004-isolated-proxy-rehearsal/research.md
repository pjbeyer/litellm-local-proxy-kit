# Research: isolated proxy rehearsal

## Configuration checking is the routine default (operator decision, 2026-11-27)

**Decision:** Routine use is the configuration-check lane (`make rehearse-check`), which needs no guest, no VM image, no external drive, and no extra hardware on this machine. The isolated macOS guest remains the opt-in backend for exercising the actual candidate runtime, requested explicitly by name and never selected automatically or as a fallback. An external drive or a separate macOS runner may host the opt-in lane; neither is required for routine use.

**Rationale:** Owner confirmed that configuration checking is enough for routine use and declined to commit 40–60 GiB on this machine to rehearsal. This resolves the former T001 storage deadlock: the guest-lane feasibility work (T001) remains gated and opt-in, while the default workflow ships without it.

**Consequences:** The check result must be typed (`checked`, `runtime_not_exercised`) so it cannot be mistaken for rehearsal evidence; `make rehearse` must refuse, not downgrade, when the backend is absent. T001's storage blocker no longer gates the default path.

## Use an offline macOS guest for the first backend

**Decision:** Plan one Apple Silicon macOS VM with zero network devices, no shared folders, no host sockets or credential/share integration, and a local fixture dependency set. Attach a read-only input image and separate disposable guest state. A narrowly scoped virtual serial result channel carries a closed record, never executable requests.

**Rationale:** The kit's [native artifact contract](../../docs/native-runtime-artifact-contract.md) includes Mach-O verification and an exact pinned Python environment. A Linux container cannot establish parity for that build. A loopback bind, sterile environment, or provider allowlist on the managed host does not isolate access to host databases, Unix sockets and credentials. [Apple Virtualization](https://developer.apple.com/documentation/virtualization/vzvirtualmachineconfiguration) exposes guest devices through the VM configuration; an existing operator-provisioned launcher may implement the required device contract if its version, configuration, and safe failure behavior are proven. Do not assume any default Tart network mode qualifies. If no installed launcher can establish that contract, a minimal Apple Virtualization adapter is the only v1 fallback under review; without its entitlement and target-host proof the candidate lane stays blocked. No VM was provisioned in planning.

**Alternatives:** Extend managed `prepare` (reject: depends on the live controller and may share real credentials and spend); run a same-user process on the host (reject: containment not established); use a Linux container (reject for the native macOS acceptance target).

### Native alternative: SandVault

**Disposition:** Worth evaluating as a native launcher, not a qualifying boundary as shipped. SandVault combines a separate macOS user/login context with `sandbox-exec`, preserving the native runtime and adding filesystem and Mach-service restrictions. The [pinned upstream profile](https://github.com/webcoyote/sandvault/blob/e2f0aae861232028d3fab19848bd8a52c7d8810d/sv#L1989-L2145) starts with `allow default`, and its [security review](https://github.com/webcoyote/sandvault/blob/e2f0aae861232028d3fab19848bd8a52c7d8810d/docs/security-review-2026-11-24.md#11-sandbox-profile-is-allow-default) confirms network and host-loopback access. That directly fails FR-003. The regular writable shared workspace and optional host bridges are not the planned read-only input and bounded result channel.

A native trial would need an independently enforced deny-by-default network boundary, limited disposable storage, proof that credential stores and host IPC are inaccessible, and supervision with durable run ownership and closure proof under FR-005. Exercise all of those with inert inputs on the target Mac before considering the real build. This was a source review only; SandVault was not installed or run. Keep the offline macOS VM as the planned first backend unless a native trial proves equivalent controls.

## Render and verify only inside the guest

**Decision:** Transport pinned, reviewed artifacts as data. The host verifies opaque file identities and closed manifest shape; the guest renders the overlay and verifies the actual guest interpreter, source, native receipt and sentinel. Use the existing renderer and proof/checker entrypoints and a rehearsal-specific fixture-only startup adapter. Keep production preflight unchanged.

**Rationale:** The managed wrapper can resolve Keychain credentials and the proof imports LiteLLM as its final check. A disposable guest must have no operator sign-in, live providers, or host/home mounts before either runs. The exact candidate may need reconstructing at the right guest paths with prepared dependencies; do not copy the serving venv or adopt a new digest without verification. Pre-provision a trusted guest with compatible local dependencies (including a disposable, bound loopback database if the auth contract for the selected build requires one). No downloads or authentication during rehearsal.

## Keep ownership outside the worker

**Decision:** Create an exclusive durable host record before VM allocation. Use a short-lived supervisor independent of the foreground command and of the guest worker, not a LaunchAgent or controller RPC. Make the durable run ID the resource namespace at creation; prove every object can be discovered even if the backend did not return a handle. Report an unresolved run if it cannot be safely reconciled. Completion is contingent on hypervisor-confirmed termination of that scope and durable terminal receipt; a surviving VM remains off-network even after a supervisor crash. A subsequent invocation refuses an unresolved record.

**Rationale:** Test backend lifecycle with disposable sentinels and inert guest payloads first. Host-side negative probes do not establish guest confinement; guest startup and descendant failures must be exercised. Never direct negative tests at managed resources. Actual supervisor/guest feasibility and the different-build acceptance run are open implementation gates, not research evidence already obtained.
