#!/usr/bin/env bash
# task-note.sh — Add a note to a Claude Code task's
# metadata JSON file.
#
# Usage: ./scripts/task-note.sh <task_id> "Note content"
#
# Appends a timestamped note to the task's JSON file in
# ~/.claude/tasks/. Requires jq.

set -euo pipefail

if [ $# -lt 2 ]; then
  echo "Usage: $0 <task_id> \"Note content\""
  exit 1
fi

TASK_ID="$1"
NOTE="$2"
TASKS_DIR="$HOME/.claude/tasks"
TASK_FILE="$TASKS_DIR/$TASK_ID.json"

if [ ! -f "$TASK_FILE" ]; then
  echo "Error: Task file not found: $TASK_FILE"
  exit 1
fi

if ! command -v jq &>/dev/null; then
  echo "Error: jq is required but not installed"
  exit 1
fi

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
NOTE_OBJ=$(jq -n \
  --arg ts "$TIMESTAMP" \
  --arg note "$NOTE" \
  '{timestamp: $ts, content: $note}')

jq --argjson note "$NOTE_OBJ" '
  .notes = ((.notes // []) + [$note])
' "$TASK_FILE" > "${TASK_FILE}.tmp" \
  && mv "${TASK_FILE}.tmp" "$TASK_FILE"

echo "Note added to task $TASK_ID"
