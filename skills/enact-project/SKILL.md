---
name: enact-project
description: Instructions for completing a greenfield software engineering project with Enact. Covers session setup, agent selection, orchestrator state machine, worktree management, and the full project lifecycle.
---

This skill describes the Enact workflow for **greenfield
software engineering projects** — adding new features,
building new systems, or extending existing codebases. The
`enact` skill is the generic orchestration layer; this
skill defines the specific project lifecycle.

## Session Setup

1. Create an Enact ID via `date +%s`
2. Resolve the absolute scratch directory path: run
   `echo $HOME` and construct the path as
   `<absolute_home>/.enact/<enact_id>/` (e.g.,
   `/home/user/.enact/1234567890/`). **Never use `~`** —
   different agents may have different `$HOME` directories,
   so `~` can resolve to different filesystem locations.
   All agents must receive the same absolute path.
3. Create the scratch directory with `mkdir -p`
4. Initialize `<scratch>/ORCHESTRATOR_STATE.md` (see
   below)
5. Report the scratch directory to the user immediately

## Agent Selection

There is no AGENTS.md file. Instead, the Orchestrator
collaboratively decides which agents to include with the
user.

### Default-On Agents

These agents run unless the user explicitly opts out:

- **Surveyors** — breadth-first analysis of the problem
  domain, creating research assignments
- **Researchers** — deep-dive into specific topics from
  surveyor assignments (run in parallel)
- **Synthesizer** — combines research into RESEARCH.md
- **Planner** — writes PLAN.md technical design
- **Task Generator** — breaks plan into Claude Code tasks
- **Feature Coders** — implement tasks (one at a time, in
  git worktrees)
- **Code Conformance Reviewers** — verify implementation
  matches spec
- **Code Quality Reviewers** — audit code quality
- **Review Feedback Coders** — implement review feedback
- **Enact Metacognizer** — post-session self-improvement

### Default-Off Agents

These agents run only when the user requests them or the
Orchestrator recommends them via AskUserQuestion:

- **Interviewer** — clarify ambiguous requirements
- **Plan Refiner** — audit complex plans
- **Task Refiner** — validate task completeness
- **QA Scenario Generator** — generate manual QA scenarios
- **Manual QA Tester** — execute QA scenarios per task
- **Bugfix Coder** — fix bugs found during QA
- **Subject Matter Expert Reviewers** — domain-specific
  review passes
- **Integration Reviewer** — end-to-end validation
- **Technical Writer** — documentation updates

Use AskUserQuestion to propose optional agents when the
project characteristics warrant them. For example: "This
project has CLI-exercisable output. Should I include QA
testing?" or "The plan is complex. Should I include a Plan
Refiner pass?"

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

1. Read the subagent's return message (brief — details
   are in files)
2. Update ORCHESTRATOR_STATE.md
3. Determine the next subagent based on the state machine
   and Claude Code task statuses
4. Spawn it with `run_in_background: true`
5. DO NOT POLL. End your turn and wait for a completion
   notification.

## ORCHESTRATOR_STATE.md

After **every** subagent completes, overwrite
`~/.enact/<enact_id>/ORCHESTRATOR_STATE.md` with the
current state. This file is your persistent memory — it
survives context compaction.

Include:

- Current state and pipeline position
- Which optional agents were selected
- What just completed
- What comes next
- A checklist of all completed pipeline steps

This is your **primary recovery mechanism**. If your
context compacts and you lose track, re-read this file and
Claude Code task statuses to fully reconstruct your state.

## Research Phase

Spawn a Surveyor to analyze the problem domain. The
Surveyor creates research assignments. Then spawn
parallel Researchers (one per assignment). Finally spawn
a Synthesizer to combine findings into
`~/.enact/<enact_id>/RESEARCH.md`.

In high-effort mode, multiple survey-research-synthesize
rounds can be conducted, exploring discovered topics in
greater detail.

## Planning Phase

Spawn a Planner to create
`~/.enact/<enact_id>/PLAN.md`. If the Plan Refiner was
selected, spawn it to audit the plan. If the Interviewer
was selected, spawn it before or after planning to
resolve ambiguities.

