---
name: enact-intake
description: Turn a task description or project plan into an Enact epic ready
  for unattended execution, by auditing it for stoppage risks with the user
  and filing the bead graph. User-invoked only.
disable-model-invocation: true
---

# Enact intake

You turn a plan into a Beads graph that `/enact` can execute for days with no
human. This is the only interactive step of an Enact run: the user is
present now and will not be later. You plan and file beads; you do not start
sessions or write code.

The invocation text is your input: a task description or a path to a plan
document, plus optional role restrictions (for example "no Mason or Sage")
and any other instructions, which override this file.

## Paths

- `ENACT` is the Enact repository clone: resolve this skill's base directory
  with `realpath` and go up two levels (conventionally `~/enact`).
- The helper is `python3 "$ENACT/scripts/enact.py"` (`py -3` on Windows),
  written `enact` below. Every subcommand prints one JSON object.
- Run all commands from the project's main checkout.

## 1. Context

1. `.enact.toml` and `.beads/` must exist and the remote must have a
   `staging` branch. If not, tell the user to run `/enact-setup` first and
   stop.
2. Read the plan, `AGENTS.md` (especially its Enact section) and
   `.enact.toml`. AGENTS.md may restrict roles or set review policy; the
   invocation overrides AGENTS.md.
3. Read enough of the codebase to judge the plan and decompose it well.

## 2. Stoppage audit

Enact only accepts a plan it can execute to completion with no human. Before
filing anything, list every stoppage risk in the plan:

- decisions left to the user ("choose between A and B", "confirm the design");
- manual steps: sign-offs, QA by a person, UI review, approvals;
- credentials, accounts, payments or access that agents do not have;
- external parties: waiting on someone, a release window, a deploy;
- anything irreversible or outward-facing the user has not authorized.

Resolve each risk with the user now: get the decision, record a default, or
cut the step from scope. Ask one question at a time with your recommendation.
If a risk cannot be resolved, refuse to file and say why. Never file a bead
that needs a human later; express acceptance as checks an agent can run.

## 3. Build the graph

File the graph with raw `bd` (always `--json`; see `bd create --help`):

- **One epic** for the run (`--type epic`) whose description holds the plan
  summary, a link to the plan document, the decisions from the audit, the
  role restrictions, and whole-run acceptance criteria.
- **Milestones** as child epics when the plan has phases. Milestone N+1
  depends on milestone N (or on its gate, below) with `bd dep add`.
- **Beads** as children (`--parent`), each completable by one fresh session in
  one sitting, with a standalone description, the relevant plan excerpt and
  file pointers, `--acceptance` criteria an agent can verify, and `--priority`.
  Give each its role label (`--labels role:<role>`); unlabeled beads are
  executor beads.
- **Dependencies** only where real (`bd dep add <dependent> <prerequisite>`).
  Unrelated work stays parallel. Two beads that would edit the same code
  non-additively get an ordering edge.

Decompose fully up front by default. For a large or uncertain plan you may
instead file concrete beads for the first milestone plus one executor bead
per later milestone: "Decompose milestone N into beads", parented under that
milestone epic and gated on the previous milestone.

### Roles

Use every role unless AGENTS.md or the invocation restricts them:

- **Executor** (`role:executor`, the default): implements.
- **Warden** (`role:warden`): fresh review that fixes problems itself. By
  default file one Warden bead per substantial implementation bead,
  depending on it; batch small related beads under one Warden bead that
  depends on all of them. Work that builds on that code depends on the Warden
  bead; unrelated work does not.
- **Mason** (`role:mason`): architecture audit at milestone boundaries; one
  pass at the end for a small project.
- **Sage** (`role:sage`): process and tooling audit ("why was this slow?") at
  milestone boundaries.
- **Steward** (`role:steward`): never planned; the slow-CI runner files these.
- **Justiciar** and **Marshal**: never filed as beads.

### Audit gates

Beads only lets epics block epics, and a blocked epic hides its children
from `bd ready`. So each Mason or Sage audit gets a small **gate epic**, for
example "M1 review gate", with the next milestone epic depending on it. The
audit bead is a child of the gate epic. The auditor files important findings
as further children of the gate, so the next milestone waits for them. The
helper closes a gate automatically once all its children are closed.

## 4. Finish

1. Run `enact sync`, then check the graph with `bd list --parent <epic>` and
   `bd ready --parent <epic>`: something must be ready, and every bead must
   be reachable.
2. Report to the user: the epic ID, bead count, milestones and gates, the
   audit decisions, and a suggested parallelism (how many beads can usefully
   run at once).
3. End with the exact line to start execution, for example
   `/enact <epic> parallelism 2` (`$enact ...` in Codex). It can run in this
   session or in a new one, on this machine or on others, and several viziers
   may work the same epic.
