---
name: writing-subagents
description: Use when creating, configuring, or debugging custom subagents for Claude Code. Covers agent file format, configuration fields, system prompt authoring, and delegation patterns. Triggers on subagent creation, agent definition files, or Task tool configuration.
---

# Writing Custom Subagents

Guide for creating subagents that handle delegated tasks
reliably within their own context window.

## The Golden Rule

**One agent, one job.** Each subagent should excel at a
single, well-defined task. A subagent that tries to do
everything will do nothing well.

## How Subagents Work

Subagents run in their own context window with a custom
system prompt, specific tool access, and independent
permissions. The parent delegates based on the subagent's
description field. Each subagent receives only its own
system prompt — not the parent's — and returns a summary
when finished.

## Core Principles

**Token efficiency.** Subagent context windows are limited.
Every line in the system prompt competes with the task
prompt and tool outputs. Challenge every instruction: does
this change the subagent's behavior? If not, cut it.

**Description-driven delegation.** The parent agent uses
the `description` field to decide when to delegate. A
vague description means the subagent never gets called.
Write descriptions using the same "Use when..." trigger
pattern as skills.

**Minimal tool access.** Grant only the tools the subagent
actually needs. Fewer tools improve security, reduce
confusion, and keep the agent focused on its job.

**Imperative instructions.** Direct commands in the system
prompt change behavior more reliably than suggestions.
"Always run tests before returning" outperforms "Consider
running tests."

**Cost-aware model selection.** Route simpler tasks to
faster, cheaper models like Haiku. Reserve Opus for tasks
that need deep reasoning. Use `model: sonnet` as a
sensible default.

## Subagent File Format

Subagent files are Markdown with YAML frontmatter. Place
them in `.claude/agents/` (project-scoped) or
`~/.claude/agents/` (personal).

```markdown
---
name: code-reviewer
description: >-
  Use when reviewing code changes for quality issues,
  security vulnerabilities, or style violations. Use
  proactively after code modifications.
tools: Read, Glob, Grep
model: sonnet
---

You are a code reviewer. For every file you review:

1. Read the file completely before commenting
2. Check for security vulnerabilities first
3. Check for correctness bugs second
4. Note style issues only if they affect readability

Return a structured summary with findings grouped by
severity: critical, warning, info.
```

The body after the frontmatter is the subagent's system
prompt — its complete instructions for how to behave.

## Configuration Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Identifier for calling the subagent |
| `description` | Yes | Tells parent when to delegate |
| `tools` | No | Allowlist of available tools |
| `disallowedTools` | No | Denylist of blocked tools |
| `model` | No | `sonnet`, `opus`, `haiku`, `inherit` |
| `permissionMode` | No | `default`, `acceptEdits`, `dontAsk`, `bypassPermissions`, `plan`, `delegate` |
| `maxTurns` | No | Cap on agentic turns |
| `skills` | No | Skills injected into subagent context |
| `memory` | No | Persistent scope: `user`, `project`, `local` |
| `hooks` | No | Lifecycle hooks (`PreToolUse`, `PostToolUse`, `Stop`) |
| `mcpServers` | No | MCP servers available to subagent |

## Writing the Description

The description determines whether the parent ever
delegates to your subagent. Apply the same rules as
skill descriptions:

- Start with "Use when..." (triggering conditions only)
- Include concrete symptoms, tool names, and situations
- NEVER summarize what the subagent does internally
- Add "Use proactively" to encourage automatic delegation

| Bad | Problem |
|-----|---------|
| "Reviews code" | Too vague, no triggers |
| "Runs tests and reports results" | Workflow summary |
| "Helper for various tasks" | No specificity |

| Good | Why |
|------|-----|
| "Use when reviewing PRs or checking code quality after modifications" | Trigger conditions |
| "Use when investigating test failures or debugging flaky tests" | Concrete symptoms |
| "Use proactively after code changes to verify test coverage" | Proactive trigger |

