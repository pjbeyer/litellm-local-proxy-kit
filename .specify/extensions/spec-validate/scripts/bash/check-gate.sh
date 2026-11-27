#!/usr/bin/env bash
# check-gate.sh — Evaluate implementation gate for a feature
# Usage: check-gate.sh <feature-name>
# Output: Gate result JSON to stdout per Contract 3
# Exit 0 for allowed, exit 1 for blocked

set -e

FEATURE="${1:-}"

if [[ ! "$FEATURE" =~ ^[0-9]{3,}-[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "Error: Expected a numbered feature directory name. Usage: check-gate.sh <feature-name>" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
READ_SCRIPT="$SCRIPT_DIR/read-approval-state.sh"
HASH_SCRIPT="$SCRIPT_DIR/compute-hash.sh"

# Read current approval state
if ! command -v python3 >/dev/null 2>&1 ||
   ! STATE=$(bash "$READ_SCRIPT" "$FEATURE" 2>/dev/null) || [ -z "$STATE" ]; then
  cat <<EOF
{
  "outcome": "blocked",
  "reason": "Failed to read approval state for feature: $FEATURE",
  "actions_required": ["Run /speckit.spec-validate.validate to validate spec.md"],
  "warnings": []
}
EOF
  exit 1
fi

# Require a real JSON parser; text matching cannot validate an approval record.
_get_field() {
  printf '%s' "$STATE" | python3 -c 'import json, sys
value = json.load(sys.stdin).get(sys.argv[1])
print(value if isinstance(value, str) and value.isascii() and value.isprintable() else "")' "$1" 2>/dev/null
}

SPEC_VALIDATION=$(_get_field "spec_self_validation")
TASKS_VALIDATION=$(_get_field "tasks_self_validation")
REVIEW_STATUS=$(_get_field "review_status")
STORED_SPEC_HASH=$(_get_field "spec_hash")
REVIEWED_SPEC_HASH=$(_get_field "reviewed_spec_hash")
STORED_TASKS_HASH=$(_get_field "tasks_hash")
TASKS_REVIEW_STATUS=$(_get_field "tasks_review_status")
REVIEWED_TASKS_HASH=$(_get_field "reviewed_tasks_hash")

# Determine spec file location
if [ -n "$SPEC_VALIDATE_SPEC_DIR" ]; then
  SPEC_DIR="$SPEC_VALIDATE_SPEC_DIR"
else
  _dir="$(pwd)"
  while [ "$_dir" != "/" ]; do
    if [ -d "$_dir/.specify" ]; then break; fi
    _dir="$(dirname "$_dir")"
  done
  # Try to find feature spec directory
  if [ -f "$_dir/specs/$FEATURE/spec.md" ]; then
    SPEC_DIR="$_dir/specs/$FEATURE"
  else
    SPEC_DIR=""
  fi
fi

WARNINGS='[]'
ACTIONS='[]'
OUTCOME="blocked"
REASON=""

# Gate rule 1: No validation at all
if [ "$SPEC_VALIDATION" = "not-run" ]; then
  REASON="Spec has not been validated"
  ACTIONS='["Run /speckit.spec-validate.validate to validate spec.md"]'
  cat <<EOF
{
  "outcome": "blocked",
  "reason": "$REASON",
  "actions_required": $ACTIONS,
  "warnings": []
}
EOF
  exit 1
fi

# Gate rule 2: Validation failed
if [ "$SPEC_VALIDATION" = "failed" ]; then
  REASON="Spec validation failed"
  ACTIONS='["Re-run /speckit.spec-validate.validate"]'
  cat <<EOF
{
  "outcome": "blocked",
  "reason": "$REASON",
  "actions_required": $ACTIONS,
  "warnings": []
}
EOF
  exit 1
fi

# All approval paths require a current validation digest.
CURRENT_HASH=""
if [ -n "$SPEC_DIR" ]; then
  CURRENT_HASH=$(bash "$HASH_SCRIPT" "$SPEC_DIR/spec.md" 2>/dev/null) || CURRENT_HASH=""
fi
if [[ ! "$CURRENT_HASH" =~ ^sha256:[0-9a-f]{64}$ ]] || [ "$CURRENT_HASH" != "$STORED_SPEC_HASH" ]; then
  cat <<EOF
{
  "outcome": "blocked",
  "reason": "Current spec digest is unavailable or does not match validation",
  "actions_required": ["Re-run /speckit.spec-validate.validate with current spec.md"],
  "warnings": []
}
EOF
  exit 1
fi

# Task comprehension is independent of spec comprehension.
CURRENT_TASKS_HASH=$(bash "$HASH_SCRIPT" "$SPEC_DIR/tasks.md" 2>/dev/null) || CURRENT_TASKS_HASH=""
if [ "$TASKS_VALIDATION" != "passed" ] || [[ ! "$CURRENT_TASKS_HASH" =~ ^sha256:[0-9a-f]{64}$ ]] || [ "$CURRENT_TASKS_HASH" != "$STORED_TASKS_HASH" ]; then
  cat <<EOF
{
  "outcome": "blocked",
  "reason": "Current tasks are not validated or their digest does not match validation",
  "actions_required": ["Run /speckit.spec-validate.validate-tasks with current tasks.md"],
  "warnings": []
}
EOF
  exit 1
fi

# Gate rule 4: Reviewer requested changes
if [ "$REVIEW_STATUS" = "changes-requested" ]; then
  REASON="Reviewer requested changes — address feedback and re-validate"
  ACTIONS='["Address reviewer comments", "Re-run /speckit.spec-validate.validate"]'
  cat <<EOF
{
  "outcome": "blocked",
  "reason": "$REASON",
  "actions_required": $ACTIONS,
  "warnings": []
}
EOF
  exit 1
fi

# Peer review must independently identify the validated bytes.
if [ "$REVIEW_STATUS" = "approved" ] && [ "$REVIEWED_SPEC_HASH" != "$CURRENT_HASH" ]; then
  REASON="Peer review does not match the current spec digest"
  ACTIONS='["Obtain peer review of current spec.md"]'
  cat <<EOF
{
  "outcome": "blocked",
  "reason": "$REASON",
  "actions_required": $ACTIONS,
  "warnings": []
}
EOF
  exit 1
fi

# Review dispositions are independent; a waiver applies only to its own review.
TASK_WARNING=""
if [ "$TASKS_REVIEW_STATUS" = "pending" ] &&
   [ "$SPEC_VALIDATION" = "passed" ] &&
   printf '%s' "$STATE" | python3 "$SCRIPT_DIR/../check-maintainer-override.py" tasks "$FEATURE"; then
  TASK_WARNING='"Pending task review overridden by recorded maintainer authorization"'
elif { [ "$TASKS_REVIEW_STATUS" != "not-required" ] && [ "$TASKS_REVIEW_STATUS" != "approved" ]; } ||
     { [ "$TASKS_REVIEW_STATUS" = "approved" ] && [ "$REVIEWED_TASKS_HASH" != "$CURRENT_TASKS_HASH" ]; }; then
  cat <<EOF
{
  "outcome": "blocked",
  "reason": "Task review is unresolved or does not match the current tasks digest",
  "actions_required": ["Complete the task review requirement for current tasks.md"],
  "warnings": []
}
EOF
  exit 1
fi

SPEC_WARNING=""
if [ "$SPEC_VALIDATION" = "passed" ]; then
  case "$REVIEW_STATUS" in
    not-required|approved) OUTCOME="allowed" ;;
    pending|timed-out)
      if printf '%s' "$STATE" | python3 "$SCRIPT_DIR/../check-maintainer-override.py"; then
        SPEC_WARNING='"Unresolved spec review overridden by recorded maintainer authorization"'
        OUTCOME="allowed"
      fi
      ;;
  esac
fi

if [ "$OUTCOME" = "allowed" ]; then
  REASON="Spec and tasks validated and review requirements met"
  if [ -n "$SPEC_WARNING$TASK_WARNING" ]; then
    OUTCOME="allowed-with-warning"
    REASON="Recorded maintainer override permits implementation"
    [ -z "$SPEC_WARNING" ] || [ -z "$TASK_WARNING" ] || SPEC_WARNING="$SPEC_WARNING, "
    WARNINGS="[$SPEC_WARNING$TASK_WARNING]"
  fi
  cat <<EOF
{
  "outcome": "$OUTCOME",
  "reason": "$REASON",
  "actions_required": [],
  "warnings": $WARNINGS
}
EOF
  exit 0
fi

# Fallback: blocked
REASON="Gate check did not match any allow condition"
cat <<EOF
{
  "outcome": "blocked",
  "reason": "$REASON",
  "actions_required": ["Run /speckit.spec-validate.status to check current state"],
  "warnings": []
}
EOF
exit 1
