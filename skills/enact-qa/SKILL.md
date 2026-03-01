---
name: enact-qa
description: Instructions for running an empirical QA session with Enact. Covers session setup, QA scenario generation, manual testing, bugfixing, and the full QA lifecycle.
---

This skill describes the Enact workflow for **empirical QA
sessions** — running tools, observing behavior, and fixing
bugs found through execution. The `enact` skill is the
generic orchestration layer; this skill defines the QA
lifecycle.

## Defining Characteristic

Every finding in a QA session must come from **running real
commands and observing real output**. Static code analysis
(reading code, comparing to docs) is research, not QA. A QA
session that never executes a CLI command has failed.

If the user's prompt mentions both building and QA, use
`enact-project` with QA agents enabled (default-off agents
toggled on). Use `enact-qa` only when testing and validation
is the **primary** activity.

## Session Setup

1. Create an Enact ID via `date +%s`
2. Resolve the scratch directory path:
   `~/.enact/<enact_id>/`
3. Create the scratch directory:
   `mkdir -p ~/.enact/<enact_id>/tasks`
4. Detect the main branch name:
   `git symbolic-ref refs/remotes/origin/HEAD | sed
   's@^refs/remotes/origin/@@'`
   If this fails, ask the user. Store as `main_branch`.
5. Initialize `<scratch>/ORCHESTRATOR_STATE.md` with:
   - `session_type: qa`
   - `worktree_mode: no-worktrees`
   - `concurrency: 1`
   - Current state: `RESEARCH`
6. Report the scratch directory to the user immediately.

QA sessions always use no-worktrees mode (serial execution
on the main repo). Skip worktree cleanup — no worktrees
are used.

## Agent Selection

QA sessions use a **fixed agent set** — no user selection
phase. The following agents are pipeline agents:

**Research Phase:**
- Surveyor
- Researchers (parallel)

**QA Phase:**
- QA Scenario Generator
- Manual QA Testers (serial)

**Bugfix Phase:**
- Bugfix Coders (serial)
- Code Conformance Review script
- Code Quality Review script
- Review Feedback Coder (if REVISE)

**Post-QA Phase:**
- Meta-Surveyor
- Mini-Metacognizers (parallel)
- Enact Metacognizer

No Synthesizer, Planner, Task Generator, Feature Coder,
Integration Reviewer, or Technical Writer.

## State Machine

| State          | Description                        |
|----------------|------------------------------------|
| RESEARCH       | Surveyor + Researchers find CLI    |
|                | entry points and expected behavior |
| QA_GENERATION  | QA Scenario Generator creates      |
|                | standalone scenario files          |
| QA_EXECUTION   | Manual QA Testers run scenarios    |
| BUGFIX         | Bugfix Coders fix discovered bugs  |
| RE_QA          | Re-run failed scenarios only       |
| POST_QA        | Metacognition pipeline             |
| COMPLETE       | All work done                      |

Multiple BUGFIX → RE_QA rounds may occur. The loop
terminates when all scenarios pass or 3 rounds are
exhausted. If bugs persist after 3 rounds, present
remaining issues to the user via AskUserQuestion.

After each subagent completes:

1. Read the subagent's return message (brief)
2. Update ORCHESTRATOR_STATE.md
3. Determine the next subagent based on the state machine
4. Spawn it with `run_in_background: true`
5. **End your turn and wait.** See Critical Rules.

## ORCHESTRATOR_STATE.md

After **every** subagent completes, overwrite
`<scratch>/ORCHESTRATOR_STATE.md` with current state.

Include:

- Current state and pipeline position
- Session type (`qa`) and worktree mode (`no-worktrees`)
- What just completed and what comes next
- A checklist of all completed pipeline steps
- QA status table (initialized after QA_GENERATION):

```markdown
## QA Status

| Scenario | File | Result | Bugs Filed |
|----------|------|--------|------------|
| scenario 1 | qa_1.md | PASS | — |
| scenario 2 | qa_2.md | FAIL | task_1 |

## Bugfix Rounds
- Round 1: [bugs fixed], [re-QA results]
- Round 2: ...

Bugfix round counter: 1/3
```