**Why this matters:** The parent agent reads descriptions
to decide delegation. If the description summarizes the
subagent's process, the parent may attempt the work itself
using the description as instructions instead of
delegating. Describe WHEN to delegate, never HOW the
subagent works.

## Writing the System Prompt

The system prompt body is the subagent's only instruction
set. It does not see the parent's system prompt, CLAUDE.md,
or conversation history.

**Be self-contained.** Include all context the subagent
needs. Don't assume it knows project conventions, file
locations, or coding standards unless you state them
explicitly.

**Use imperative commands.** "Always read the file before
commenting" not "You should consider reading the file."

**Specify output format.** Tell the subagent exactly what
to return. The parent only sees the subagent's final
summary, so structure it for easy consumption.

**Close loopholes.** When subagents rationalize around
rules, add explicit counters. "Do NOT skip files even if
they appear trivial."

**Keep it concise.** The system prompt shares the context
window with the task and tool outputs. Every unnecessary
line reduces the subagent's working memory.

## Scope and Discovery

Subagents are discovered based on file location. Higher
priority overrides lower when names collide.

### Priority Order (highest to lowest)

1. **CLI flag** (`--agents`, current session only)
2. **Project** (`.claude/agents/`, shared via version
   control)
3. **Home** (`~/.claude/agents/`, personal, all projects)

### Choosing Scope

| Scope | Location | Use When |
|-------|----------|----------|
| Personal | `~/.claude/agents/` | Experimenting, personal workflows |
| Project | `.claude/agents/` | Team-shared, follows repo conventions |

Personal agents are available immediately. Project agents
follow your team's normal code review workflow.

## Rules and Constraints

- Subagents **cannot spawn other subagents** (no nesting)
- Subagents receive only their own system prompt, **not**
  the parent's
- Subagents **do not inherit skills** from the parent;
  list them explicitly via `skills`
- Background subagents auto-deny any permissions not
  pre-approved and **cannot use MCP tools**
- If the parent uses `bypassPermissions`, it takes
  precedence and cannot be overridden
- Subagent file changes require a session restart or
  `/agents` to take effect

## Composition Patterns

**Parallel research.** Spawn multiple subagents for
independent investigations. Each returns a summary; the
parent synthesizes.

**Sequential pipeline.** Chain subagents for multi-step
workflows. Each completes its task and returns results
for the next stage.

**Context isolation.** Use subagents to isolate verbose
output (tests, logs, large code reviews). Only the
summary returns to the parent's context.

## When to Use a Subagent

**Use a subagent when:**

- The task produces verbose output you don't need in
  the main context
- You want to enforce specific tool restrictions
- The work is self-contained and can return a summary
- Multiple independent investigations can run in parallel

**Stay in the main conversation when:**

- The task needs frequent back-and-forth or iteration
- Multiple phases share significant context
- Latency matters (subagents start fresh)

## Quick Reference

| Rule | Detail |
|------|--------|
| Golden Rule | One agent, one job |
| Description | "Use when..." triggers, not workflow |
| Tools | Grant only what's needed |
| System prompt | Self-contained, imperative, concise |
| Output | Specify return format explicitly |
| Model | Haiku simple, Sonnet default, Opus complex |
| Nesting | Subagents cannot spawn subagents |
| Inheritance | No parent prompt or skill inheritance |
| Scope | Project for team, home for personal |
| Proactive | Add "Use proactively" to auto-delegate |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Vague description | "Use when..." with concrete triggers |
| Too many tools | Grant only what the task requires |
| Assuming parent context | System prompt must be self-contained |
| No output format | Define what the summary should contain |
| Opus for simple tasks | Route to Haiku or Sonnet |
| Multi-job agent | Split into focused single-job agents |
| Missing skill inheritance | List skills explicitly via `skills` |
| Passive language | Use imperative commands in prompt |
| Description as workflow | Triggers only, never process steps |
