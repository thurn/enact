---
name: enact-project
description: Instructions for completing a greenfield software engineering project with Enact. Covers session setup, agent selection, orchestrator state machine, worktree management, and the full project lifecycle.
---

This skill describes the Enact workflow for **greenfield software engineering
projects** — adding new features, building new systems, or extending existing
codebases. The `enact` skill is the generic orchestration layer; this skill
defines the specific project lifecycle.

## Session Setup

1. Create an Enact ID via `date +%s`
2. Resolve the scratch directory path:
   `~/.enact/<enact_id>/`
3. Create the scratch directory with `mkdir -p`
4. Detect the main branch name:
   `git symbolic-ref refs/remotes/origin/HEAD |
   sed 's@^refs/remotes/origin/@@'`
   If this fails, ask the user. Store as
   `main_branch` and pass it to every pipeline agent.
5. Clean up stale worktrees from previous sessions:
   ```
   git worktree list
   ```
   For any worktrees matching `~/.enact/*/task_*`,
   remove them:
   ```
   git worktree remove --force <path>
   git branch -D <branch>
   ```
6. Initialize `<scratch>/ORCHESTRATOR_STATE.md` (see
   below)
7. Report the scratch directory to the user immediately

## Agent Selection

The Orchestrator collaboratively decides which agents to include with the user.

### Default-On Agents

These agents run unless the user explicitly opts out:

- **Surveyors** — breadth-first analysis of the problem domain, creating
  research assignments
- **Researchers** — deep-dive into specific topics from surveyor assignments
  (run in parallel)
- **Synthesizer** — combines research into RESEARCH.md
- **Planner** — writes PLAN.md technical design
- **Plan Refiner** — audit complex plans
- **Task Generator** — breaks plan into Claude Code tasks
- **Task Refiner** — validate task completeness
- **Feature Coders** — implement tasks (concurrently, in git worktrees)
- **Code Conformance Reviewers** — verify implementation matches spec
- **Code Quality Reviewers** — audit code quality
- **Review Feedback Coders** — implement review feedback
- **Integration Reviewer** — end-to-end validation
- **Technical Writer** — documentation updates
- **Enact Metacognizer** — post-session self-improvement

### Default-Off Agents

These agents run only when the user requests them or the Orchestrator recommends
them via AskUserQuestion:

- **Interviewer** — clarify ambiguous requirements
- **QA Scenario Generator** — generate manual QA scenarios
- **Manual QA Tester** — execute QA scenarios per task
- **Bugfix Coder** — fix bugs found during QA
- **Subject Matter Expert Reviewers** — domain-specific review passes

### Ad-Hoc Agents

These agents are not selected during Agent Selection.
The Orchestrator spawns them on demand:

- **Merge Conflict Resolver** — spawned during worktree
  merges when rebase conflicts are too complex to
  resolve inline.

Use AskUserQuestion to propose optional agents when the
project characteristics warrant them. For example: "This
project has CLI-exercisable output. Should I include QA
testing?"

## Orchestrator State Machine

You are always in one of these states:

| State           | Description                          |
|-----------------|--------------------------------------|
| RESEARCH        | Surveyor, Researchers, Synthesizer   |
| PLANNING        | Planner, optional Refiner/Interview  |
| TASK_GENERATION | Task Generator, optional refinement  |
| TASK_PIPELINE   | Per-task: Coder, Review, opt. QA     |
| POST_TASK       | Integration, Writer, Metacognizer    |
| COMPLETE        | All work done                        |

After each subagent completes:

1. Read the subagent's return message (brief — details are in files)
2. Update ORCHESTRATOR_STATE.md
3. Determine the next subagent based on the state machine and Claude Code task
   statuses
4. Spawn it with `run_in_background: true`
5. **End your turn and wait.** See Critical Rules.

## ORCHESTRATOR_STATE.md

After **every** subagent completes, overwrite
`~/.enact/<enact_id>/ORCHESTRATOR_STATE.md` with the current state. This file is
your persistent memory — it survives context compaction.

Include:

- Current state and pipeline position
- Which optional agents were selected
- Concurrency limit (default 3, or user-specified)
- Currently active task pipelines and their pipeline step
- What just completed
- What comes next
- A checklist of all completed pipeline steps

This is your **primary recovery mechanism**. If your context compacts and you
lose track, re-read this file and Claude Code task statuses to fully reconstruct
your state.

## Research Phase

Spawn a Surveyor to analyze the problem domain. The Surveyor creates research
assignments. Do not read the assignments, only note their names. Then spawn
parallel Researchers (one per assignment file). Finally spawn a Synthesizer to
combine findings into `~/.enact/<enact_id>/RESEARCH.md`. Do not read any
research files at any time.

In high-effort mode, multiple survey-research-synthesize rounds can be
conducted, exploring discovered topics in greater detail.

## Planning Phase

