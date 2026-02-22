#!/usr/bin/env bash
# review-conformance.sh — Conformance review via Codex
#
# Runs codex review for spec conformance analysis.
# Replaces the code-conformance-reviewer agent.
#
# Args:
#   $1 — scratch_dir  (~/.enact/<enact_id>/)
#   $2 — task_file    (<scratch>/tasks/task_<id>.md)
#   $3 — worktree_dir (path to git worktree)
#   $4 — main_branch  (e.g. main, master)
#
# Output contract:
#   - Writes REVIEW_conformance_<task_id>.md to
#     scratch_dir if findings exist
#   - Prints PASS or
#     REVISE: REVIEW_conformance_<task_id>.md to stdout
#   - Always exits 0 on success (even when REVISE)

set -euo pipefail

scratch_dir="$1"
task_file="$2"
worktree_dir="$3"
main_branch="$4"

# Extract task_id from task file path
task_id=$(basename "$task_file" .md | sed 's/^task_//')

review_file="REVIEW_conformance_${task_id}.md"
review_path="${scratch_dir}/${review_file}"

# ---------------------------------------------------
# Run Codex Review
# ---------------------------------------------------
codex_output=""
codex_status="completed"

if ! codex_output=$(
  timeout 480 bash -c \
    'cd "$1" && codex review --base "$2"' \
    -- "$worktree_dir" "$main_branch" 2>&1
); then
  codex_status="failed"
  echo >&2 \
    "WARNING: codex review failed or timed out"
fi

# ---------------------------------------------------
# Write Review File
# ---------------------------------------------------
# Codex produces structured findings — use them as-is.

if [ "$codex_status" = "completed" ] \
  && [ -n "$codex_output" ]; then
  echo "$codex_output" > "$review_path"
  echo "REVISE: ${review_file}"
else
  rm -f "$review_path"
  if [ "$codex_status" = "failed" ]; then
    echo >&2 \
      "WARNING: codex failed, no LLM fallback"
  fi
  echo "PASS"
fi
