#!/usr/bin/env bash
# read-approval-state.sh — Read approval state for a feature
# Usage: read-approval-state.sh <feature-name>
# Output: JSON to stdout
# Uses SPEC_VALIDATE_STATE_DIR env var if set, otherwise default path

set -e

FEATURE="${1:-}"

if [[ ! "$FEATURE" =~ ^[0-9]{3,}-[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "Error: Expected a numbered feature directory name. Usage: read-approval-state.sh <feature-name>" >&2
  exit 1
fi

# Determine state directory
if [ -n "$SPEC_VALIDATE_STATE_DIR" ]; then
  STATE_DIR="$SPEC_VALIDATE_STATE_DIR"
else
  # Find project root
  _dir="$(pwd)"
  while [ "$_dir" != "/" ]; do
    if [ -d "$_dir/.specify" ]; then
      break
    fi
    _dir="$(dirname "$_dir")"
  done
  STATE_DIR="$_dir/.specify/extensions/spec-validate/status"
fi

STATE_FILE="$STATE_DIR/${FEATURE}.json"

if [ ! -f "$STATE_FILE" ]; then
  # Return default state
  cat <<EOF
{
  "feature": "$FEATURE",
  "spec_hash": null,
  "tasks_hash": null,
  "spec_self_validation": "not-run",
  "tasks_self_validation": "not-run",
  "spec_validated_at": null,
  "tasks_validated_at": null,
  "spec_critical_count": 0,
  "spec_missed_items": [],
  "review_status": null,
  "tasks_review_status": null,
  "review_requested_at": null,
  "reviewer": null,
  "review_comments": [],
  "approval_status": "blocked",
  "warnings": [],
  "override": {"used": false, "reason": null, "by": null},
  "timeout_self_approval": {"used": false, "reason": null}
}
EOF
  exit 0
fi

# Reject unreadable or malformed documents, including when the parser is absent.
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import json, sys
with open(sys.argv[1], encoding="utf-8") as state:
    if not isinstance(json.load(state), dict):
        raise ValueError("Expected a JSON object")' "$STATE_FILE" 2>/dev/null; then
  echo "Error: Could not validate approval state" >&2
  exit 1
fi

cat "$STATE_FILE"
