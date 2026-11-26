# Implementation Gate

## Command: `speckit.spec-validate.gate`

Enforce the hard gate before `/speckit.implement`. This command is registered as a **mandatory** `before_implement` hook — it runs automatically and blocks implementation when validation or review requirements are not met.

## Execution Flow

1. Determine the current feature from `.specify/feature.json`
2. Run `check-gate.sh <feature>` to evaluate the gate
3. Parse the JSON result containing `outcome`, `reason`, `actions_required`, and `warnings`

### Gate Outcomes

**`blocked` (exit code 1):**
- Display the reason clearly
- List all required actions as a numbered checklist
- **FAIL the hook** — this prevents `/speckit.implement` from proceeding
- Example output:
  ```
  BLOCKED: Spec has not been validated

  Required actions:
  1. Run /speckit.spec-validate.validate to validate spec.md
  ```

**`allowed` (exit code 0):**
- Proceed silently — no output needed unless warnings exist
- Implementation continues

**`allowed-with-warning` (exit code 0):**
- Display the warning. Implementation may proceed for the approved revisions.

## Important Constraints

- This command is **separate from status** — status reports, gate enforces.
- Fail the hook on any error, malformed output, or outcome other than `allowed` or `allowed-with-warning`.
- A maintainer can waive pending or timed-out **spec** review only when `gate.allow_maintainer_override: true` is set in the extension config. Record `override.used: true`, a non-empty `reason` and `by`, and the approved `spec_hash` and `tasks_hash` inside `override` in private local approval state.
- Both validations and task review still apply. Unknown spec review states, change requests, and mismatched digests block. A timeout alone does not authorize implementation. The override path requires Python 3 with PyYAML.
- Approval state is local and ignored by Git. A chat approval is not a validation or review record.
