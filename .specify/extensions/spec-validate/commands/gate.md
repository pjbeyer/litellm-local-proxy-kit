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

## Important Constraints

- This command is **separate from status** — status reports, gate enforces.
- Fail the hook on any error, malformed output, or outcome other than `allowed`.
- The kit does not permit a maintainer override or timeout self-approval. Pending, timed-out, and unknown review states remain blocked.
- Approval state is local and ignored by Git. A chat approval is not a validation or review record.
