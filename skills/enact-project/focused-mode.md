# Focused Mode Pipeline

Focused mode runs a streamlined pipeline for
isolated, single-system changes. It skips task
generation and decomposition, treating the entire
plan as a single implementation task.

## State Machine (Focused)

| State           | Description                        |
|-----------------|------------------------------------|
| RESEARCH        | Surveyor (2-3 assignments),        |
|                 | Researchers (no Synthesizer)       |
| SCOPE_SELECTION | Scope confirmed as focused         |
| PLANNING        | Planner only (no Plan Refiner)     |
| PLAN_APPROVAL   | User reviews and approves plan     |
| TASK_PIPELINE   | Single Feature Coder, Code Review, |
|                 | optional Review Feedback Coder     |
| POST_TASK       | Metacognition pipeline, Technical  |
|                 | Writer                             |
| COMPLETE        | All work done                      |

No TASK_GENERATION state. No Synthesizer in
RESEARCH. No Integration Reviewer in POST_TASK.

## Research Phase (Focused)

Spawn a Surveyor with the user's prompt. The
Surveyor creates 2-3 research assignments. Spawn
Researchers in parallel (one per assignment). Do
**not** spawn a Synthesizer — with only 2-3 result
files, the Planner reads them directly.

After Researchers complete, pass the research
result file paths to the Planner (see below).

## Planning Phase (Focused)

Spawn a Planner to create PLAN.md. In the Planner's
prompt, note that there is no RESEARCH.md — instead
list the research result file paths
(`<scratch>/research/<N>_result.md`) so the Planner
reads them directly.

Do NOT spawn a Plan Refiner. Proceed directly to
PLAN_APPROVAL.

## Plan Approval Phase (Focused)

Identical to full mode. The user reviews and approves
the plan before implementation begins.

## Task Pipeline (Focused)

Instead of generating tasks, the Orchestrator creates
a single synthetic task:

1. Create `<scratch>/tasks/task_1.md` with:
   - `id: 1`, `status: pending`, `owner: ""`
   - `tags: [feature]`, `blocked_by: []`
   - Title: the project title from the user's prompt
   - Body: "Implement the plan described in
     `~/.enact/<enact_id>/PLAN.md`. Read the full
     plan for requirements and acceptance criteria."

2. Create a worktree (skip in no-worktrees mode) and
   spawn a Feature Coder for task 1, passing the task
   file path, worktree_dir, project_dir, and
   main_branch.

3. After the Feature Coder completes, run code review
   scripts in parallel:
   - `review-quality.sh <scratch> <task_file>
     <worktree_dir> <main_branch>`
   - `review-conformance.sh <scratch> <task_file>
     <worktree_dir> <main_branch>`

4. If any reviewer returns REVISE, spawn a Review
   Feedback Coder.

5. Merge the worktree to main_branch using the
   standard worktree lifecycle (rebase, fast-forward
   merge, cleanup). Skip in no-worktrees mode — code
   is already on main.

No concurrency is needed — there is exactly one task.

## Post-Task Phase (Focused)

After the single task completes:

- **Skip** the Integration Reviewer (the change is
  isolated; conformance and quality review already
  validated it).
- **Run** the Metacognition Phase (Meta-Surveyor,
  Mini-Metacognizers, Enact Metacognizer) — this
  always runs regardless of scope.
- **Run** the Technical Writer — this always runs
  regardless of scope.

## Agents Skipped in Focused Mode

| Agent              | Reason                          |
|--------------------|---------------------------------|
| Synthesizer        | 2-3 result files are small      |
|                    | enough for the Planner to read  |
| Plan Refiner       | Single-system changes rarely    |
|                    | need a second planning pass     |
| Task Generator     | No decomposition needed         |
| Task Refiner       | No tasks to validate            |
| Integration        | No multi-task integration to    |
| Reviewer           | validate                        |
| QA Scenario        | No multi-task QA matrix         |
| Generator          |                                 |
| Manual QA Tester   | Feature Coder testing suffices  |
| Bugfix Coder       | No QA bugs to fix               |

## Upgrading to Full Mode

If the Feature Coder reports that the scope is larger
than expected (e.g., reports it cannot complete the
work in one task, or flags scope concerns), the
Orchestrator should:

1. Notify the user that focused scope may be
   insufficient.
2. Ask whether to continue in focused mode or switch
   to full mode via AskUserQuestion.
3. If switching: enter TASK_GENERATION with the
   existing PLAN.md. The Feature Coder's partial work
   in the worktree is preserved — merge it first,
   then the Task Generator creates tasks that build
   on it.
