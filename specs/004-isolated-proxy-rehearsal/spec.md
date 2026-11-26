# Feature Specification: Isolated LiteLLM rehearsal

- **Feature Branch**: `feat/isolated-proxy-rehearsal`
- **Created**: 2026-11-26
- **Status**: Draft for operator review

Keep this contract separate from [restart controller operations](../003-restart-controller/spec.md). The repo-wide Spec Kit scaffold recovery and live controller reconciliation are separate work; this worktree does not change their feature pointers, code, policy, or installed assets.

## User Scenarios & Testing

### User Story 1 — Test a proposal before staging it (Priority: P1)

An operator can choose a reviewed deployment overlay and a prebuilt, pinned patched LiteLLM runtime; either can differ from the running proxy. Rehearsal renders the proposal, starts a temporary proxy, and checks startup, readiness, and authentication without requiring a working production controller.

**Independent Test**: Rehearse a pinned candidate build with a local fixture provider while another proxy is running. Retain the result and independently observe both listeners and the managed assets before and after.

1. **Given** a valid candidate and an available isolated runner, **when** the operator invokes `make rehearse`, **then** the exact candidate is verified, starts on a loopback-only test endpoint, and passes the declared checks without real provider calls.
2. **Given** a missing pin, unsafe path, inconsistent configuration, or unavailable isolation, **when** rehearsal is requested, **then** it refuses before starting the candidate, rather than borrowing the managed deployment's inputs or permissions.
3. **Given** a candidate that fails to start or exits during a probe, **when** rehearsal ends, **then** the result is non-success and cleanup only concerns resources owned by that rehearsal.

### User Story 2 — Know what the result does not prove (Priority: P2)

An operator can distinguish a passing isolated rehearsal from a production controller qualification, a successful `make prepare`, or permission to activate anything.

**Independent Test**: Compare the rehearsal result and operator documentation with the existing controller contract. Confirm there is no approval record, restart plan, or automatic promotion path.

## Requirements

- **FR-001**: `make rehearse` must require an explicit test deployment, include the selected overlay and runtime pin in its result, and never use `POLICY` or the managed deployment as fallback inputs. Take data inputs, not executable deployment settings from an arbitrary checkout. Accept only reviewed candidate builds; this is not a runner for untrusted code.
- **FR-002**: The rehearsal must reuse the kit renderer and existing runtime/provenance and patch-sentinel checks. Verify the selected interpreter, source, pin, and native receipt where required; never rebuild, install over, or edit a serving runtime. The real candidate must be the one used for the HTTP checks, not a stand-in. Any rehearsal-only fixture substitutions must be explicit and reported; a result must distinguish proposal validation from exercised behavior.
- **FR-003**: Before startup, the rehearsal must establish a private disposable state boundary and a loopback-only test endpoint separate from managed endpoints. Prevent candidate access to the managed listener, databases, controller socket, managed files for writing, and real upstreams. Clean environment variables and a loopback bind alone are not proof of isolation. Use an enforceable runner boundary or refuse. The candidate must not source real credentials or contact credential stores; use an ephemeral in-memory test key and only local fixture dependencies. Real-provider checks are separate, explicitly authorized work, not a flag that relaxes the default.
- **FR-004**: Probes must attribute the listener to the owned candidate and check liveness, readiness, rejection without a key, and acceptance with the ephemeral test key. A local fixture may exercise a route; report which routes and config features were actually exercised. An occupied or ambiguous endpoint must not be accepted as the candidate.
- **FR-005**: Bound startup, probing, and teardown. Clean up only resources created by this run, including on normal failures and supported interrupts. If ownership or cleanup cannot be proved, report an incomplete run and preserve sanitized evidence; do not widen cleanup to other jobs, listeners, or processes.
- **FR-006**: Report the overlay and effective configuration digests, runtime identity, declared fixture substitutions, check outcomes, and teardown status on success or failure; fields not yet established stay explicitly unknown. Return success only after required checks and teardown succeed. No credentials, raw responses, or host-specific paths in a shareable result.
- **FR-007**: The rehearsal must not use launchd service control, call the live controller, change installed files, create approval/qualification records, or promote a proposal. The existing meaning of `make prepare` and the startup supervision canary must not change. Unsupported production features or dependencies must be reported, never silently omitted and claimed tested.

## Scope decisions and trade-offs

- Prefer a separate, short-lived runner over extending the live controller RPC. The existing `prepare` deliberately requires a managed primary and uses its bound assets and potentially shared dependencies. Reusing its authority would undermine the separation.
- Reuse the managed wrapper only through a reviewed boundary that cannot invoke its normal credential resolvers. The current wrapper requires a service-account token early in startup and can fall back to Keychain; it is not an offline entrypoint as written. The plan must identify the smallest reusable seam without weakening the managed path or changing its trust inventory without review.
- Prefer a local contained runner if a negative probe proves the boundaries in this environment. A dedicated VM is a fallback if the host cannot enforce them. A second full managed deployment, a permanent profile registry, and controller or guard changes are out of scope.

## Success Criteria

- **SC-001**: A test run selects and verifies a real patched build different from the running managed proxy, succeeds against local fixtures, and yields a sanitized result identifying the exact build and tested config. Fixture-only tests do not satisfy this criterion.
- **SC-002**: During that run, read-only before/after evidence confirms the managed listener's identity, managed asset digests, and controller state were not changed by rehearsal. On a shared host, concurrent owner changes are not proof of isolation or of a rehearsal-caused write; rerun under controlled conditions if attribution is ambiguous.
- **SC-003**: Negative tests cover missing or mismatched runtime evidence, attempted managed-path access, disallowed egress and credentials, an occupied endpoint, startup/probe failures, and failed cleanup. Each produces a non-success result without changing a protected target.
- **SC-004**: Existing Makefile maintenance tests remain green. A passing result makes no claim about live inference, production credential compatibility, controller qualification, or restart authorization.