If the Interviewer was selected, spawn it before planning
to resolve ambiguities.

Spawn a Planner to create `~/.enact/<enact_id>/PLAN.md`.
If the Plan Refiner was selected, spawn it to audit the
plan. Do not read the plan.

## Task Generation Phase

Spawn the Task Generator to break the plan into Claude Code tasks. If the Task
Refiner was selected, spawn it to validate tasks. If QA was selected, spawn the
QA Scenario Generator after task generation.

Tasks are created as Claude Code tasks (via TaskCreate). Each task
includes a description, acceptance criteria, and dependencies on other
tasks. The Orchestrator never reads task content — it only tracks task
IDs and passes them to subagents, who read the tasks themselves.

## Task Pipeline

**Tasks execute concurrently — up to 3 at a time by default.** The user
can request a different concurrency limit during session setup. Whenever
a task pipeline completes (or a slot is otherwise free) and unblocked
tasks are available, spawn the next task's pipeline immediately. Each
task's per-task pipeline still runs its steps sequentially within its
own git worktree.

Task dependencies enforce code visibility: a blocked
task does not start until its dependencies are merged
to `<main_branch>`, which is when their code becomes
visible. This is by design — each worktree branches
from the current `<main_branch>`, so dependent code
must be merged first.

Track active task pipelines in ORCHESTRATOR_STATE.md.
Before spawning new task pipelines, verify the active
worktree count with `git worktree list` — do not rely
solely on ORCHESTRATOR_STATE.md for concurrency
tracking.

Process completions **one at a time**, in notification
order. This prevents merge conflicts from concurrent
fast-forward attempts. When a subagent completes:

1. The current task's pipeline has a next step — if so,
   spawn it.
2. A concurrency slot is free and unblocked tasks
   remain — if so, start a new task pipeline.

### Git Worktrees

Each task's pipeline runs in a single git worktree that
the Orchestrator creates and manages. All agents in
that task's pipeline (Feature Coder, reviewers, Review
Feedback Coder, QA Tester, Bugfix Coder) share the
same worktree. No agent creates or removes worktrees.

Orchestrator worktree lifecycle:

1. **Create** before spawning the Feature Coder:
   `git worktree add ~/.enact/<enact_id>/task_<id>
   -b enact/<enact_id>/task_<id> <main_branch>`
2. **Pass the path** to every agent in the pipeline as
   `worktree_dir` (along with `project_dir` for the
   main project directory)
3. **Rebase** before merging:
   ```
   cd ~/.enact/<enact_id>/task_<id>
   git fetch <project_dir> <main_branch>
   git rebase FETCH_HEAD
   ```
   Resolve simple conflicts yourself rather than
   spawning an agent — only spawn a Merge Conflict
   Resolver if conflicts are too complex to resolve
   inline.
4. **Fast-forward merge** after rebase succeeds:
   ```
   cd <project_dir>
   git checkout <main_branch>
   git merge --ff-only enact/<enact_id>/task_<id>
   ```
5. **Clean up**: `git worktree remove
   ~/.enact/<enact_id>/task_<id> &&
   git branch -d enact/<enact_id>/task_<id>`

### Per-Task Pipeline

For each task, run these pipeline phases in order:

1. **Feature Coder** — implement the task in its
   worktree
2. **Code Review** — spawn all applicable reviewers in
   parallel:
   - Code Conformance Reviewer
   - Code Quality Reviewer
   - (Optional) SME Reviewer
3. **Review Feedback Coder** — if any reviewer returned
   REVISE, implement feedback
4. **(Optional) Manual QA Tester** — execute QA
   scenarios for this task
5. **(Optional) Bugfix Coder** — fix bugs found during
   QA

All code-writing agents (Feature Coder, Review Feedback
Coder, Bugfix Coder) rebase onto `<main_branch>` before
reporting complete. This reduces merge conflicts at
merge time. The Orchestrator rebases again before the
fast-forward merge as a safety net.
6. Merge the worktree to `<main_branch>` and mark the
   task completed (only the Orchestrator marks tasks
   completed — pipeline agents do not)

All code review agents return either the single word
`PASS` or
`REVISE: REVIEW_<reviewer_type>_<task_id>.md`.

After code review (and Review Feedback if needed), check
`<scratch>/QA_SCENARIOS.md` for QA scenarios that
validate this task. If they exist, spawn the Manual QA
Tester. If the tester finds bugs, spawn a Bugfix Coder
before merging.

Record QA status in ORCHESTRATOR_STATE.md for each task.

## Post-Task Phase

After all tasks complete, run selected post-task agents:

- **Integration Reviewer** — validates the entire
  pipeline end-to-end. Runs at most 2 rounds. If the
  second round still files tasks, present remaining
  findings to the user. Corrective tasks filed by the
  Integration Reviewer follow the standard per-task
  pipeline: the Orchestrator re-enters TASK_PIPELINE
  state, creates worktrees, runs coders, code review,
  and merges back to main.