This is your **primary recovery mechanism**. If your
context compacts and you lose track, re-read this file to
reconstruct your state.

## Research Phase

The research phase in QA mode has a **different goal** than
in build mode. Researchers are NOT auditing code against a
spec. They are answering:

1. **How do I run this tool?** Find CLI entry points,
   scripts, commands, REPLs, or test harnesses that
   exercise the code. If none exist, identify what a
   minimal CLI wrapper would look like.
2. **What does correct behavior look like?** Read design
   docs, specs, or existing tests to understand expected
   outputs for various inputs.
3. **What are the riskiest areas?** Identify edge cases,
   complex logic, recently changed code, or areas with
   poor test coverage most likely to have bugs.

Spawn a Surveyor with these instructions added to the
prompt:

> This is a **QA session**. The goal is empirical
> testing — running the tool and observing behavior.
> Research assignments must focus on: (1) finding CLI
> entry points and how to exercise the tool from the
> command line, (2) understanding expected behavior from
> docs/specs/tests, and (3) identifying risky areas to
> test. Do NOT create assignments that compare code to a
> spec — that is code review, not QA. Every assignment
> should help the QA Scenario Generator design scenarios
> that **run real commands and observe real output**.

After Researchers complete, proceed directly to
QA_GENERATION. There is no Synthesizer, Planner, or Task
Generator in QA mode.

## QA Generation Phase

Spawn a QA Scenario Generator with these modified
instructions:

> This is a standalone QA session, not a per-task QA
> pass. There are no implementation task files. Instead,
> create standalone QA scenario files at
> `<scratch>/tasks/qa_<N>.md`. Each file should be a
> self-contained QA scenario following the standard
> format (objective, prerequisites, steps, success
> criteria, failure actions). Use the research results
> at `<scratch>/research/*_result.md` to understand CLI
> entry points and expected behavior. Create 3-10
> scenarios covering happy paths, error handling,
> boundary conditions, and the riskiest areas identified
> during research.

After completion, list `<scratch>/tasks/qa_*.md` to
discover scenario files. Initialize the QA status table
in ORCHESTRATOR_STATE.md with each scenario set to
`pending`.

## QA Execution Phase

For each scenario file (`<scratch>/tasks/qa_<N>.md`),
spawn a Manual QA Tester:

- Pass the scenario file path as the task file
- Set `worktree_dir` to the project directory
- Set `project_dir` to the project directory

Run testers **serially** (one at a time) to avoid
interfering with each other's state. After each tester
completes, update ORCHESTRATOR_STATE.md with the result
(PASS or FAIL).

If a tester files bug tasks
(`<scratch>/tasks/task_<N>.md`), collect them for the
BUGFIX phase.

After all scenarios are executed:

    QA complete: X/Y passed. [N bugs filed.]

If no bugs were found, transition to POST_QA. Otherwise
transition to BUGFIX.

## Bugfix Phase

Spawn a Bugfix Coder for each bug task. Since QA mode
uses no-worktrees, coders work directly on the main
repo. Run bugfix coders **serially**.

After each bugfix:

1. Run code review scripts in parallel via Bash:
   - `~/.claude/scripts/review-quality.sh <scratch>
     <task_file> <project_dir> <main_branch>`
   - `~/.claude/scripts/review-conformance.sh <scratch>
     <task_file> <project_dir> <main_branch>`
2. If either returns REVISE, spawn a Review Feedback
   Coder.
3. Update ORCHESTRATOR_STATE.md.

After all bugfixes complete:

    Bugfix round N complete. Re-running failed QA.

Transition to RE_QA.

## Re-QA Phase

Re-run only the QA scenarios that previously **failed**.
Spawn Manual QA Testers with the same parameters as the
original execution.

If all re-run scenarios pass, transition to POST_QA.

