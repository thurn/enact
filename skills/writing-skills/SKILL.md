---
name: writing-skills
description: Create and refine Claude Code skills. Use when building a new skill, editing an existing skill, writing skill descriptions, organizing skill directories, or testing skills with subagents. Triggers on skill authoring, skill testing, or SKILL.md creation.
---

# Writing Effective Claude Code Skills

Guide for creating skills that change agent behavior reliably
and are discoverable when needed.

## The Golden Rule

**No skill without a failing test first.** Before writing any
skill, run the target scenario without the skill and document
how the agent fails. Only then write the minimal skill that
fixes the observed failures.

## What Is a Skill?

A skill is a reusable technique, pattern, or reference that
extends Claude's capabilities. Skills are concise, actionable
instructions — not narratives or tutorials.

Skills live in a directory with a `SKILL.md` entry point:

```
skill-name/
├── SKILL.md              # Entry point (required)
├── reference-topic.md    # Supporting docs (optional)
└── scripts/              # Executable tools (optional)
```

There are three types:

- **Technique**: Teaches a non-obvious method (e.g., testing
  strategies, debugging approaches)
- **Pattern**: Provides a reusable template or structure (e.g.,
  document hierarchy, PR workflow)
- **Reference**: Supplies domain knowledge (e.g., API specs,
  style guides)

## Core Principles

**Token efficiency.** The context window is a shared public
good. Every line competes with conversation history, other
skills, and system prompts. Challenge every paragraph: does
this change agent behavior? If not, cut it.

- Getting-started skills: <150 words
- Frequently-loaded skills: <200 words total
- Other skills: <500 words in SKILL.md body

**Progressive disclosure.** Organize content in three levels:

1. **Metadata** — name + description in frontmatter (~100
   words, always in context)
2. **SKILL.md body** — Core instructions loaded when triggered
   (<500 lines)
3. **Bundled resources** — Supporting docs and scripts loaded
   only when needed

Keep references one level deep from SKILL.md. Never chain
A → B → C.

**Appropriate degrees of freedom.** Match instruction precision
to task variability:

- **High freedom:** Prose instructions for creative or variable
  tasks
- **Medium freedom:** Parameterized scripts for semi-structured
  tasks
- **Low freedom:** Exact scripts for fragile or deterministic
  operations

**Discoverability.** A skill that can't be found is worthless.
The `description` field determines whether Claude ever loads
your skill. See
[search-optimization.md](search-optimization.md) for
techniques.

**Imperative language.** Direct commands change behavior more
reliably than suggestions. "Always run tests" outperforms
"Consider running tests."

## SKILL.md Structure

Every SKILL.md follows this template:

```yaml
---
name: kebab-case-name
description: >-
  Use when [triggering conditions]. Triggers on
  [keyword list].
---
```

The body follows a consistent structure:

```markdown
# Descriptive Title

[One-line summary of the skill's purpose.]

## The Golden Rule

**[Single most important principle.]**

## Core Principles

[3-6 key principles with brief explanations.]

## [Domain-Specific Sections]

[Practical guidance organized by topic.]

## Quick Reference

| Rule | Detail |
|------|--------|
| ...  | ...    |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| ...     | ... |

## Supporting Documents

- [file.md](file.md): [What's inside]. [When to read].
```

## Writing the Description Field

The description is the single most important line in your
skill. It controls whether Claude finds and loads the skill.

**Rules:**

- Start with "Use when..." (triggering conditions only)
- Include concrete symptoms, tool names, and situations
- NEVER summarize the skill's workflow or process
- Keep under 500 characters
- Use third person

**Why this matters:** When a description summarizes workflow,
Claude shortcuts to the description instead of reading the
full skill body. A description saying "dispatches subagent per
task with code review between tasks" causes Claude to follow
the description literally, ignoring the skill's actual
instructions.

| Bad | Problem |
|-----|---------|
| "Manages deploys by running tests then pushing" | Workflow |
| "A skill for writing documentation" | No triggers |
| "Helps with code review" | Too vague |

