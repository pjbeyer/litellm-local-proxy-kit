# Spec Validate

Comprehension validation, review gating, and approval state for spec-kit artifacts.

## Overview

Spec Validate ensures developers actually read and understand generated specifications before building. It adds a staged-reveal multiple-choice quiz, peer review workflow, and a hard gate before implementation.

## Install

1. Copy `spec-kit-spec-validate/` into your project's `.specify/extensions/` directory
2. Register the extension with spec-kit (the hooks in `extension.yml` are auto-detected)
3. Optionally copy `config-template.yml` to `.specify/extensions/spec-validate/spec-validate-config.yml` and customize

## Commands

### `/speckit.spec-validate.validate`

Validate your comprehension of the current spec. Presents sections one at a time with multiple-choice questions for critical items.

```
/speckit.spec-validate.validate
```

- Specs with 0 critical items are auto-passed
- Wrong answers are recorded (no retries) and may trigger peer review
- Results are stored as JSON with a SHA-256 content hash

### `/speckit.spec-validate.validate-tasks`

Same validation mechanism applied to `tasks.md`.

```
/speckit.spec-validate.validate-tasks
```

### `/speckit.spec-validate.gate`

Hard gate before implementation. Runs automatically as a mandatory `before_implement` hook.

- **Blocks** when: no validation, failed validation, stale hash, pending review, changes requested
- **Allows** when the current spec and task digests match their respective validations and either the applicable peer review is recorded or peer review is not required. Peer approval must independently record the current `reviewed_spec_hash` or `reviewed_tasks_hash` for the artifact reviewed. Legacy records missing the task review disposition must go through the current task validation and review workflow.
- **Allows with warning** when a [recorded maintainer override](commands/gate.md) waives pending or timed-out spec review or, under a separate opt-in, pending task review. Both validations and all other gates still apply.

### `/speckit.spec-validate.review`

Peer review for the validated spec (`review spec`) or tasks (`review tasks`). The reviewer sees missed item IDs but not the operator’s private selections.

```
/speckit.spec-validate.review
```

- Approve, request changes, or add notes
- 24-hour SLA — report the delay and keep the gate blocked if review is required

### `/speckit.spec-validate.status`

Read-only display of current validation and approval state.

```
/speckit.spec-validate.status
```

### `/speckit.spec-validate.analytics`

Private and aggregated comprehension analytics.

```
/speckit.spec-validate.analytics
```

## Workflow

```
/speckit.specify          → spec.md generated
      ↓
[after_specify hook]      → "Run comprehension validation?" (optional)
      ↓
/speckit.spec-validate.validate  → staged quiz
      ↓
/speckit.tasks            → tasks.md generated
      ↓
[after_tasks hook]        → "Run task validation?" (optional)
      ↓
/speckit.spec-validate.validate-tasks  → staged quiz
      ↓
/speckit.implement
      ↓
[before_implement hook]   → gate check (MANDATORY)
      ↓
      ├── allowed         → proceed
      └── blocked         → STOP, show actions needed
```

## Configuration

Edit `.specify/extensions/spec-validate/spec-validate-config.yml`:

| Setting | Default | Description |
|---------|---------|-------------|
| `complexity.low_max_items` | 3 | Specs with ≤N items = low complexity |
| `complexity.high_min_items` | 8 | Specs with ≥N items = high complexity |
| `always_critical_sections` | See config | Sections always treated as critical |
| `review.require_on_any_miss` | true | Force review on wrong answers |
| `review.sla_hours` | 24 | Hours before review times out |
| `review.timeout_policy` | block | An overdue required review still blocks |
| `gate.block_without_validation` | true | Block if not validated |
| `gate.allow_maintainer_override` | false | Permit a recorded waiver of spec review |
| `gate.allow_task_review_override` | false | Permit a separate, feature- and revision-bound waiver of pending task review |
| `privacy.personal_metrics_visibility` | self-and-maintainers | Who sees personal metrics |
| `question_generation.mode` | deterministic-first | Distractor generation strategy |

## Solo Developer Mode

Current spec and task validations are both required. A solo workflow or expired SLA does not waive review. A maintainer must explicitly [record and enable a waiver](commands/gate.md) for the applicable review.

## Troubleshooting

### "BLOCKED: Spec has not been validated"
Run `/speckit.spec-validate.validate` before attempting implementation.

### "BLOCKED: Spec content has changed since validation"
Spec was edited after validation. Re-run `/speckit.spec-validate.validate`.

### "BLOCKED: Reviewer requested changes"
Address reviewer feedback, update the spec, then re-validate.

### "BLOCKED: Peer review is pending"
Wait for independent review of the current spec. A missed SLA does not change the gate outcome.

## Privacy

- **Local only**: Approval state, review outcomes, identities, wrong answers, distractor selections, and personal accuracy stay out of Git.
- **Team analytics**: Aggregated only — no individual names on scores

## License

MIT
