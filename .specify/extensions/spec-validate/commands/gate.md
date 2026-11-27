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
- Both validations must pass and match the current artifact digests. A waiver does not cover change requests, unknown or missing review states, stale peer approvals, or separate security and deployment gates.
- Approval state is local and ignored by Git. A chat instruction alone is not a validation or review record. The override paths require Python 3 with PyYAML.

## Maintainer overrides

Spec and task review have separate opt-ins and records. The existing spec waiver is unchanged: `gate.allow_maintainer_override: true` plus `override.used: true`, non-blank `by` and `reason`, and matching `spec_hash` and `tasks_hash` can waive a `pending` or `timed-out` **spec** review only.

To waive `pending` **task** review:

1. Confirm the maintainer explicitly authorized the waiver for the named feature and both current validated digests. Do not infer authorization from a timeout or a request to implement the feature.
2. Set `gate.allow_task_review_override: true` in this worktree's extension config. The checked-in setting is `false`; keep the opt-in local to the worktree.
3. Read the existing feature record with `read-approval-state.sh` (PowerShell: `read-approval-state.ps1`). Confirm its `feature` matches the requested feature and both hashes match the `compute-hash` results for `spec.md` and `tasks.md`. Merge only this field into the record, then save it with the matching `write-approval-state` script. Keep the existing `tasks_review_status`, `reviewed_tasks_hash`, and `approval_status` unchanged.

   ```json
   {
     "task_review_override": {
       "used": true,
       "by": "<maintainer>",
       "reason": "<reason for waiving pending task review>",
       "feature": "<numbered-feature>",
       "spec_hash": "<validated sha256:… for spec.md>",
       "tasks_hash": "<validated sha256:… for tasks.md>"
     }
   }
   ```

4. Rerun `check-gate.sh <feature>` (PowerShell: `check-gate.ps1 <feature>`). If all other checks pass, expect `allowed-with-warning` and a task-review warning. If both waivers are used, the gate reports both. Never mark the task review approved on the strength of a waiver.

Remove the field or turn off the opt-in to revoke the waiver. Do not carry the record into another feature or update its hashes to follow later edits; a new revision needs new authorization.
