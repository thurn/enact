#!/usr/bin/env bash
# review-conformance.sh — Conformance review via Codex CLI
#
# Runs codex review with task/plan context for spec
# conformance analysis. Replaces the
# code-conformance-reviewer agent.
#
# Args:
#   $1 — scratch_dir  (~/.enact/<enact_id>/)
#   $2 — task_file    (<scratch>/tasks/task_<id>.md)
#   $3 — worktree_dir (path to git worktree)
#   $4 — main_branch  (e.g. main, master)
#   $5 — plan_file    (path to PLAN.md)
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
plan_file="$5"

# Extract task_id from task file path
# e.g. /path/to/tasks/task_42.md -> 42
task_id=$(basename "$task_file" .md | sed 's/^task_//')

raw_file="${scratch_dir}/CODEX_conformance_${task_id}.raw"
review_file="${scratch_dir}/REVIEW_conformance_${task_id}.md"

codex_status="completed"

# ---------------------------------------------------
# Phase 1: Build Conformance Prompt
# ---------------------------------------------------
prompt="Review this code for conformance against the "
prompt+="following requirements."
prompt+=$'\n\n'
prompt+="## Task Specification"
prompt+=$'\n\n'

if [ -f "$task_file" ]; then
  prompt+=$(cat "$task_file")
else
  echo >&2 "WARNING: task file not found: $task_file"
  prompt+="(task file not available)"
fi

prompt+=$'\n\n'
prompt+="## Project Plan"
prompt+=$'\n\n'

if [ -f "$plan_file" ]; then
  prompt+=$(cat "$plan_file")
else
  echo >&2 "WARNING: plan file not found: $plan_file"
  prompt+="(plan file not available)"
fi

prompt+=$'\n\n'
prompt+="Focus on whether the implementation satisfies "
prompt+="every requirement and acceptance criterion. "
prompt+="Ignore code style, naming, and structural "
prompt+="quality — only check that the right thing "
prompt+="was built."

# ---------------------------------------------------
# Phase 2: Run Codex Review with Prompt
# ---------------------------------------------------
if ! echo "$prompt" | timeout 480 bash -c \
  "cd '$worktree_dir' && codex review \
    --base '$main_branch' -" \
  &> "$raw_file"; then
  codex_status="failed"
  echo >&2 \
    "WARNING: codex review failed or timed out"
fi

# ---------------------------------------------------
# Phase 3: Parse Codex Output
# ---------------------------------------------------
blockers=()
suggestions=()

if [ "$codex_status" = "completed" ] \
  && [ -f "$raw_file" ] \
  && [ -s "$raw_file" ]; then

  # Extract high-priority findings as blockers
  p1_findings=$(grep -B2 -A5 -iE \
    '(P1|high|critical|error|missing|bug)' \
    "$raw_file" 2>/dev/null || true)
  if [ -n "$p1_findings" ]; then
    blockers+=(
      "### Codex Conformance Finding (High)
- **Severity**: blocker
- **Source**: codex
- **Issue**: See ${raw_file} for details
- **Recommendation**: Review and address \
conformance findings from Codex analysis"
    )
  fi

  # Extract medium-priority findings as suggestions
  p2_findings=$(grep -B2 -A5 -iE \
    '(P2|medium|warning|partial)' \
    "$raw_file" 2>/dev/null || true)
  if [ -n "$p2_findings" ]; then
    suggestions+=(
      "### Codex Conformance Finding (Medium)
- **Severity**: suggestion
- **Source**: codex
- **Issue**: See ${raw_file} for details
- **Recommendation**: Review and address \
medium-priority conformance findings"
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
    echo "# Conformance Review — Task ${task_id}"
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

  echo "REVISE: REVIEW_conformance_${task_id}.md"
else
  if [ "$codex_status" = "failed" ]; then
    echo >&2 \
      "WARNING: codex failed, no LLM fallback available"
  fi
  echo "PASS"
fi
