#!/usr/bin/env bash
# write-approval-state.sh — Write approval state for a feature
# Usage: write-approval-state.sh <feature-name> <json-string>
# Uses SPEC_VALIDATE_STATE_DIR env var if set, otherwise default path

set -e

FEATURE="${1:-}"
JSON_DATA="${2:-}"

if [[ ! "$FEATURE" =~ ^[0-9]{3,}-[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "Error: Expected a numbered feature directory name. Usage: write-approval-state.sh <feature-name> <json-string>" >&2
  exit 1
fi

if [ -z "$JSON_DATA" ]; then
  echo "Error: No JSON data provided. Usage: write-approval-state.sh <feature-name> <json-string>" >&2
  exit 1
fi

# Require a parser before creating or replacing state.
if ! command -v python3 >/dev/null 2>&1 || ! printf '%s' "$JSON_DATA" | python3 -c 'import json, sys
record = json.load(sys.stdin)
if not isinstance(record, dict) or not {"spec_self_validation", "approval_status"} <= record.keys():
    raise ValueError("Missing approval fields")' 2>/dev/null; then
  echo "Error: Could not validate approval state" >&2
  exit 1
fi

# Determine state directory
if [ -n "$SPEC_VALIDATE_STATE_DIR" ]; then
  STATE_DIR="$SPEC_VALIDATE_STATE_DIR"
else
  _dir="$(pwd)"
  while [ "$_dir" != "/" ]; do
    if [ -d "$_dir/.specify" ]; then
      break
    fi
    _dir="$(dirname "$_dir")"
  done
  STATE_DIR="$_dir/.specify/extensions/spec-validate/status"
fi

# Create directory if needed
mkdir -p "$STATE_DIR"

STATE_FILE="$STATE_DIR/${FEATURE}.json"

# Keep local state private and treat its destination as data.
umask 077
printf '%s' "$JSON_DATA" | python3 -c 'import json, sys
with open(sys.argv[1], "w", encoding="utf-8") as state:
    json.dump(json.load(sys.stdin), state, indent=2)
    state.write("\n")' "$STATE_FILE"

echo "State written to $STATE_FILE" >&2