| Good | Why |
|------|-----|
| "Use when creating deployment pipelines or debugging failed deploys" | Triggers |
| "Use when creating docs, building indexes, or structuring doc hierarchies" | Situations |
| "Use when reviewing PRs, identifying code smells, or checking test coverage" | Symptoms |

See [search-optimization.md](search-optimization.md) for
advanced techniques.

## Testing Skills

**The Iron Law: No skill ships without testing.**

The RED-GREEN-REFACTOR cycle for skills:

1. **RED** — Run the scenario without your skill. Document
   exact agent failures and rationalizations verbatim.
2. **GREEN** — Write the minimal skill that fixes observed
   failures. Re-run the scenario. Verify compliance.
3. **REFACTOR** — Find new failure modes. Add explicit
   counters for each rationalization. Re-test under pressure.

**Pressure testing:** Combine 3+ pressures simultaneously:

- Time pressure ("do this quickly")
- Conflicting priorities ("also do X")
- Edge cases (ambiguous inputs, missing data)
- Scale (many files, long conversations)

**Bulletproofing:** Don't just state rules — forbid specific
workarounds. Build rationalization tables from observed agent
behavior:

| Rationalization | Counter |
|-----------------|---------|
| "Too simple to test" | ALL skills require testing |
| "I'll test after writing" | Test BEFORE writing |
| "Just a small update" | Updates change behavior; test it |

See [testing-skills.md](testing-skills.md) for the complete
methodology.

## Writing Effective Instructions

**Close loopholes explicitly.** When agents rationalize around
a rule, add an explicit counter. Don't assume intent is
obvious.

**Show, don't just tell.** Include good/bad example pairs for
any non-obvious instruction. Annotate why examples succeed or
fail.

**One concept per section.** Readers scan, not read. Keep
sections focused on a single actionable idea.

**Consistent terminology.** Pick one term per concept and use
it everywhere. Don't alternate between synonyms.

## Directory Organization

```
skills/
├── my-skill/
│   ├── SKILL.md
│   ├── topic-reference.md
│   └── scripts/
│       └── validate.py
```

- Skill directory names: kebab-case, max 64 characters
- Verb-first, active voice: `writing-skills` not
  `skill-writing`
- No auxiliary files (README, CHANGELOG, INSTALLATION_GUIDE)
- Only include files Claude actually needs
- Reference files >100 lines need a table of contents

## Quick Reference

| Rule | Detail |
|------|--------|
| Golden Rule | No skill without a failing test first |
| Description | "Use when..." with trigger conditions only |
| Line limit | SKILL.md under 500 lines |
| Token budget | Challenge every paragraph's value |
| Disclosure | Three levels: metadata → body → resources |
| References | One level deep, never chain A → B → C |
| Naming | Kebab-case, verb-first, max 64 chars |
| Degrees | Match precision to task variability |
| Examples | Good/bad pairs for non-obvious rules |
| Loopholes | Forbid specific workarounds explicitly |
| Language | Imperative commands, not suggestions |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Writing before testing | Run scenario without skill first |
| Description summarizes workflow | "Use when..." triggers only |
| All content in SKILL.md | Split into supporting docs |
| Vague instructions | Imperative commands with examples |
| Rules assumed obvious | Close loopholes explicitly |
| Nested references (A→B→C) | One level deep from SKILL.md |
| No pressure testing | Combine 3+ pressures |
| Auxiliary files | Only include what Claude needs |
| Passive language | Use direct imperative commands |

## Supporting Documents

- [search-optimization.md](search-optimization.md): Techniques
  for writing description fields that maximize skill
  discoverability by Claude's search. Read when crafting or
  improving skill descriptions.

- [testing-skills.md](testing-skills.md): Complete TDD
  methodology for skills including pressure scenarios,
  subagent testing, and bulletproofing techniques. Read when
  testing a new or modified skill.
