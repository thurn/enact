# Testing Skills with TDD

Complete methodology for testing Claude Code skills before
deployment. Skills are tested documentation — write them the
same way you write code.

## The Iron Law

**NO SKILL WITHOUT A FAILING TEST FIRST.**

This is not a guideline. This is an absolute requirement.
Every skill must be tested before it ships. No exceptions
for:

- "Simple additions"
- "Just adding a section"
- "Documentation-only updates"
- "Small fixes"

ALL changes to skills require testing because all changes
alter agent behavior.

## The RED-GREEN-REFACTOR Cycle

### RED: Establish the Baseline

Run the target scenario WITHOUT your skill loaded. Document:

1. **What the agent does wrong**: Exact behaviors, not
   generalizations
2. **What the agent says**: Capture rationalizations verbatim
3. **Where it deviates**: Specific steps it skips or invents

This baseline proves the skill is needed. Without it, you're
guessing about what to write.

**How to test without a skill:** Use a subagent (via the Task
tool) with no access to your skill directory. Give it the
same prompt a real user would give.

### GREEN: Write the Minimal Fix

Write ONLY the skill content that addresses observed failures:

- Each instruction must counter a specific failure from RED
- If you can't point to a RED failure, the instruction is
  speculative — cut it
- Re-run the same scenario WITH the skill loaded
- Verify the agent now behaves correctly

**Do NOT:**

- Write comprehensive guidance "while you're at it"
- Add sections for hypothetical future failures
- Include best practices you haven't tested against

### REFACTOR: Close Loopholes

After GREEN passes, probe for new failure modes:

1. **Add pressure**: Time pressure, conflicting instructions,
   edge cases, scale
2. **Observe new rationalizations**: How does the agent
   justify deviating under pressure?
3. **Add explicit counters**: For each rationalization, add a
   rule that specifically forbids the workaround
4. **Re-test**: Verify the agent complies under pressure

## Pressure Scenarios

Effective pressure testing combines 3+ types simultaneously.

### Pressure Types

| Type | Example |
|------|---------|
| Time | "Do this quickly" |
| Conflicting | "Also handle X while doing Y" |
| Scale | Many files, long conversations |
| Ambiguity | Unclear requirements, missing context |
| Edge cases | Empty inputs, unusual formats |
| Resource | Limited tokens, large codebase |

### Building a Pressure Scenario

1. Start with the core task your skill addresses
2. Add time pressure ("quickly", "efficiently")
3. Add a secondary task that competes for attention
4. Add an edge case that tempts shortcuts
5. Run the scenario and observe failures

Example pressure prompt:

```
"Quickly create a new skill for database migrations.
Also update the existing deployment skill while you're
at it. The migration skill needs to handle both SQL and
NoSQL databases — focus on getting it done fast."
```

This combines: time pressure + competing tasks + scope
ambiguity.

## Rationalization Tables

The most powerful testing artifact is a rationalization
table. After observing agent failures, record exact
rationalizations and write explicit counters.

### Building the Table

1. Run pressure scenarios from REFACTOR phase
2. When the agent deviates, note its EXACT justification
3. Write a rule that specifically names and forbids the
   rationalization
4. Add the rule to your skill
5. Re-test to verify

### Example Table

| Observed Rationalization | Counter Rule |
|--------------------------|--------------|
| "This is a simple change, no test needed" | ALL changes require testing, regardless of perceived simplicity |
| "I'll write the test after the skill" | Tests MUST come first. Write baseline before any skill content |
| "The skill is already tested enough" | Test under 3+ simultaneous pressures before shipping |
| "I can adapt the existing skill" | Do not modify skills during testing; test first, then modify |

## Subagent Testing

Use Claude's Task tool to spawn subagents for testing.

### Without Skill (RED phase)

Spawn a subagent with a prompt matching your target scenario.
Do not give it access to the skill directory. Record its
behavior.

### With Skill (GREEN phase)

Spawn a subagent with the same prompt, but this time include
the skill in its accessible files. Compare behavior against
the RED baseline.

### Under Pressure (REFACTOR phase)

Spawn a subagent with the pressure scenario prompt and the
skill loaded. Observe where it deviates and build your
rationalization table.

## Testing Checklist

Before shipping a skill, verify:

- [ ] RED baseline exists documenting failures without skill
- [ ] GREEN test passes with skill loaded
- [ ] Pressure tested with 3+ simultaneous pressures
- [ ] Rationalization table built from observed failures
- [ ] All counters tested and verified effective
- [ ] Description field passes the shortcut test (see
  [search-optimization.md](search-optimization.md))
- [ ] SKILL.md stays under 500 lines
- [ ] Supporting docs stay under 500 lines each

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Writing skill before RED baseline | Always test without skill first |
| Testing with simple scenarios only | Combine 3+ pressures |
| Generic rationalization counters | Use EXACT observed language |
| Testing once and shipping | Re-test after every REFACTOR change |
| Skipping subagent testing | Use Task tool for isolated testing |
| Testing your own skill yourself | Use subagents for objectivity |
