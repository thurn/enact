# Claude Search Optimization

How to write skill descriptions that ensure Claude finds and
loads your skill when it's relevant.

## The Discovery Problem

Claude discovers skills through their `description` field in
YAML frontmatter. This ~100-word field determines whether your
skill is ever used. A great skill with a poor description is
invisible.

## Description Anatomy

The description field serves one purpose: **tell Claude when
to trigger the skill.** It does NOT summarize what the skill
does.

```yaml
---
name: writing-skills
description: >-
  Use when creating new skills, editing existing skills,
  writing skill descriptions, or testing skills with
  subagents. Triggers on skill authoring, skill testing,
  or SKILL.md creation.
---
```

### Structure

1. **Opening trigger**: Start with "Use when..." followed by
   concrete situations
2. **Keyword coverage**: Include terms users actually type
3. **Trigger sentence**: End with "Triggers on [keyword list]"

### Rules

- Start with "Use when..."
- List triggering conditions, not workflow steps
- Include concrete symptoms and tool names
- Keep under 500 characters
- Use third person throughout
- Technology-agnostic unless the skill is tech-specific

## The Workflow Trap

**Never summarize your skill's process in the description.**

When Claude reads a workflow summary in a description, it
treats the description as a sufficient instruction set and
shortcuts to following the description literally instead of
reading the full SKILL.md body.

**Observed failure:** A skill description said "dispatches
subagent per task with code review between tasks." The skill
body specified two code reviews (before and after). Claude
performed only one review because the description said
"code review" (singular).

The fix: describe WHEN to use the skill, never HOW it works.

## Keyword Strategy

Think about what a user or agent would type when they need
your skill:

1. **Action verbs**: "creating", "debugging", "reviewing",
   "testing", "deploying"
2. **Artifact names**: "SKILL.md", "CLAUDE.md", "PR", "index"
3. **Symptom words**: "failing", "broken", "slow", "missing"
4. **Domain terms**: Specific to the skill's area of expertise

Include 2-3 keyword clusters to cover different phrasings of
the same need. Don't stuff keywords unnaturally.

## Testing Descriptions

After writing a description, verify it works:

1. **Search test**: Would Claude find this skill if a user
   asked about the topic using different phrasings?
2. **Trigger test**: Does the description match situations
   where the skill is actually needed?
3. **Exclusion test**: Does it avoid triggering for unrelated
   tasks?
4. **Shortcut test**: If Claude only read the description and
   skipped the body, would it behave incorrectly?

If the shortcut test passes (description alone is sufficient),
the description contains too much workflow detail. Remove it.

## Examples

### Good Descriptions

```yaml
# Specific triggers, no workflow
description: >-
  Use when creating new skills, editing existing skills,
  or testing skills with subagents. Triggers on skill
  authoring, SKILL.md creation, or skill testing.

# Symptom-based triggers
description: >-
  Use when tests fail intermittently, test suites run
  slowly, or test coverage reports show gaps. Triggers
  on flaky tests, test performance, or coverage issues.

# Tool and artifact triggers
description: >-
  Use when creating docs, building document indexes,
  structuring doc hierarchies, or applying progressive
  disclosure patterns. Triggers on documentation
  authoring, docs organization, or index management.
```

### Bad Descriptions

```yaml
# Workflow summary — causes shortcutting
description: >-
  Creates skills by first running a baseline test, then
  writing the skill, then pressure testing it.

# Too vague — triggers too broadly or not at all
description: >-
  A skill for helping with development tasks.

# No trigger conditions — unclear when to use
description: >-
  Comprehensive guide to writing and testing skills for
  Claude Code with best practices and examples.
```

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Workflow in description | Triggers only — "Use when..." |
| Vague terms ("helps with") | Concrete situations and tools |
| Too long (>500 chars) | Trim to essential triggers |
| Missing keyword coverage | Add 2-3 phrasings per concept |
| First person ("I will...") | Third person throughout |
