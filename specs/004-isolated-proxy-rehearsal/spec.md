# Feature Specification: Isolated LiteLLM rehearsal

- **Feature Branch**: `feat/isolated-proxy-rehearsal`
- **Created**: 2026-11-26
- **Status**: Revised after operator-approved red-team dispositions; comprehension validation pending

This contract owns isolated proposal rehearsal. [Spec 003](../003-restart-controller/spec.md) owns managed preparation, qualification, and restart; neither workflow may assume the authority of the other. This work does not change the repo-wide Spec Kit scaffold or the live controller reconciliation.

## User Scenarios & Testing

### User Story 1 — Test a proposal before staging it (Priority: P1)

An operator chooses a reviewed deployment overlay and a prebuilt, pinned patched LiteLLM runtime. Either can differ from the running proxy. Rehearsal renders the proposal, starts a temporary proxy, and checks startup, readiness, and authentication without requiring a working production controller.

**Independent Test**: Rehearse a pinned candidate build with a local fixture while another proxy is running. Retain the result and independently observe coexistence and managed state as specified under SC-002.

1. **Given** a valid candidate and an available isolated runner, **when** the operator invokes `make rehearse`, **then** the exact candidate is verified, starts on a loopback-only test endpoint, and passes the declared checks without real provider calls.
2. **Given** unavailable isolation or a missing required input, **when** rehearsal is requested, **then** it refuses without executing candidate code or borrowing managed inputs or permissions. A mismatch discovered by isolated runtime verification prevents proxy startup.
3. **Given** a candidate that fails to start, exits, or loses identity during a probe, **when** rehearsal ends, **then** it reports a non-success outcome and cleanup only concerns resources owned by that rehearsal.

### User Story 2 — Know what the result does not prove (Priority: P2)

An operator can distinguish a passing isolated rehearsal from a production controller qualification, a successful `make prepare`, or permission to activate anything.

**Independent Test**: Compare the rehearsal result and operator documentation with the existing controller contract. Confirm there is no approval record, restart plan, or automatic promotion path.

## Requirements

- **FR-001**: `make rehearse` must require explicit data inputs for a reviewed overlay and pinned candidate runtime, never `POLICY`, executable deployment settings, or managed inputs as fallback. A trusted operator reviews the exact pairing and explicitly supplies its expected overlay and pin digests for a fixture-only run. Match those digests to the input bytes before executing candidate code, then carry the same bytes into rendering and verification. A mismatch refuses. No unattended rehearsal or untrusted-code execution is in scope. Record the digests, not local input paths.
- **FR-002**: Reuse the kit renderer, runtime provenance checks, and patch sentinel in proof-before-sentinel order. Verify the interpreter, source, pin and applicable native receipt inside the runner; bind the owned proxy process and its listener to the same verified versioned runtime and effective configuration held stable through probing. Recheck runtime proof and effective input identity before accepting success; verify the loaded native path and hash where native behavior is exercised. Reject drift or uncertain continuity. Do not rebuild, install over or edit a serving runtime.
- **FR-003**: Establish an enforceable boundary before processing proposal content or invoking the candidate interpreter, renderer, provenance or sentinel helpers, fixtures, or startup; keep it in force through cleanup. Deny access to real credentials and credential stores and to managed listener, database and controller endpoints. No managed files may be writable. Deny network access by default except for explicitly admitted run-owned loopback connections needed for fixtures and probes; imported helpers and descendants inherit the same restrictions. Do not inherit host secrets, sockets or session environment. Use an ephemeral in-memory test key and disposable state. Verify the boundary with trusted negative probes before candidate-bearing work, or refuse. A clean environment and a loopback bind alone do not qualify. Real-provider checks are separate, explicitly authorized work, not a relaxation flag.
- **FR-004**: Attribute every accepted probe to the same owned proxy instance and endpoint. Check liveness, readiness, unauthenticated rejection and test-key acceptance. Refuse occupied or ambiguous endpoints and fail on identity loss or replacement. Local fixture route checks are allowed; report exactly which routes and features were exercised.
- **FR-005**: Bound verification, startup, probing and teardown. Create a durable run record before executing candidate code. Clean up only resources created by the run on success, failure, SIGINT and SIGTERM. Assign cleanup to an isolation owner independent of the CLI and the candidate-bearing worker. Keep restrictions active until closure of all owned processes, descendants and endpoints is proved. If the isolation owner fails too, the boundary must persist or stop the entire owned scope; an unverified outcome is incomplete, not a pass. Preserve a sanitized record and have the next invocation detect unresolved runs before new candidate work; do not repeat or widen cleanup.
- **FR-006**: A sanitized result must bind the overlay and pin digests, rendered proposal digest, and effective launch identity (fixture YAML plus sanitized behavior-affecting settings and fixture identities) to the verified runtime and check outcomes. Declare substitutions, omitted features, teardown, and unknown fields; keep a run ID for incomplete outcomes. Return success only after required checks, identity revalidation and teardown. Omit credentials, raw responses and host-specific paths. Never include the ephemeral key in either the report or a digest input.
- **FR-007**: Never use launchd service control, call the live controller, change installed files, create approval/qualification records, or promote a proposal. Do not change `make prepare` or the startup supervision canary. Unsupported production features must be refused or explicitly marked unexercised, never silently omitted and claimed tested.

