# Feature Specification: Restart controller operations

**Feature**: `003-restart-controller` · **Branch**: `docs/controller-routine-work` · **Created**: 2026-11-25

**Status**: Implemented and verified offline. See the [closeout record](analysis.md#offline-closeout) for the implementation and review evidence. This specification does not authorize live work. Select `specs/003-restart-controller` in your own worktree; do not commit the local feature pointer.

## User Scenarios & Testing

### US1 — Diagnose and stage without disrupting the proxy (P1)

A Hermes session starts from its own worktree and can choose a single command for status, health, preview, staging or verification. Given a missing bound source or a failed verification, it stops and reports what needs review instead of substituting its checkout. Test independently with disposable policy, installer and HTTP fixtures.

### US2 — Distinguish installed bytes from live readiness (P1)

An operator sees whether the policy, installed receipt, controller bundle and policy-bound kit source agree on disk. Given a missing receipt or differing files, the check returns failure without changing them. A match is not a loaded-job, daemon-epoch, or qualification verdict. Test independently with a synthetic bundle and changed bytes or mode.

### US3 — Use the controlled restart and preserve refusals (P1)

After preflight, the session requests a rehearsal and provides the exact prepared plan to the owner. Only fresh owner approval and exact plan binding permit submission. On refusal, ambiguous ownership, stale evidence or timeout, the session preserves the listener and recovery records and does not retry by another route. Use isolated controller, recovery and acceptance fixtures; do not interrupt a live proxy for verification.

### US4 — Find the normative controller contract (P2)

A later session starts in `specs/`, finds the operative contracts and current requirements, and can distinguish historical plans and receipts from instructions. Test by following links from the repository and mapping requirements to code, documentation and tests.

### Edge cases and error handling

- Healthy HTTP or a successful status RPC cannot establish ownership, qualification, or absence of drift.
- A worktree can supply the utility but is not a deployment source unless deliberately selected by the owner in the installed policy. Missing inputs are a stop, not a fallback.
- An expired plan or uncertain previous operation cannot be repaired by replaying submission, deleting state, or issuing replacement evidence from a historical snippet.
- A read-only observation is a point-in-time comparison. It is not a lock, atomic snapshot, or defense against malicious same-user edits.

## Requirements

- **FR-001**: The kit MUST provide one routine runbook and a command index that separate observation, preview, staging, rehearsal, and owner submission. Kit work MUST use a dedicated Git worktree, without repointing installed policy to it.
- **FR-002**: Maintenance commands MUST use the installed policy's explicit roots and existing installer, drift, health and controller interfaces. Staging MUST NOT activate, qualify, approve, or restart a service. Preview is not a credential-free operation; review the deployment input before invoking installer commands.
- **FR-003**: The on-disk binding check MUST use the existing policy reader, digest, controller-input registry and file-fact algorithm to compare the current policy, recorded install receipt, installed copies, and policy-bound kit controller inputs. It MUST report mismatches or unreadable inputs without writing state, contacting services or running external commands. Its result MUST identify its scope as on-disk controller binding only, not full deployment drift, loaded-job parity or qualification.
- **FR-004**: The controller MUST serialize prepare and restart, rehearse real candidate acceptance before disruption, bind owner authorization to the exact current plan and deadline, and consume it durably before service control. Keep command shape and fail-closed semantics in the [controller contract](contracts/controller.md).
- **FR-005**: The controller MUST bind qualification and supervision to the intended incarnation, stop admitting mutation on shutdown, and retain recovery evidence after interruption. Use the [shutdown amendment](contracts/shutdown.md) and its [subsequent reconciliation](contracts/reconciliation.md) together.
- **FR-006**: The controller spec MUST preserve prior design and live evidence as historical records, map requirements to their owning contracts and verification, and separate repo acceptance from live activation, qualification, approval and recovery decisions.

### Key entities

- **Installed policy**: Binds deployment root, kit root, bundle, state, supervisor label and ports.
- **Install receipt**: Records exactly the version, policy digest and controller byte/mode facts at installation; not a live proof.
- **Qualification**: Attests to the intended supervision and canary for one daemon incarnation; distinct from plan or approval.
- **Prepared plan and approval**: A short-lived bound request and separately issued owner authorization; neither authorizes a replay. The controller validates a same-UID record; owner issuance is a workflow rule, not independent human authentication.
- **Recovery records**: Durable operation, fence, child and canary evidence; retain when the result is uncertain.

## Success Criteria

- **SC-001**: An agent starting from `AGENTS.md` can locate one command and its authority boundary for each routine task without using an incident script.
- **SC-002**: Fixture verification covers a valid binding, changed kit input, changed installed input, changed policy and absent receipt; failures are nonzero and the check makes no service or process call.
- **SC-003**: Offline verification retains the controller tests for approval, no-disruption refusal, shutdown and recovery, without granting live approval.
- **SC-004**: Spec Kit prerequisite and identifier/coverage checks pass, and every current requirement has an implementation or documentation owner and verification target. Pending gates are recorded, not backfilled as historical approvals.

## Scope and relationships

This feature owns controller behavior and owner maintenance. The [guard's incidence-reduction contract](../../docs/contracts/guard-incidence-reduction.md) owns interception and refusal; this feature neither changes guard policy nor supplies a fallback around it. The [safe-restart foundation](../../docs/safe-restart-contract.md) retains shared deployment and credential rules. The [native artifact contract](../../docs/native-runtime-artifact-contract.md) owns optional native provenance. No changes to the installed proxy/controller, guard, qualifications, approvals, or recovery state are in scope.

## Clarifications

- Use the installed policy as the only runtime routing input; never derive the deployment from the caller's working directory.
- `make check-binding` compares on-disk facts only. Use the install verifier for bundle and plist parity. The daemon remains authoritative for live admission, recovery and boot-time bindings. See the [runbook](../../docs/restart-controller-operations.md#existing-deployment-before-you-prepare) for the diagnostic’s limits.
- Preserve dated owner packages as evidence, not templates or runnable instructions for the current host.
