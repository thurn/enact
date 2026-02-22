#!/usr/bin/env bash
# review-quality.sh — Code quality review via Codex CLI
#
# Runs codex review for structural quality analysis and
# checks for internal tooling leaks. Replaces the
# code-quality-reviewer agent.
#
# Args:
#   $1 — scratch_dir  (~/.enact/<enact_id>/)
#   $2 — task_file    (<scratch>/tasks/task_<id>.md)
#   $3 — worktree_dir (path to git worktree)
#   $4 — main_branch  (e.g. main, master)
#
# Output contract:
#   - Writes REVIEW_quality_<task_id>.md to scratch_dir
#     if findings exist
#   - Prints PASS or REVISE: REVIEW_quality_<task_id>.md
#     to stdout
#   - Always exits 0 on success (even when REVISE)

set -euo pipefail

scratch_dir="$1"
task_file="$2"
worktree_dir="$3"
main_branch="$4"

# Extract task_id from task file path
task_id=$(basename "$task_file" .md | sed 's/^task_//')

review_file="REVIEW_quality_${task_id}.md"
review_path="${scratch_dir}/${review_file}"
has_findings=false

# ---------------------------------------------------
# Phase 1: Internal Tooling Leaks Check
# ---------------------------------------------------
# Codex cannot detect these — this is the one piece
# of manual analysis the script must do.

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

changed_files=$(
  cd "$worktree_dir" && \
  git diff --name-only "$main_branch" 2>/dev/null \
    || true
)

leaks=""
if [ -n "$changed_files" ]; then
  while IFS= read -r file; do
    filepath="${worktree_dir}/${file}"
    [ -f "$filepath" ] || continue
    matches=$(grep -nE "$leak_regex" "$filepath" \
      2>/dev/null || true)
    if [ -n "$matches" ]; then
      leaks+="### Tooling Leak in ${file}"$'\n'
      leaks+="${matches}"$'\n\n'
    fi
  done <<< "$changed_files"
fi

# ---------------------------------------------------
# Phase 2: Run Codex Review
# ---------------------------------------------------
codex_output=""
codex_status="completed"

if ! codex_output=$(
  timeout 480 bash -c \
    'cd "$1" && codex review --base "$2"' \
    -- "$worktree_dir" "$main_branch" 2>&1
); then
  codex_status="failed"
  echo >&2 "WARNING: codex review failed or timed out"
fi

# ---------------------------------------------------
# Phase 3: Assemble Review File
# ---------------------------------------------------
# If codex produced findings, use its output directly.
# Append tooling leaks if any were found.

{
  if [ "$codex_status" = "completed" ] \
    && [ -n "$codex_output" ]; then
    echo "$codex_output"
    echo ""
    has_findings=true
  fi

  if [ -n "$leaks" ]; then
    echo "## Internal Tooling Leaks (Blockers)"
    echo ""
    echo "$leaks"
    has_findings=true
  fi
} > "$review_path"

if [ "$has_findings" = true ]; then
  echo "REVISE: ${review_file}"
else
  rm -f "$review_path"
  echo "PASS"
fi