If new bugs are found, loop back to BUGFIX. The loop
limit is **3 rounds**. After 3 rounds, present remaining
issues to the user via AskUserQuestion:

> QA found persistent bugs after 3 bugfix rounds. The
> following scenarios still fail: [list]. Would you like
> to continue fixing, skip remaining issues, or stop?

## Post-QA Phase

After QA is clean (or the loop limit is reached), run
the metacognition pipeline only:

1. Spawn a **Meta-Surveyor**, wait for completion.
2. List `<scratch>/meta/*.md` (exclude `*_result.md`).
   Spawn one **Mini-Metacognizer** per assignment file
   in parallel. Wait for all to complete.
3. Spawn the **Enact Metacognizer**, wait for completion.

Skip Integration Reviewer and Technical Writer — no
feature was built.

## Prompting Subagents

When spawning any subagent, keep your prompt **minimal**.
Provide:

- The enact scratch directory path (absolute)
- A brief project summary (1-3 sentences)
- Any user-specified constraints relevant to this agent
- Specific information this agent needs that is not in
  the scratch directory files
- For QA agents: the scenario file path
  (`<scratch>/tasks/qa_<N>.md`)
- For bugfix agents: the bug task file path
  (`<scratch>/tasks/task_<N>.md`), `project_dir`, and
  `main_branch`

Spawn agents by their `name` field as the
`subagent_type` parameter of the Task tool (e.g.,
`subagent_type: "manual-qa-tester"`). Claude Code's
native agent system automatically loads each agent's
definition, including its configured model, tools, and
skills.

Do NOT write detailed instructions that duplicate or
override the agent's own definition. Each agent has its
own prompt and loads its own skills. Your job is to give
it the right *inputs*, not to redefine its *behavior*.

## Context Discipline

Subagents return **brief** summaries (3-5 lines). You do
NOT need their full output in your context.

**Do NOT**: read scenario descriptions (pass file paths
to subagents instead), read source code files, read
detailed bug reports, accumulate long subagent outputs,
or repeat summaries back to the user verbatim.

**DO**: track state via ORCHESTRATOR_STATE.md, report
brief progress, pass file paths between subagents (not
file contents), trust that subagents wrote their results
to the correct files.

## Error Recovery

If a subagent fails (returns with errors, runs out of
context, or reports it could not finish):

1. Read the subagent's output to understand what was
   accomplished.
2. For **Manual QA Testers**: record the scenario as
   inconclusive in ORCHESTRATOR_STATE.md. Re-run once.
   If it fails again, mark as FAIL and note the error.
3. For **Bugfix Coders**: spawn a new Bugfix Coder with
   context about what the previous coder accomplished.
   If no progress was made, present the bug to the user.
4. For **any subagent**: if the failure appears
   transient, retry once before escalating.

## Context Recovery

If you are uncertain about the project state (e.g. after
context compaction):

1. Re-read `<scratch>/ORCHESTRATOR_STATE.md` — your
   persistent state log. Note `session_type: qa` and
   the current state.
2. Check the QA status table — which scenarios passed,
   failed, or are still pending.
3. Check the bugfix round counter — how many rounds
   have been completed.
4. List `<scratch>/tasks/` to see scenario and bug
   files.

Do NOT spawn new agents until you have confirmed the
current state.

## Progress Reporting

After each QA scenario completes:

    QA N/M: [scenario title] — PASS/FAIL

After all scenarios complete:

    QA complete: X/Y passed. [N bugs filed.]

After bugfix rounds:

    Bugfix round N complete. Re-running failed QA.

## Critical Rules

### Waiting for Agents (MOST IMPORTANT)

After launching background agents, you MUST **stop
generating immediately**. Do not send any tool calls, do
not write any messages, do not attempt to check on the
agents. End your turn. The system will deliver a
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
- NEVER use the `resume` parameter — always spawn fresh
  agents
- NEVER read large files yourself — let agents read
  what they need
- Launch multiple agents in parallel when possible
  (single message, multiple Task calls)
