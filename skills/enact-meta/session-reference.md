# Session Investigation Reference

How to find, read, and diagnose Enact session
transcripts. Use this when META.md findings are
insufficient and you need to see what an agent
actually did.

## Session Structure

Each Enact session lives at `~/.enact/<enact_id>/`.
The enact ID is a Unix timestamp. Artifacts:

| File | Contents |
|------|----------|
| `ORCHESTRATOR_STATE.md` | Pipeline state log |
| `RESEARCH.md` | Combined research findings |
| `PLAN.md` | Technical project plan |
| `REVIEW_<type>_<task>.md` | Code review findings |
| `INTEGRATION_REVIEW.md` | End-to-end audit |
| `META.md` | Metacognizer synthesis |
| `meta/<N>.md` | Analysis assignments |
| `meta/<N>_result.md` | Mini-metacognizer findings |
| `tasks/` | Task specification files |
| `research/` | Individual research results |

## Finding Transcripts

Use `enact-transcripts.py`:

```bash
# Labeled list of all transcripts
~/.claude/scripts/enact-transcripts.py <enact_id>

# Bare paths only
~/.claude/scripts/enact-transcripts.py --paths <id>

# Full contents (large output)
~/.claude/scripts/enact-transcripts.py --cat <id>
```

Omit `<enact_id>` for the most recent session.

Output includes the orchestrator transcript first,
then direct subagents with labels (e.g., "Planner",
"Feature Coder: task 23"), then team members grouped
by team.

## Summarizing Transcripts

Use `summarize-session.py` to convert raw JSONL into
readable markdown:

```bash
# By subagent ID (e.g., a917fe1)
~/.claude/scripts/summarize-session.py <agent-id>

# By transcript path
~/.claude/scripts/summarize-session.py <path.jsonl>

# By team reference
~/.claude/scripts/summarize-session.py \
  <team-name>/<agent-name>

# Most recent session's orchestrator
~/.claude/scripts/summarize-session.py --latest
```

Summaries include: session metadata, each prompt,
thinking blocks, assistant responses, every tool call
with inputs and results, and a statistics footer.

**Typical investigation workflow:**

1. Run `enact-transcripts.py` to list agents and
   paths.
2. Run `summarize-session.py <agent-id>` on agents
   relevant to the problem.
3. For deeper inspection, read the raw JSONL.

## JSONL Transcript Format

Each line is a JSON object with a `type` field:

- `type: "assistant"` — Agent output.
  `message.content` contains `{"type": "text"}` and
  `{"type": "tool_use"}` items.
- `type: "user"` — Tool results. Check for error
  messages and non-zero exit codes.
- `type: "progress"` — Progress updates (less useful).

The `usage` field in the final tool result contains
`total_tokens`, `tool_uses`, and `duration_ms`.

## Friction Signals

When reading transcripts, look for these patterns:

| Signal | Indicates |
|--------|-----------|
| Tool call errors / non-zero exits | Agent tried something that doesn't work |
| Repeated similar searches | Couldn't find what it needed |
| "Let me try a different approach" | First approach failed |
| Long transcript, few results | Tokens wasted |
| Re-reading files already read | Context lost |
| Apologizing or hedging | Low confidence |
| Build failures after "verified" | Premature success claim |

## Prompt Gaps

Evidence that an agent's prompt needs improvement:

- Agent didn't know something it should have.
- Agent assumed something wrong that research should
  have prevented.
- Agent duplicated work from a previous pipeline step.
- Agent used wrong conventions for the project.
- Agent struggled with a tool its prompt didn't
  explain.

## Efficiency Problems

- Token usage disproportionate to output quality.
- Tool calls that returned nothing useful.
- Files read multiple times unnecessarily.
- Over-verification (same check 3+ times).
- Under-verification (claiming success without
  running checks).

## Root Cause Categories

When diagnosing problems, trace to one of:

1. **Prompt deficiency** — Missing, wrong, or
   misaligned instructions.
2. **Skill gap** — Skill file is missing, incomplete,
   or teaches the wrong pattern.
3. **Pipeline design flaw** — Structural weakness in
   session phases or ordering.
4. **Tool limitation** — Agent's tools cannot do what
   is needed.
5. **Coordination failure** — Orchestrator managed
   handoffs poorly.
6. **Model limitation** — Not fixable with prompts.

For each diagnosis, ask: "Would fixing this prevent
the problem in *all future sessions*, or just this
one?" Prioritize systemic fixes.
