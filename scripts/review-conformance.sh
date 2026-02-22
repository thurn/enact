#!/usr/bin/env bash
# review-conformance.sh — Conformance review via Codex
#
# Args:
#   $1 — scratch_dir  (~/.enact/<enact_id>/)
#   $2 — task_file    (<scratch>/tasks/task_<id>.md)
#   $3 — worktree_dir (path to git worktree)
#   $4 — main_branch  (e.g. main, master)
#
# Output contract:
#   - REVIEW_conformance_<task_id>.md written by codex
#     only if there are findings
#   - Prints PASS or REVISE to stdout
#   - Always exits 0

set -euo pipefail

scratch_dir="$1"
task_file="$2"
worktree_dir="$3"
main_branch="$4"

task_id=$(basename "$task_file" .md | sed 's/^task_//')
review_file="REVIEW_conformance_${task_id}.md"
review_path="${scratch_dir}/${review_file}"

rm -f "$review_path"

prompt="Review the git diff against '${main_branch}' "
prompt+="for spec conformance. The task specification "
prompt+="is at '${task_file}' — read it and verify "
prompt+="the implementation satisfies every requirement "
prompt+="and acceptance criterion. Focus on whether the "
prompt+="right thing was built, not code style. "
prompt+="If you find issues, write findings to "
prompt+="'${review_path}'. "
prompt+="If there are no issues, do not create the "
prompt+="file."

timeout 480 codex exec \
  --full-auto \
  --add-dir "$scratch_dir" \
  -C "$worktree_dir" \
  "$prompt" \
  > /dev/null 2>&1 || true

if [ -f "$review_path" ]; then
  echo "REVISE: ${review_file}"
else
  echo "PASS"
fi