The Planner should load the `enact-planner` skill
(`~/enact/skills/enact-planner/SKILL.md`) for planning
quality standards.

## Task Generation Phase

Spawn the Task Generator to break the plan into Claude
Code tasks. If the Task Refiner was selected, spawn it
to validate tasks. If QA was selected, spawn the QA
Scenario Generator after task generation.

Tasks are created as Claude Code tasks (via TaskCreate).
Each task includes a description, acceptance criteria,
and dependencies on other tasks.

## Task Pipeline

**Tasks execute serially — one at a time.** Even if
multiple tasks are unblocked, run the complete per-task
pipeline for one task before starting the next.

### Git Worktrees

Each Feature Coder works in a git worktree to isolate
changes:

1. Before spawning a Feature Coder, create a worktree:
   `git worktree add ~/.enact/<enact_id>/task_<id>
   -b enact/<enact_id>/task_<id>`
2. Tell the Feature Coder to work in the worktree path
3. After code review passes (and optional QA), merge:
   `git checkout main &&
   git merge enact/<enact_id>/task_<id>`
4. Clean up: `git worktree remove
   ~/.enact/<enact_id>/task_<id> &&
   git branch -d enact/<enact_id>/task_<id>`

### Per-Task Pipeline

For each task, run these subagents sequentially:

1. **Feature Coder** — implement the task in its worktree
2. **Code Conformance Reviewer** — verify spec conformance
3. **Code Quality Reviewer** — audit code quality
4. **(Optional) SME Reviewer** — domain-specific review
5. **Review Feedback Coder** — if any reviewer returned
   REVISE, implement feedback
6. **(Optional) Manual QA Tester** — execute QA scenarios
   for this task
7. **(Optional) Bugfix Coder** — fix bugs found during QA
8. Merge the worktree to main

All code review agents return either the single word
`PASS` or `REVISE: <path_to_review_doc>`. Read-only
review agents (conformance, quality, SME) can run in
parallel with each other.

After code review (and Review Feedback if needed), check
whether QA scenarios exist for this task. If they do,
spawn the Manual QA Tester. If the tester finds bugs,
spawn a Bugfix Coder before merging.

Record QA status in ORCHESTRATOR_STATE.md for each task.

## Post-Task Phase

After all tasks complete, run selected post-task agents:

- **Integration Reviewer** — validates the entire
  pipeline end-to-end. Runs at most 2 rounds. If the
  second round still files tasks, present remaining
  findings to the user.
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

Do NOT write detailed instructions that duplicate or
override the agent's own definition. Each agent has its
own prompt and loads its own skills. Your job is to give
it the right *inputs*, not to redefine its *behavior*.

## Context Discipline

Subagents return **brief** summaries (3-5 lines). You do
NOT need their full output in your context.

**Do NOT**: read source code files, read detailed review
findings, accumulate long subagent outputs, or repeat
summaries back to the user verbatim.

**DO**: track state via ORCHESTRATOR_STATE.md, report
brief progress, pass file paths between subagents (not
file contents), trust that subagents wrote their results
to the correct files.

## Error Recovery

If a subagent fails (returns with errors, runs out of
context, or reports it could not finish):

1. Read the subagent's output to understand what was
   accomplished.
2. For **Feature Coders**: spawn a new Feature Coder with
   the same task plus context about what the previous
   coder accomplished. If no progress was made, check
   whether the task description needs refinement.
3. For **Code Review with systemic problems**: if the
   implementation is fundamentally wrong, update the task
   and re-spawn a Feature Coder.
4. For **any subagent**: if the failure appears transient
   (timeout, context exhaustion), retry once before
   escalating.

## Progress Reporting

After each per-task pipeline completes, report:

    Task N/M complete: [task title]

After all tasks complete:

    All N tasks complete. Running post-task pipeline.

## Context Recovery

If you are ever uncertain about the project state,
re-read:

1. `~/.enact/<enact_id>/ORCHESTRATOR_STATE.md`
2. Claude Code task statuses (via TaskList/TaskGet)

Do this **proactively** every 5-10 subagent rounds, not
just when confused.
