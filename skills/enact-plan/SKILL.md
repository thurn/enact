---
name: enact-plan
description: >-
  Use when creating a technical project plan using Enact
  subagents. Stops after planning — does not generate tasks
  or write code. Triggers on requests to plan a feature,
  create a technical design, or write a project plan using
  Enact.
---

This skill describes the Enact workflow for **planning
only** — researching a problem domain and producing a
standalone technical design document. Unlike `enact-project`,
this skill stops after the plan is complete. No tasks are
generated and no code is written.

The final plan is written to a user-specified output file.
If the user did not provide an output path, ask for one
before starting.

## Session Setup

1. **Resolve the output file.** If the user provided a
   markdown file path, store it as `output_file`. If not,
   use AskUserQuestion to ask where to write the plan.
2. Create an Enact ID via `date +%s`
3. Resolve the scratch directory path:
   `~/.enact/<enact_id>/`
4. Create the scratch directory with `mkdir -p`
5. Detect the main branch name:
   `git symbolic-ref refs/remotes/origin/HEAD |
   sed 's@^refs/remotes/origin/@@'`
   If this fails, ask the user. Store as `main_branch`
   and pass it to every agent.
6. Initialize `<scratch>/ORCHESTRATOR_STATE.md` (see
   below)
7. Report the scratch directory and output file to the
   user immediately

## Agent Selection

The Orchestrator collaboratively decides which agents to
include with the user.

### Default-On Agents

These agents run unless the user explicitly opts out:

- **Surveyors** — breadth-first analysis of the problem
  domain, creating research assignments
- **Researchers** — deep-dive into specific topics from
  surveyor assignments (run in parallel)
- **Synthesizer** — combines research into RESEARCH.md
- **Planner** — writes PLAN.md technical design
- **Plan Refiner** — audit the plan

### Default-Off Agents

These agents run only when the user requests them or
the Orchestrator recommends them via AskUserQuestion:

- **Interviewer** — clarify ambiguous requirements

Use AskUserQuestion to propose the Interviewer when the
project requirements seem ambiguous or underspecified.

## Orchestrator State Machine

You are always in one of these states:

| State     | Description                          |
|-----------|--------------------------------------|
| INTERVIEW | Optional Interviewer                 |
| RESEARCH  | Surveyor, Researchers, Synthesizer   |
| PLANNING  | Planner, optional Plan Refiner       |
| COMPLETE  | Plan delivered to output file        |

After each subagent completes:

1. Read the subagent's return message (brief — details
   are in files)
2. Update ORCHESTRATOR_STATE.md
3. Determine the next subagent based on the state machine
4. Spawn it with `run_in_background: true`
5. DO NOT POLL. End your turn and wait for a completion
   notification.

## ORCHESTRATOR_STATE.md

After **every** subagent completes, overwrite
`~/.enact/<enact_id>/ORCHESTRATOR_STATE.md` with the
current state. This file is your persistent memory — it
survives context compaction.

Include:

- Current state
- Which optional agents were selected
- Output file path
- What just completed
- What comes next
- A checklist of all completed steps

This is your **primary recovery mechanism**. If your
context compacts and you lose track, re-read this file to
fully reconstruct your state.

## Interview Phase

If the Interviewer was selected, spawn it first to
resolve ambiguities before research begins.

## Research Phase

Spawn a Surveyor to analyze the problem domain. The
Surveyor creates research assignments. Do not read the
assignments, only note their names. Then spawn parallel
Researchers (one per assignment file). Finally spawn a
Synthesizer to combine findings into
`~/.enact/<enact_id>/RESEARCH.md`. Do not read any
research files at any time.

In high-effort mode, multiple survey-research-synthesize
rounds can be conducted, exploring discovered topics in
greater detail.

## Planning Phase

Spawn a Planner to create `~/.enact/<enact_id>/PLAN.md`.
If the Plan Refiner was selected, spawn it to audit the
plan. Do not read the plan.

## Delivery

After the plan (and optional refinement) is complete:

1. Copy the plan to the output file:
   `cp ~/.enact/<enact_id>/PLAN.md <output_file>`
2. Report completion to the user with the output path
3. Update ORCHESTRATOR_STATE.md to COMPLETE

Do NOT generate tasks, create worktrees, or spawn coders.
The plan is the final deliverable.

## Prompting Subagents

When spawning any subagent, keep your prompt **minimal**.
Provide:

- The enact scratch directory path (absolute)
- A brief project summary (1-3 sentences)
- Any user-specified constraints relevant to this agent

Spawn agents by their `name` field as the
`subagent_type` parameter of the Task tool (e.g.,
`subagent_type: "surveyor"`). Claude Code's native agent
system automatically loads each agent's definition,
including its configured model, tools, and skills.

Do NOT write detailed instructions that duplicate or
override the agent's own definition. Each agent has its
own prompt and loads its own skills. Your job is to give
it the right *inputs*, not to redefine its *behavior*.

## Context Discipline

Subagents return **brief** summaries (3-5 lines). You do
NOT need their full output in your context.

**Do NOT**: read research files, read the plan, accumulate
long subagent outputs, or repeat summaries back to the
user verbatim.

**DO**: track state via ORCHESTRATOR_STATE.md, report brief
progress, pass file paths between subagents (not file
contents).

## Error Recovery

If a subagent fails (returns with errors, runs out of
context, or reports it could not finish):

1. Read the subagent's output to understand what was
   accomplished.
2. If the failure appears transient (timeout, context
   exhaustion), retry once before escalating.
3. If a Planner or Refiner fails, spawn a replacement
   with context about what was accomplished.

## Context Recovery

If you are ever uncertain about the project state,
re-read:

1. `~/.enact/<enact_id>/ORCHESTRATOR_STATE.md`

Do this **proactively** every 5-10 subagent rounds, not
just when confused.
