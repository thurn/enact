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
# e.g. /path/to/tasks/task_42.md -> 42
task_id=$(basename "$task_file" .md | sed 's/^task_//')

raw_file="${scratch_dir}/CODEX_quality_${task_id}.raw"
review_file="${scratch_dir}/REVIEW_quality_${task_id}.md"

blockers=()
suggestions=()
codex_status="completed"

# ---------------------------------------------------
# Phase 1: Run Codex Review
# ---------------------------------------------------
if ! timeout 480 bash -c \
  "cd '$worktree_dir' && codex review \
    --base '$main_branch'" \
  &> "$raw_file"; then
  codex_status="failed"
  echo >&2 "WARNING: codex review failed or timed out"
fi

# ---------------------------------------------------
# Phase 2: Internal Tooling Leaks Check
# ---------------------------------------------------
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

# Build a combined grep pattern
leak_regex=$(IFS='|'; echo "${leak_patterns[*]}")

# Get changed files in the worktree
changed_files=$(
  cd "$worktree_dir" && \
  git diff --name-only "$main_branch" -- \
    '*.py' '*.js' '*.ts' '*.tsx' '*.jsx' \
    '*.rs' '*.go' '*.java' '*.rb' '*.sh' \
    '*.md' '*.txt' '*.yaml' '*.yml' '*.toml' \
    '*.json' '*.css' '*.html' 2>/dev/null || true
)

if [ -n "$changed_files" ]; then
  while IFS= read -r file; do
    filepath="${worktree_dir}/${file}"
    [ -f "$filepath" ] || continue

    matches=$(grep -nE "$leak_regex" "$filepath" \
      2>/dev/null || true)
    if [ -n "$matches" ]; then
      while IFS= read -r match; do
        blockers+=(
          "### Tooling Leak in ${file}
- **Severity**: blocker
- **Category**: tooling-leak
- **Source**: manual
- **File**: ${file}:$(echo "$match" \
  | cut -d: -f1)
- **Issue**: Internal tooling reference found: \
$(echo "$match" | cut -d: -f2-)
- **Recommendation**: Remove all references to \
internal tooling, planning frameworks, and \
orchestration concepts. The codebase must read as \
if no planning framework exists."
        )
      done <<< "$matches"
    fi
  done <<< "$changed_files"
fi

# ---------------------------------------------------
# Phase 3: Parse Codex Output
# ---------------------------------------------------
if [ "$codex_status" = "completed" ] \
  && [ -f "$raw_file" ] \
  && [ -s "$raw_file" ]; then

  # Extract P1 findings as blockers
  # Codex uses markdown headers and severity markers
  p1_findings=$(grep -B2 -A5 -iE \
    '(P1|high|critical|error)' "$raw_file" \
    2>/dev/null || true)
  if [ -n "$p1_findings" ]; then
    blockers+=(
      "### Codex High-Priority Finding
- **Severity**: blocker
- **Category**: codex-finding
- **Source**: codex
- **Issue**: See ${raw_file} for details
- **Recommendation**: Review and address \
high-priority findings from Codex analysis"
    )
  fi

  # Extract P2 findings as suggestions (skip P3)
  p2_findings=$(grep -B2 -A5 -iE \
    '(P2|medium|warning)' "$raw_file" \
    2>/dev/null || true)
  if [ -n "$p2_findings" ]; then
    suggestions+=(
      "### Codex Medium-Priority Finding
- **Severity**: suggestion
- **Category**: codex-finding
- **Source**: codex
- **Issue**: See ${raw_file} for details
- **Recommendation**: Review and address \
medium-priority findings from Codex analysis"
    )
  fi
fi

# ---------------------------------------------------
# Phase 4: Write Findings
# ---------------------------------------------------
blocker_count=${#blockers[@]}
suggestion_count=${#suggestions[@]}
total=$((blocker_count + suggestion_count))

if [ "$total" -gt 0 ]; then
  {
    echo "# Quality Review — Task ${task_id}"
    echo ""
    echo "## Findings"
    echo ""
    for finding in "${blockers[@]}"; do
      echo "$finding"
      echo ""
    done
    for finding in "${suggestions[@]}"; do
      echo "$finding"
      echo ""
    done
    echo "## Summary"
    echo ""
    echo "- **Blockers**: ${blocker_count}"
    echo "- **Suggestions**: ${suggestion_count}"
    echo "- **Codex status**: ${codex_status}"
  } > "$review_file"

  echo "REVISE: REVIEW_quality_${task_id}.md"
else
  echo "PASS"
fi
