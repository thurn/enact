---
name: enact-agents
description: Describes the available subagents for the Enact LLM framework. Use when the Orchestrator needs to know which agent types exist and where their definitions live.
---

# Available Subagents

Enact is a specification for a set of specialist LLM
subagents which work together to accomplish software
engineering tasks. The Enact Orchestrator coordinates
their overall efforts.

`<enact_dir>` refers to the Enact project root directory
resolved by the Orchestrator at session start (see the
`enact` skill).

When spawning subagents via the Task tool, reference
each agent by its `name` field as the `subagent_type`
parameter (e.g., `subagent_type: "feature-coder"`).
This uses Claude Code's native custom agent system,
so each agent's frontmatter configuration (model,
tools, skills) is respected automatically. Agents
must be discoverable by Claude Code — either placed
in `.claude/agents/` or made available via the
`--agents` flag.

## Default-On Agents

These agents run unless the user explicitly opts out.

### Surveyors

Definition: <enact_dir>/agents/surveyor.md

Surveyors are the "breadth first" analysis pass in Enact
planning. They survey the landscape of the problem domain
and search for "what questions do we need to ask?". They
create research assignments based on this high level
survey, identifying areas that require further analysis.

### Researchers

Definition: <enact_dir>/agents/researcher.md

Researchers help research a specific topic, based on a
research assignment created by a Surveyor. They gather
information by analyzing the codebase. Starting multiple
researchers in parallel is always encouraged for best
results.

### Synthesizers

Definition: <enact_dir>/agents/synthesizer.md

Synthesizers combine the results of individual research
assignments into a cohesive document, RESEARCH.md. This
is the basic repository of knowledge about the existing
state of the world used by Enact planning. In high-effort
modes of operation, several survey-research-synthesize
rounds can be conducted, exploring discovered topics in
greater detail.

### Planners

Definition: <enact_dir>/agents/planner.md

Planners help write technical project plans based on
research findings. They create a
`~/.enact/<enact_id>/PLAN.md` file with a standalone
technical design doc describing the project. They
investigate the codebase directly to fill knowledge gaps
before writing the plan.

### Plan Refiners

Definition: <enact_dir>/agents/plan_refiner.md

Plan refiners audit technical project plans from a fresh
perspective, identifying whether the plan stands alone as
a complete description of the task to achieve. In some
modes of operation multiple iterations of planning and
refining can be performed.

### Task Generators

Definition: <enact_dir>/agents/task_generator.md

Task generators turn a project plan into a list of Claude
Code tasks, each of which can be completed by a subagent.
They manage creating dependencies between tasks and
ensuring sufficient context is present. Tasks are written
to describe what to do, but also allow for independent
judgement to make implementation choices as work
continues. They can investigate the codebase directly to
fill knowledge gaps.

### Task Refiners

Definition: <enact_dir>/agents/task_refiner.md

Task refiners validate tasks for completeness and
correctness, ensuring they make sense independently
without additional context.

### Feature Coders

Definition: <enact_dir>/agents/feature_coder.md

Feature coders implement a new feature from a task,
writing code and tests to specification, but also
exercising independent judgement to make decisions based
on real world understanding of the project.

### Code Conformance Reviewers

Definition: <enact_dir>/agents/code_conformance_reviewer.md

Code conformance reviewers ensure code conforms to the
task specification and project plan. They check to make
sure the task was fully implemented in line with project
requirements. All code review agents return either the
single word `PASS` or
`REVISE: REVIEW_<reviewer_type>_<task_id>.md` and write
a code review document with a description of needed
revisions in the "REVISE" case.

### Code Quality Reviewers

Definition: <enact_dir>/agents/code_quality_reviewer.md

Code quality reviewers ensure code is well written. They
flag opportunities to reduce code duplication, suggest
refactorings, and audit tests for quality. They operate
on a "less code is better" principle, which also applies
to tests.

### Review Feedback Coders

Definition: <enact_dir>/agents/review_feedback_coder.md

Review feedback coders implement feedback from code review
subagents, resolving their issues and then re-validating
task acceptance criteria.

### Integration Reviewers

Definition: <enact_dir>/agents/integration_reviewer.md

The integration reviewer is the final stop-the-line audit
before a project is considered complete. It validates the
entire pipeline end-to-end: that the plan matched the
original prompt, tasks fully covered the plan,
implementation is correct, testing is sufficient, and the
pieces integrate into a working whole.

### Technical Writers

Definition: <enact_dir>/agents/technical_writer.md

The technical writer creates and maintains project
documentation after all tasks complete. It updates
existing documents and identifies documentation gaps
that could prevent problems in future projects.

### Enact Metacognizers

Definition: <enact_dir>/agents/enact_metacognizer.md

The enact metacognizer is a special subagent which is
responsible for improving Enact itself. It should always
be included in every Enact session. It runs Mini
Metacognizers to read transcripts, and synthesizes their
findings into a post-session review at
`~/.enact/<enact_id>/META.md` with concrete
recommendations for improving Enact's own agent prompts
and skills.

## Default-Off Agents

These agents run only when the user requests them or the
Orchestrator recommends them via AskUserQuestion.

### Interviewers

Definition: <enact_dir>/agents/interviewer.md

Interviewer subagents help the user brainstorm solutions
to a problem, asking questions to clarify the project
objectives and gather information. They investigate the
codebase directly to fill knowledge gaps discovered during
the interview.

### QA Scenario Generators

Definition: <enact_dir>/agents/qa_scenario_generator.md

QA scenario generators run once after task generation.
They read the project plan and all implementation task
descriptions, then generate manual QA scenarios for
implementation tasks that have CLI-exercisable
functionality.

### Manual QA Testers

Definition: <enact_dir>/agents/manual_qa_tester.md

Manual QA testers execute QA scenarios for a specific
implementation task. They walk through a given scenario
using the command line interface, and optionally exercise
autonomy to search for additional production problems and
file tasks.

### Bugfix Coders

Definition: <enact_dir>/agents/bugfix_coder.md

Bugfix coders fix bugs from bug report tasks using a
reproduce-first methodology: manually reproducing the bug,
capturing it in an automated test, fixing the root cause,
verifying the fix both manually and via tests, and then
thinking one level deeper about systemic prevention —
either implementing hardening directly or filing a task to
address architectural issues that allowed the bug to
exist.

### Subject Matter Expert Reviewers

Definition: <enact_dir>/agents/sme_reviewer.md

Subject matter expert reviewers look at changes from the
perspective of a single subject, on which they have deep
expertise, and suggest changes based on their knowledge.

## Ad-Hoc Agents

These agents are not selected during Agent Selection.
The Orchestrator spawns them on demand.

### Merge Conflict Resolvers

Definition: <enact_dir>/agents/merge_conflict_resolver.md

Merge conflict resolvers handle git merge or rebase
conflicts that arise when integrating task worktrees
back into `<main_branch>`. They understand both sides
of a conflict and resolve them to preserve the intent
of both changes, then verify the result passes all
tests.
