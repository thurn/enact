---
name: enact-meta
description: Use when updating Enact based on insights from the most-recently-completed Enact project. Triggers on post-session improvement, implementing metacognizer recommendations, applying META.md findings, or improving agent prompts after an Enact run.
---

# Improving Enact After a Session

Make targeted changes to Enact agents, skills, or
pipeline design, grounded in evidence from the most
recent session.

## The Golden Rule

**The user's prompt drives direction; session evidence
drives grounding.** The user tells you *what* to
change. Session artifacts tell you *why* it matters
and *how* agents actually behaved. Never apply META.md
recommendations uncritically — the user decides what
to act on.

## Workflow

### 1. Understand the Request

Read the user's prompt carefully. They may ask to:
- Fix a specific problem they observed
- Implement a META.md recommendation they agree with
- Rework an agent's behavior based on session results
- Change pipeline structure or agent selection
- Address a pattern across multiple sessions

### 2. Gather Evidence

Find the most recent session and read artifacts that
are relevant to the user's request:

```bash
ls -t ~/.enact/*/META.md | head -1
```

**Session artifacts** (in `~/.enact/<id>/`):

| File | Use for |
|------|---------|
| `META.md` | Synthesized findings and recommendations |
| `meta/*_result.md` | Detailed per-batch findings |
| `meta/*.md` (not *_result) | Assignment context |
| `ORCHESTRATOR_STATE.md` | Pipeline overview |
| `PLAN.md` | What the session was building |
| `REVIEW_*.md` | Code review findings |
| `INTEGRATION_REVIEW.md` | End-to-end audit |

**Transcripts** — use when you need to see what an
agent actually did. See
[session-reference.md](session-reference.md) for
transcript tools, friction signals, and diagnosis
patterns.

Read only what is relevant. If the user says "fix the
conformance reviewer," you need META.md findings about
conformance and maybe a transcript summary — not the
full research phase.

### 3. Diagnose Before Editing

Before changing any file, state:
- **What went wrong** (specific agent behavior)
- **Why** (root cause from evidence)
- **What change fixes it** (targeted edit)

If the evidence is ambiguous, investigate further
using `summarize-session.py` on relevant agent
transcripts before proposing edits.

### 4. Make Changes

**Agent prompts**: `agents/<agent_name>.md`
**Skills**: `skills/<skill-name>/SKILL.md`
**Pipeline**: `skills/enact-project/SKILL.md` and
`skills/enact-agents/SKILL.md`

For every edit:
1. Read the full target file first.
2. Make the minimal change that addresses the problem.
3. Verify the file stays under 500 lines and is
   line-wrapped at 80 characters.
4. When changing pipeline behavior, check that agent
   prompts referencing that behavior stay consistent.

### 5. Check Prior Sessions

If multiple sessions exist (`ls ~/.enact/*/META.md`),
skim prior META.md files for recurring themes. A
recommendation that appears in multiple sessions is
stronger evidence. A recommendation that was already
addressed but reappears indicates the fix was
insufficient.

## What NOT to Do

- Do NOT apply all META.md recommendations. The user
  decides what to act on.
- Do NOT rewrite prompts from scratch. Targeted edits.
- Do NOT make speculative improvements the user did
  not ask for.
- Do NOT edit files in `~/.enact/`. Session artifacts
  are read-only historical records.

## Quick Reference

| Rule | Detail |
|------|--------|
| Direction | User prompt, not META.md table |
| Evidence | Session artifacts and transcripts |
| Agent prompts | agents/<name>.md |
| Skills | skills/<name>/SKILL.md |
| Pipeline | skills/enact-project/SKILL.md |
| Line limit | 500 lines per file |
| Line wrap | 80 characters |
| Edit style | Minimal, targeted, evidence-grounded |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Applying all META.md blindly | User decides scope |
| Editing without reading file | Always Read first |
| No evidence for change | Cite specific findings |
| Rewriting entire sections | Targeted insertions |
| Ignoring prior sessions | Check recurring themes |

## Supporting Documents

- [session-reference.md](session-reference.md):
  How to find and read session transcripts, friction
  signal taxonomy, and root cause diagnosis patterns.
  Read when investigating agent behavior beyond what
  META.md provides.
