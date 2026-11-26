# Validate Task Comprehension

## Command: `speckit.spec-validate.validate-tasks`

Validate the current feature's `tasks.md` using the same staged-reveal multiple-choice mechanism as spec validation. Results are stored independently from spec validation with a separate content hash.

## Prerequisites

- A generated `tasks.md` exists for the current feature
- Shell scripts are available: `compute-hash.sh`, `read-approval-state.sh`, `write-approval-state.sh`

## Execution Flow

This command follows the [spec validation workflow](validate.md), with these differences:

### Differences from Spec Validation

1. **Target artifact**: Read `tasks.md` instead of `spec.md`
2. **Hash field**: Store hash as `tasks_hash` (not `spec_hash`)
3. **Validation field**: Update `tasks_self_validation` (not `spec_self_validation`)
4. **Timestamp field**: Update `tasks_validated_at` (not `spec_validated_at`)
5. **Item classification for tasks**:
   - **Simple**: Straightforward setup tasks, single-file creation tasks, obvious configuration
   - **Critical**: Tasks with complex dependencies, multi-step acceptance criteria, edge case handling, integration tasks, tasks referencing specific contracts or data model constraints
6. **Private analytics**: Write attempt details separately from spec analytics in the same private state file, using `"artifact": "tasks.md"` in the attempts array

### Preserved Behavior

- Staged reveal (one section/phase at a time)
- Multiple-choice with deterministic-first distractor generation
- No retries on wrong answers
- Auto-pass when 0 critical items
- Same review trigger rules (missed items, complexity)
- Same agent-adaptive UX (vscode/askQuestions fallback)
- Same privacy model (local approval state and private analytics; neither is committed)

### State Update

Apply the review rules in `validate.md` to the tasks independently of the spec. Merge the task digest, validation result and timestamp, critical count and missed IDs, and `tasks_review_status` (`pending` or `not-required`) into the current record. For required review, record `tasks_review_requested_at`. Clear prior task review decisions when the digest changes or a new review is required, including `reviewed_tasks_hash`. Use `tasks_critical_count` and `tasks_missed_items` for reviewer context. Preserve all spec validation and review fields. Record the task validator as `tasks_validated_by` in private state; preserve `validated_by` for the spec. Keep the legacy `approval_status` field `blocked`; run the gate for the combined verdict.

## Important Constraints

- Tasks validation is independent from spec validation — each has its own hash and state
- Both must pass (or be auto-passed) for the gate to allow implementation
- A task review is not interchangeable with a spec review. Use `/speckit.spec-validate.review tasks` when required.