## Security Considerations and Edge Cases

- A credential-free isolated runner is a prerequisite even for a candidate interpreter used only for verification or rendering. A trusted isolation supervisor may provision and observe its own run boundary and emit sanitized evidence; it does not invoke managed credential resolvers or use candidate inputs to control the host.
- An occupied endpoint, replaced listener, changed runtime, or missing proof is not a successful test. Failed or uncertain cleanup is not a pass and must not tear down containment around surviving resources.
- An independent observer may read managed evidence solely for acceptance, outside the runner. It is not an input or credential source for the candidate.

## Scope decisions and trade-offs

- Prefer a separate short-lived runner over extending the live controller RPC. `prepare` requires a managed primary and uses controller-bound assets and potentially shared dependencies.
- The existing wrapper needs a service-account token early in startup and can fall back to Keychain. Reuse its safe building blocks, not the normal resolver path; do not weaken the managed path or change its trust inventory without review.
- Prefer a contained local runner only if it proves the required boundary on the target host. A dedicated VM is a fallback if the host cannot enforce the boundary. A full second managed deployment, a profile system, provider calls, and controller or guard changes are out of scope.

## Acceptance Criteria

- **SC-001**: Select and verify a real patched build different from the running managed proxy, exercise it against local fixtures, and report the exact selected and effective identities. Demonstrate that an allowed environment-only change changes the effective identity and that omitted features are not credited; a stub-only startup does not pass this criterion.
- **SC-002**: An independent read-only observer outside the runner declares its managed asset and state inventory before the run. The managed listener must retain the same process identity before, during and after the run, and the candidate must coexist during it. Compare before/after asset digests and controller policy, qualification, bootstrap and durable operation/recovery evidence for equality. These are point observations, not proof of continuous integrity. Incomplete or ambiguous observations, including concurrent owner changes obscuring attribution, are inconclusive and require a controlled rerun.
- **SC-003**: Exercise changed inputs after operator review; missing isolation before any candidate helper runs; denied managed paths, credentials and egress across candidate-bearing stages; missing or changed runtime and launch identity; occupied or replaced listeners; startup/probe errors; SIGINT, SIGTERM and worker loss; and failed teardown with containment still active. Use disposable sentinels rather than any live protected target for negative tests. Require non-success outcomes and no mutation of the protected sentinel or target. Demonstrate that a new invocation reports the unresolved run without starting another candidate.
- **SC-004**: Existing Makefile maintenance tests remain green. A passing result makes no claim about live inference, production credential compatibility, controller qualification, restart authorization, or unexercised behavior.
