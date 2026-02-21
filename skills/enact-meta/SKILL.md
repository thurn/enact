---
name: enact-meta
description: >-
  Use when updating Enact based on insights from the
  most-recently-completed Enact project. Triggers on
  post-session improvement, implementing metacognizer
  recommendations, applying META.md findings, or
  improving agent prompts after an Enact run.
---

# Implement Metacognizer Recommendations

Apply META.md findings from the most recent Enact
session to improve agent prompts, skills, and pipeline
design.

## The Golden Rule

**Every edit must trace to specific evidence.** Do not
make speculative improvements. Each change must
reference a META.md recommendation number, a
mini-metacognizer finding, or a recurring theme across
sessions.

## Discovering the Session

Find the most recent Enact session:

```bash
ls -t ~/.enact/*/META.md | head -1
```

If no META.md exists, abort — there is nothing to
implement.

Read these files in order:

1. `~/.enact/<id>/META.md` — the synthesized report
2. `~/.enact/<id>/meta/*_result.md` — mini-metacognizer
   findings (skim for detail behind recommendations)
3. `~/.enact/<id>/ORCHESTRATOR_STATE.md` — pipeline
   overview and session config

## Checking Prior Implementation

Multiple sessions may have META.md files. Read all of
them:

```bash
ls ~/.enact/*/META.md
```

Check each META.md's "Recurring Themes" section for
recommendations that keep reappearing. Prioritize
these — they indicate systemic problems that previous
sessions identified but nobody fixed.

## Triage Recommendations

From META.md's Recommendations table, classify each:

- **Implement now**: Specific prompt/skill edits with
  a clear target file and change description.
- **Needs investigation**: Recommendation is vague or
  requires reading transcripts to understand the
  problem. Use `summarize-session.py` to investigate
  before editing.
- **Skip**: Recommendation is model-limitation,
  already implemented, or not reproducible.

Process recommendations in severity order: Critical
first, then Significant, then Minor.

## Making Changes

### Agent Prompt Edits

Agent definitions live at:
`/path/to/enact/agents/<agent_name>.md`

When editing an agent prompt:

1. Read the full agent file first.
2. Make the minimal edit that addresses the
   recommendation. Do not refactor surrounding text.
3. Verify the file stays under 500 lines and is
   line-wrapped at 80 characters.

### Skill Edits

Skills live at:
`/path/to/enact/skills/<skill-name>/SKILL.md`

Same rules: read first, minimal edit, respect limits.

### Pipeline Design Changes

Pipeline logic lives in:
- `skills/enact-project/SKILL.md` — orchestrator
  state machine and per-task pipeline
- `skills/enact-agents/SKILL.md` — agent descriptions
  and selection defaults

For pipeline changes, also update any agent prompts
that reference the changed pipeline behavior.

## Verification Checklist

After all edits:

1. Every edited file is under 500 lines.
2. Every edited file is line-wrapped at 80 characters.
3. No edit introduced contradictions with other agent
   prompts or skills.
4. Each edit is traceable to a specific META.md
   recommendation or mini-metacognizer finding.

## What NOT to Do

- Do NOT rewrite agent prompts from scratch. Make
  targeted insertions or modifications.
- Do NOT implement recommendations marked as
  "fundamental model limitation" — these cannot be
  fixed with prompt changes.
- Do NOT add speculative improvements beyond what
  META.md recommends. Enact sessions are the testing
  ground for changes; untested changes are noise.
- Do NOT edit META.md or any file in `~/.enact/`.
  Session artifacts are read-only historical records.

## Quick Reference

| Rule | Detail |
|------|--------|
| Source of truth | META.md recommendations table |
| Detail source | meta/*_result.md findings |
| Agent prompts | agents/<name>.md |
| Skills | skills/<name>/SKILL.md |
| Pipeline | skills/enact-project/SKILL.md |
| Line limit | 500 lines per file |
| Line wrap | 80 characters |
| Edit style | Minimal, targeted, traceable |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Editing without reading full file | Always Read first |
| Speculative improvements | Trace every edit to evidence |
| Ignoring recurring themes | Prioritize repeat findings |
| Rewriting entire sections | Minimal targeted edits |
| Skipping verification | Check line count and wrapping |
| Editing session artifacts | ~/.enact/ is read-only |
