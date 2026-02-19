#!/usr/bin/env bash
# task-search.sh — Search Claude Code tasks by metadata.
#
# Usage: ./scripts/task-search.sh <field> <value>
#
# Searches task JSON files in ~/.claude/tasks/ for tasks
# where the given field contains the given value.
# Requires jq.
#
# Examples:
#   ./scripts/task-search.sh tag qa
#   ./scripts/task-search.sh status completed
#   ./scripts/task-search.sh title "feature coder"

set -euo pipefail

if [ $# -lt 2 ]; then
  echo "Usage: $0 <field> <value>"
  exit 1
fi

FIELD="$1"
VALUE="$2"
TASKS_DIR="$HOME/.claude/tasks"

if [ ! -d "$TASKS_DIR" ]; then
  echo "Error: Tasks directory not found: $TASKS_DIR"
  exit 1
fi

if ! command -v jq &>/dev/null; then
  echo "Error: jq is required but not installed"
  exit 1
fi

for f in "$TASKS_DIR"/*.json; do
  [ -f "$f" ] || continue
  MATCH=$(jq -r --arg field "$FIELD" --arg val "$VALUE" '
    if (.[$field] // "" | tostring |
        ascii_downcase | contains($val |
        ascii_downcase)) then .id
    elif ((.[$field] // []) | type == "array") and
         ((.[$field] // []) | map(tostring |
         ascii_downcase) |
         any(contains($val | ascii_downcase))) then .id
    else empty
    end
  ' "$f" 2>/dev/null) || continue
  if [ -n "$MATCH" ]; then
    jq -r '"[\(.id)] \(.title // .name // "untitled")"' \
      "$f" 2>/dev/null
  fi
done
