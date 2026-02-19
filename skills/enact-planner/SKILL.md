---
name: enact-planner
description: >-
  Guidelines for writing technical project plans in Enact
  sessions. Use when creating PLAN.md files, writing
  technical design docs for implementation, or evaluating
  plan quality.
user_invocable: false
---

# Enact Project Planning

Guidelines for writing technical design documents
(PLAN.md) that give coders everything they need to
implement a project without prior domain knowledge.

## What a Good Plan Looks Like

**Starts with context.** The reader's first question is
"what is this project?" The plan opens with a clear goal
(1-3 sentences) and enough background that a coder
unfamiliar with the project understands what they are
building and why.

**Shows the user interface.** For CLI tools, show example
commands and their expected output. For APIs, show
endpoint signatures and example request/responses. For
libraries, show how a caller would use the public API.
The coder needs to know what the end user will experience
before they can design the internals.

**Information dense.** Every sentence carries weight. No
filler, no throat-clearing, no restating the obvious. If
a sentence could be removed without losing information,
remove it.

**Standalone.** A coder who has never seen this codebase
should be able to read PLAN.md and understand what to
build, where it fits, and why. Do not assume the reader
has context from the research, interview, design docs, or
any other file. If a design doc specifies the board view
layout, the plan must describe that layout — not say "see
design.md lines 149-165." References to source files are
useful as *pointers for further reading*, but the plan
itself must contain every specification the coder needs.
If the plan were the only document the coder received,
they should still be able to build the project.

**Context-rich.** Link to every relevant file, module,
interface, and convention. Use absolute file paths with
line numbers where useful (e.g.,
`src/auth/middleware.ts:42`). The coder will use these as
starting points.

**Describes what, not how.** State what the system should
do and what properties it should have. Do not prescribe
implementation steps, function signatures, or class
hierarchies — coders have more context at implementation
time than you do now. API surface definitions are fine
when they are part of a contract (e.g., an endpoint spec
or a public interface), but internal implementation
details are not.

**Trusts the coder.** The plan provides context and goals.
The coder makes implementation choices. When there are
multiple valid approaches, note them briefly and state any
constraints that would rule one out, but do not pick for
the coder unless there is a clear reason to.

**No task breakdown.** The plan describes the project
holistically. Task decomposition happens later in the Task
Generator phase. You may describe high-level workstreams
(e.g., "this involves changes to the API layer, the
storage layer, and the CLI") but do not enumerate specific
tasks, acceptance criteria, or dependency graphs.

**No diagrams.** No ASCII art, no Mermaid, no flowcharts.
Use prose and lists.

**No pseudocode.** Do not include code blocks showing
"here is roughly what the implementation should look
like." If you need to specify an API contract, a config
schema, or a data format, that is fine — those are
specifications, not pseudocode.

**Concise.** Length should be proportional to project
scope, but every line must earn its place. If you find
yourself writing implementation code, step back and ask:
"Am I describing requirements, or am I writing the code?"
Every line of implementation code in the plan is a line
where you are doing the coder's job with less context
than they will have. 1000 lines of text wrapped at 80
characters is the absolute upper limit.

## Common Anti-Patterns

These are signs the plan has gone too far:

- **Class definitions with method bodies.** A plan should
  say "Cards have an ID, title, description, priority
  (1-5), due date, and labels" — not write out a
  `@dataclass` with `to_dict()` and `from_dict()` methods.
- **Complete function implementations.** If you have
  written a function body, you are coding, not planning.
  Describe what the function should do, not how.
- **Import statements.** These are implementation details.
- **Exception class hierarchies with code.** Say "user
  errors return exit code 1, system errors return exit
  code 2" — do not write the exception classes.
- **Task lists with acceptance criteria.** That is the
  Task Generator's job.
- **Test boilerplate.** Say "tests use temp directories
  for isolation" — do not write setUp/tearDown methods.
- **"Module breakdown" sections listing every file.** The
  coder decides the file structure. Describe the logical
  components and their responsibilities.
- **Delegating to other documents.** "See design.md for
  the display format" is not a plan — it is a pointer.
  The plan must contain the specification itself.

## What IS Appropriate in Code Blocks

Code blocks are appropriate only for **external
specifications**:

- CLI command syntax and example invocations with expected
  output
- Config file format (JSON schema, example config)
- Data format on disk (example JSON structure)
- API endpoint specs (request/response format)
- Exact strings the user will see (error messages, help
  text)

These are contracts the coder must implement to, not
suggestions for how to write the internals.

## Greenfield Projects

For projects built from scratch, the plan should describe:

1. **What the tool does** — user-facing behavior, not
   internal architecture.
2. **How users interact with it** — commands, arguments,
   config options, with examples showing input and
   expected output.
3. **What data exists** — entities, their relationships,
   and key fields at a conceptual level.
4. **Behavioral requirements** — edge cases, error
   handling rules, idempotency expectations, concurrency
   requirements.
5. **Constraints** — stdlib only, specific language
   version, platform requirements, storage conventions.

The coder will decide the file structure, class hierarchy,
module organization, and internal APIs. Trust them.

## Minimum Detail Checklist

The anti-patterns above define a ceiling — do not write
the code. This section defines a floor — do not leave out
specifications the coder needs.

**For CLI tools**, the plan must include:

- Representative command invocations with expected terminal
  output for at least the 2-3 most important commands.
- The exact display format for any non-trivial terminal
  rendering (board views, detail views, tables). Show what
  the user sees, character for character.

**For any project with persistent data**, the plan must
include:

- An example of the primary data format on disk.
- Storage location and file naming conventions.

**For any project with configuration**, the plan must
include:

- All config keys, their types, default values, and valid
  values.
- An example config file.

**For any project with filtering, sorting, or querying**,
the plan must specify:

- How multiple filters combine (AND vs OR).
- Default sort order.
- What "no results" looks like.

The goal is concrete specifications a coder can test
against. "Cards can be filtered by label" is too vague.
"The `--label` flag filters to cards that have that label
assigned; multiple `--label` flags select cards matching
any of the specified labels (OR semantics)" is a testable
specification.

## PLAN.md Structure

```markdown
# Technical Design: [Project Title]

## Goal
[1-3 sentences. What is this project and why?]

## Background
[What the reader needs to know about the current system.
Include file paths. Keep it to what is relevant.]

## Design

### [Workstream or Aspect 1]
[What needs to happen. Properties the result should have.
Relevant files, interfaces, conventions. Constraints.]

### [Workstream or Aspect 2]
[Same structure. Add as many sections as needed.]

## Constraints
[Non-negotiable requirements: compatibility, performance,
conventions, things that must NOT change.]

## Non-Goals
[What this project explicitly does not cover.]

## Open Questions
[Anything unresolved. Omit if none.]

## References
[Links to relevant files, docs, and resources.]
```

Adapt this structure to fit the project. Small projects
may collapse sections. Large projects may add subsections.
The structure serves clarity, not bureaucracy.
