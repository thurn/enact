---
name: qa
description: Use when a prompt requires empirical verification through CLI commands. Triggers on /qa, manual testing, real-world verification, end-to-end validation, or when implementation must be proven correct by running it.
---

# QA Mode: Empirical Verification Required

Marker skill that signals: "this prompt's success can and
must be verified by running real commands." Completing a
`/qa` prompt without manual testing is a **failure case**.

## The Golden Rule

**No task is done until you have run real commands against
the real system and observed correct behavior with your
own eyes.** Code that compiles, type-checks, and passes
unit tests can still be wrong. QA mode demands proof.

## When QA Mode Applies

The user invokes `/qa` when they believe the prompt's
result can be empirically verified through CLI commands.
This means:

- The implementation produces observable output (stdout,
  stderr, files, exit codes, network responses).
- Correctness can be judged by running commands and
  inspecting results.
- "It looks right in the code" is insufficient evidence.

## The QA Workflow

### 1. Implement First

Write the code, make the changes, do the normal work.
QA comes after implementation, not instead of it.

### 2. Design Test Scenarios

Before running anything, think critically about what to
test. Design scenarios across these categories:

- **Happy path** -- Does the primary use case work with
  valid, typical inputs?
- **Error handling** -- Does the system produce clear,
  helpful errors for invalid inputs?
- **Boundary conditions** -- Empty input, maximum-size
  input, zero values, special characters, Unicode.
- **Integration** -- Do multiple components work together?
- **Regression** -- Is existing behavior preserved?

Aim for 3-7 scenarios. Prioritize by risk: test the most
important and most fragile behaviors first.

### 3. Find or Create CLI Entry Points

Every scenario needs a way to exercise the code from the
command line. Look for:

- The project's own CLI tool or commands
- Test runners invoked in targeted ways
- Scripts or debug utilities in the codebase
- REPL or interactive modes

If no CLI entry point exists, **create a minimal one** --
a small script that invokes the relevant code and prints
results. This is part of the QA work, not optional.

### 4. Execute and Observe

For every scenario, run real commands. For each command:

1. **Run it.** Use real inputs, not hypothetical ones.
2. **Read the full output.** Not a summary -- the actual
   output.
3. **Think critically.** Ask yourself:
   - Does this output make sense?
   - Is this what a domain expert would expect?
   - Are there anomalies, warnings, or unexpected
     patterns?
   - Is the output internally consistent?
   - If it contains data, is the data reasonable?

**Do NOT just check exit codes.** A command that exits 0
can still produce wrong output. A command that produces
the expected string can still be subtly incorrect.

### 5. Go Above and Beyond

The scripted scenarios are your **starting point**, not
your ceiling. After completing them, do exploratory
testing:

- **Try variations.** What happens with empty input? Very
  long input? Special characters? Running it twice?
- **Test boundaries.** If it accepts a number, try 0, -1,
  the maximum, and a non-number.
- **Try to break it.** Think adversarially. What would a
  confused user do? Wrong argument order? Missing flags?
- **Test interactions.** Does the feature still work after
  other operations modify state?
- **Evaluate holistically.** If you were a real user,
  would this workflow make sense?

Focus exploratory effort where the highest risk lies.

### 6. Report Results

After testing, include a brief QA summary in your
response:

```
## QA Results

### Scenarios Tested
1. [scenario] -- PASS/FAIL: [what happened]
2. [scenario] -- PASS/FAIL: [what happened]

### Exploratory Testing
- [what you tried beyond the script]

### Issues Found
- [any bugs, concerns, or observations]
```

If you find bugs, **fix them and re-test**. The QA cycle
is: implement -> test -> fix -> re-test until clean.

## What "Correct" Means

Correct is not "exits 0." Correct means:

- Output is **factually right** given the input.
- Error messages are **helpful and accurate**.
- No data is **silently dropped or truncated**.
- Behavior matches **what the user asked for**.
- Edge cases produce **sensible results**, not crashes
  or nonsense.

Think like a skeptical expert evaluating the system, not
a test executor checking boxes.

## Failure Modes to Avoid

| Failure | Why it's wrong |
|---------|---------------|
| "Code looks correct, shipping it" | QA demands proof |
| "Tests pass, that's enough" | Unit tests != QA |
| "Command exited 0, PASS" | Wrong output can exit 0 |
| "Ran happy path only" | Boundaries and errors matter |
| Skipping exploratory testing | Scripted is the floor |
| Not reading full output | Skimming misses bugs |
| Testing in your head | Run real commands |

## Quick Reference

| Rule | Detail |
|------|--------|
| Golden Rule | Run real commands, observe results |
| Scenarios | 3-7, across happy/error/boundary |
| CLI entry | Find or create one, no excuses |
| Observation | Read full output, think critically |
| Exploratory | Go beyond scripted scenarios |
| Correctness | Domain-correct, not just exit 0 |
| Cycle | Implement -> test -> fix -> re-test |
| Failure case | Completing without manual testing |