- **Technical Writer** — creates and updates
  documentation
- **Enact Metacognizer** — post-session review at
  `~/.enact/<enact_id>/META.md`

## Prompting Subagents

When spawning any subagent, keep your prompt **minimal**.
Provide:

- The enact scratch directory path (absolute)
- A brief project summary (1-3 sentences)
- Any user-specified constraints relevant to this agent
- Specific information this agent needs that is not in
  the scratch directory files
- For task-related agents: the Claude Code **task ID**
  (not the task content — the subagent reads the task
  itself via TaskGet)
- For pipeline agents: `worktree_dir`, `project_dir`,
  and `main_branch`

Reviewers discover changed files themselves via
`git diff`. You do not need to pass file lists.

Spawn agents by their `name` field as the
`subagent_type` parameter of the Task tool (e.g.,
`subagent_type: "feature-coder"`). Claude Code's
native agent system automatically loads each agent's
definition, including its configured model, tools,
and skills.

Do NOT write detailed instructions that duplicate or
override the agent's own definition. Each agent has
its own prompt and loads its own skills. Your job is
to give it the right *inputs*, not to redefine its
*behavior*.

## Context Discipline

Subagents return **brief** summaries (3-5 lines). You do NOT need their full
output in your context.

**Do NOT**: read task descriptions (pass task IDs to subagents
instead), read source code files, read detailed review findings,
accumulate long subagent outputs, or repeat summaries back to the
user verbatim.

**DO**: track state via ORCHESTRATOR_STATE.md, report brief progress, pass file
paths between subagents (not file contents), trust that subagents wrote their
results to the correct files.

## Error Recovery

If a subagent fails (returns with errors, runs out of context, or reports it
could not finish):

1. Read the subagent's output to understand what was accomplished.
2. For **Feature Coders**: spawn a new Feature Coder with the same task plus
   context about what the previous coder accomplished. If no progress was made,
   check whether the task description needs refinement.
3. For **Code Review with systemic problems**: if the implementation is
   fundamentally wrong, update the task and re-spawn a Feature Coder.
4. For **any subagent**: if the failure appears transient (timeout, context
   exhaustion), retry once before escalating.

### Failed Pipeline Cleanup

If a task pipeline fails irrecoverably:

1. Reset any partial changes in the worktree:
   `cd ~/.enact/<enact_id>/task_<id> && git checkout .`
2. Remove the worktree: `git worktree remove
   ~/.enact/<enact_id>/task_<id>`
3. Delete the branch: `git branch -D
   enact/<enact_id>/task_<id>`
4. Update the task status and record the failure in
   ORCHESTRATOR_STATE.md.

## Progress Reporting

After each per-task pipeline completes, report:

    Task N/M complete: [task title]

After all tasks complete:

    All N tasks complete. Running post-task pipeline.

## Context Recovery

If you are ever uncertain about the project state
(e.g. after context compaction), run these steps
before doing anything else:

1. `git worktree list` — shows which worktrees
   currently exist. Any `task_*` worktree means an
   agent is (or was) working on that task.
2. `TaskList` — shows task statuses.
3. Cross-reference worktrees with task statuses:
   - `in_progress` + worktree exists → agent is likely
     still running; wait for its completion notification
   - `in_progress` + no worktree → agent finished but
     wasn't merged; check for the branch with
     `git branch --list 'enact/*/task_*'` and merge if
     it exists, or reset the task to `pending` if gone
   - `pending` + worktree exists → task was spawned but
     not marked in_progress (treat as in_progress)
4. Re-read `~/.enact/<enact_id>/ORCHESTRATOR_STATE.md`

Do NOT spawn new agents until you have confirmed the
active worktree count is below the concurrency limit.

## Critical Rules

### Waiting for Agents (MOST IMPORTANT)

After launching background agents, you MUST **stop
generating immediately**. Do not send any tool calls,
do not write any messages, do not attempt to check on
the agents. End your turn. The system will deliver a
notification when each agent finishes.

**Do NOT do any of the following while waiting:**

- Do NOT call `Bash` with `sleep` + `tail` to poll
- Do NOT call `Read` on the agent's output file
- Do NOT call `TaskOutput` — this can KILL running
  background agents when interrupted
- Do NOT write a polling loop or repeated checks
- Do NOT send any message — just stop and wait

The correct pattern:
1. Launch agents with `run_in_background: true`
2. Optionally send a short status message to the user
3. **End your turn. Do nothing else.**

### Other Rules

- **ALL** Task tool invocations MUST use
  `run_in_background: true`
- NEVER use the `resume` parameter to continue
  agents — always spawn fresh agents
- NEVER read large files yourself — let agents read
  what they need
- Launch multiple agents in parallel when possible
  (single message, multiple Task calls)
