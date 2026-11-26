# Check Validation Status

## Command: `speckit.spec-validate.status`

Display the current validation and approval state for the active feature. This command is **read-only** — it MUST NOT modify any state.

## Execution Flow

1. Determine the current feature from `.specify/feature.json`
2. Run `read-approval-state.sh <feature>` to load the approval state
3. Display a formatted summary:

### Output Format

```markdown
## Spec Validate Status: <feature-name>

| Field                  | Value              |
|------------------------|--------------------|
| Feature                | <feature>          |
| Spec validation        | <not-run/passed/failed/stale> |
| Spec hash              | <hash or "not set"> |
| Spec validated at      | <timestamp or "never"> |
| Tasks validation       | <not-run/passed/failed/stale> |
| Tasks hash             | <hash or "not set"> |
| Spec review status     | <not set/not-required/pending/approved/changes-requested/timed-out> |
| Tasks review status    | <not set/not-required/pending/approved/changes-requested/timed-out> |
| Spec / tasks reviewer  | <name or "none"> |

### Warnings
<list of active warnings, or "None">

### Review Comments
<list of reviewer comments, or "None">
```

4. Report missing or unresolved validation and review fields. Refer to the gate for the actual verdict; the legacy stored `approval_status` is not authoritative.
5. Do NOT compute or update any hashes, staleness checks, or approval decisions — those are the `gate` command's responsibility

## Important Constraints

- **Never modify state** — this is purely informational
- **Never evaluate gate rules** — just display stored state as-is
- **Always show all fields** — even if null or empty, show the field with "not set" / "none" / "never"
