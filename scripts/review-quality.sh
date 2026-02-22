#!/usr/bin/env bash
# review-quality.sh — Code quality review via Codex
#
# Args:
#   $1 — scratch_dir  (~/.enact/<enact_id>/)
#   $2 — task_file    (<scratch>/tasks/task_<id>.md)
#   $3 — worktree_dir (path to git worktree)
#   $4 — main_branch  (e.g. main, master)
#
# Output contract:
#   - REVIEW_quality_<task_id>.md written by codex
#     (and/or tooling leaks check) only if findings
#   - Prints PASS or REVISE to stdout
#   - Always exits 0

set -euo pipefail

scratch_dir="$1"
task_file="$2"
worktree_dir="$3"
main_branch="$4"

task_id=$(basename "$task_file" .md | sed 's/^task_//')
review_file="REVIEW_quality_${task_id}.md"
review_path="${scratch_dir}/${review_file}"

rm -f "$review_path"

# ---------------------------------------------------
# Tooling Leaks Check (codex cannot detect these)
# ---------------------------------------------------
# Only scan added lines to avoid flagging pre-existing
# content.

leak_patterns=(
  '[Ee]nact'
  'PLAN\.md'
  'per the plan'
  'task_[0-9]'
  'task [0-9]'
  '[Gg]ate [0-9]'
  '[Pp]hase [0-9]'
  'orchestrat'
  'pipeline'
  'subagent'
)
leak_regex=$(IFS='|'; echo "${leak_patterns[*]}")

added_lines=$(
  cd "$worktree_dir" && \
  git diff -U0 "$main_branch" 2>/dev/null \
    | awk '
      /^--- /{ next }
      /^\+\+\+ /{ file=substr($0,7); next }
      /^@@/{ split($3,a,","); line=int(a[1]); next }
      /^\+/{ print file ":" line ": " substr($0,2);
              line++ }
    ' || true
)

leaks=""
if [ -n "$added_lines" ]; then
  leaks=$(echo "$added_lines" \
    | grep -E "$leak_regex" 2>/dev/null || true)
fi

# ---------------------------------------------------
# Codex Review
# ---------------------------------------------------

prompt="Review the git diff against '${main_branch}' "
prompt+="for code quality: structure, duplication, "
prompt+="API design, complexity, test quality. "
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

# Append tooling leaks if any
if [ -n "$leaks" ]; then
  {
    echo ""
    echo "## Internal Tooling Leaks (Blockers)"
    echo ""
    echo "$leaks"
  } >> "$review_path"
fi

if [ -f "$review_path" ]; then
  echo "REVISE: ${review_file}"
else
  echo "PASS"
fi
