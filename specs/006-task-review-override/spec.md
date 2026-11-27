# Feature Specification: Revision-bound task-review override

## User Scenarios & Testing

### User Story 1 — Record a maintainer exception (Priority: P1)

A maintainer accepts the risk of proceeding while task review is pending, without recording a peer approval.

**Independent Test**: Run the gate on an offline fixture with both validations current and spec review satisfied. Verify that a valid task waiver still blocks under default config and permits implementation with a warning after explicit opt-in.

**Acceptance Scenarios**:

1. **Given** a pending task review and no policy opt-in or no matching maintainer record, **when** the gate runs, **then** it returns `blocked` (exit 1).
2. **Given** the opt-in, a matching record, and all other prerequisites, **when** the gate runs, **then** it returns `allowed-with-warning` (exit 0) and leaves approval state unchanged.
3. **Given** a task waiver, **when** either artifact changes or the record is used for another feature, **then** the gate refuses the waiver even if the revised artifact is validated again.

### Edge Cases

- An expired SLA is not authorization. `timed-out`, `changes-requested`, missing or unrecognized task dispositions, and stale task peer approvals are ineligible.
- Spec review and task review stay independent. If both waivers are valid and used, the gate reports both; if either review is still unresolved, the gate blocks.
- Removing the waiver record or turning off the task opt-in revokes the exception. A current peer approval takes precedence over an unused waiver.

## Requirements

- **FR-001**: The checked-in and template configs MUST leave task waivers disabled. Only an explicit Boolean `gate.allow_task_review_override: true` opts in, independently of the existing spec override.
- **FR-002**: The gate MUST require a private, local `task_review_override` recording explicit use, a named maintainer, a non-blank reason, the requested feature, and both exact validated SHA-256 hashes. The receipt and containing approval state MUST identify the same feature as the request.
- **FR-003**: The waiver MUST apply only while task review is `pending`. It MUST NOT waive either validation, spec review, a request for changes, or any separate security or deployment gate. The existing spec-review waiver retains its own scope.
- **FR-004**: Bash and PowerShell MUST report the same outcome and identify each waiver used. The gate MUST NOT change peer-review state, record approval, or disclose the maintainer's name or reason in its output.

## Success Criteria

- **SC-001**: The offline Bash and PowerShell gate tests cover default denial, successful opt-in, malformed receipts and settings, feature and hash mismatches, changed artifacts with and without revalidation, ineligible dispositions, independent spec review, and unchanged state.
- **SC-002**: The operator instructions explain recording and revoking the exception without treating it as peer approval or changing the live controller.
