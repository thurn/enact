---
name: enact
description: Execute Enact work inside this session, using subagents for
  epics. Takes a bead ID or a task prompt and waits for slow CI before
  finishing. Never starts other sessions. User-invoked only. For unattended
  multi-session runs and plan documents, use /enact-plan.
disable-model-invocation: true
---

# Enact inline

You run Enact work in this session. Beads, worktrees, `enact push` and slow
CI work exactly as in `/enact-plan`, but nothing runs outside this session:
you do bead work yourself, or hand it to subagents and wait for them. Your
session title always shows which role is working.

The invocation text is your input. It is one of:

- a **plain bead ID**: you are its Executor;
- an **epic ID**: you orchestrate its beads, one subagent per bead;
- a **task prompt**: file one executor bead, then you are its Executor.

A plan document (a path) is not an input: tell the user to use
`/enact-plan` and stop. The invocation may also give a parallelism level for
an epic (default **1**), the keyword **light** for a
[light run](#light-runs), and other instructions, which override this file.

## Never

- Never start a session of any kind: no `start_session`, `create_thread`,
  Marshal, scheduled task or automation, and no Justiciar. Those belong to
  `/enact-plan`. Never run `enact run register` or `enact next`.
- Never file Mason, Sage or Archivist beads. Run those roles only for beads
  an epic already contains, and never in a light run.
- Never end your turn while delivered work is waiting for slow CI.

## Paths and identity

- `ENACT` is the Enact repository clone: resolve this skill's base directory
  with `realpath` and go up two levels (conventionally `~/enact`). Role files
  are in `$ENACT/skills/enact/roles/`, written `ROLES` below.
- The helper is `python3 "$ENACT/scripts/enact.py"` (`py -3` on Windows),
  written `enact` below. Every subcommand prints one JSON object.
- Run all commands from the project's main checkout.
- Subagents share your session ID, so a bead you claim with `enact claim`
  is theirs to push and close.

## Titles

Title yourself with `set_session_title` on `self` (Claude Desktop; load it
with ToolSearch first) or `set_thread_title` (Codex), as
`<emoji> [<id>] <short summary>`:

- your own role while you do bead work yourself: ⚒️ Executor;
- the running subagent's role while you wait for it: ⚒️ Executor,
  🛡️ Warden, 🚒 Steward, 🧱 Mason, 📖 Sage, 📜 Archivist; with several at
  once, all their emojis (`⚒️🛡️ [<epic>] ...`);
- 🔮 while you orchestrate between subagents or wait for slow CI;
- ✅ when finished, or ⛔ when you stopped without finishing.

Retitle before each subagent starts and after it returns. `<id>` is the
bead you are working, or the epic for an epic.

## Pre-flight

1. `git -C "$ENACT" pull --ff-only` (ignore failure when offline).
2. `.enact.toml` and `.beads/` must exist and the remote must have a
   `staging` branch. If not, tell the user to run `/enact-setup` and stop.
3. Read `AGENTS.md` (its Enact section may restrict roles or set review
   policy; the invocation overrides it) and `.enact.toml`.
4. Classify the input: `bd show <input> --json` finds a bead (an epic if its
   `issue_type` is `epic`); otherwise a path to a file is a plan document;
   otherwise it is a task prompt.

## Review

Every implementation gets one of:

- **Executor only**, for a routine change;
- **Executor + Warden**, for a change that is large or risky (data loss,
  concurrency, security, many files), already followed by a Warden bead, or
  when the user or `AGENTS.md` asks for one.

A [light run](#light-runs) narrows this to at most one final Warden.

A `review:inline` label asks for a cold review: you give it yourself before
pushing when you are the Executor, and a Warden gives it after an Executor
subagent pushes (subagents do not start subagents).

The Warden always runs in a fresh subagent on its own bead, after the
Executor has pushed. When no Warden bead exists yet, file one:

```sh
bd create --title "Review <bead>: <summary>" --description "..." \
  --acceptance "..." --labels role:warden [--parent <epic>] --json
bd dep add <warden bead> <executor bead>
enact sync
```

## Light runs

**light** trades robustness for velocity, overriding `AGENTS.md`'s role
and review policy:

- No Mason, Sage or Archivist work.
- At most one review, at the end. Ignore `review:inline` labels and run no
  per-bead Wardens. Unless the whole run is one routine change, file a
  single final Warden bead depending on every executor bead in the run.
- Label the run `run:light`: `bd label add <input> run:light` on an input
  bead or epic, and `--labels` on beads you file (beads filed under a
  labeled epic inherit it). Subagents read it to keep follow-ups and the
  final review light.
- An epic: first close each unclaimed Mason, Sage, Archivist and Warden
  bead in it (`enact claim <id>`, then `enact close <id> --reason "skipped:
  light run"`; on exit 3 leave it). The final Warden depends on the beads
  those Wardens would have reviewed and every open executor bead.

## A task prompt

Read enough code to scope the task. If it plainly needs a human (a choice
between options, credentials, a manual step), settle that with the user
now. If it describes a large work item (several independent pieces,
phases, days of work), tell the user to use `/enact-plan` and stop.

File one executor bead carrying the whole prompt, with acceptance criteria
an agent can verify (`bd create ... --labels role:executor --json`, then
`enact sync`), and continue as for a plain bead.

## A plain bead

You implement it yourself, in this session.

1. `enact claim <bead>`. Exit 3 means someone else holds it or it is
   closed: report that and stop.
2. Title yourself ⚒️, then read `ROLES/common.md` and `ROLES/executor.md`
   and follow them as the Executor, with the inline rules in `common.md`.
   Outside a light run, a `review:inline` label means you run that
   cold-review subagent before pushing, as `executor.md` describes; that
   replaces the Warden.
3. After you close the bead, decide [review](#review). For Executor +
   Warden, claim the Warden bead and run it as a subagent
   ([Subagents](#subagents)).
4. Follow-up beads filed during the work are reported, not executed. Only
   Steward work for slow CI runs here.

Then go to [Slow CI](#slow-ci).

## An epic

Every bead in the epic runs in a subagent. You only pick, claim, title,
launch and check. Loop:

1. `bd ready --parent <epic> --json`. Drop epics (gate epics close
   themselves), beads assigned to someone else, and beads whose
   `enact_host` or `enact_provider` metadata names another machine or
   provider. Hold an Archivist bead until every other bead in the epic is
   closed.
2. Nothing ready: if every descendant is closed, the epic is done. If open
   beads remain and no subagent of yours is running, the run is
   deadlocked or held elsewhere: report the open beads and stop (⛔).
3. Take the highest-priority bead (lowest number), or up to the
   parallelism level of beads that will not edit the same code.
   `enact claim <bead>` each; on exit 3 skip it.
4. Title yourself with the running roles and launch one subagent per bead.
   The role is the bead's `role:` label (`executor` when unlabeled).
5. When a subagent returns, check `bd show <bead> --json`. If the bead is
   still open, read the subagent's summary and the bead's comments: if the
   work can continue, launch one fresh subagent for the same bead; if not
   (or the second one also stops), comment the state on the bead, run
   `enact release <bead> --reason "..."`, and stop with a report (⛔).
6. Outside a light run, a closed Executor bead labeled `review:inline` with
   no Warden bead after it gets one now ([Review](#review)), filed under
   the same parent.
7. Follow-up beads subagents file under the epic join the loop by
   themselves. Repeat.

Then go to [Slow CI](#slow-ci). Closing the last bead closes the epic.

## Subagents

Use a general-purpose subagent with a fresh context (Claude: the Agent tool;
Codex: a spawned agent) and this prompt, filled in:

> You are the `<emoji>` `<Role>` for bead `<bead>` in an inline Enact run.
> Read `<ROLES>/common.md` and then `<ROLES>/<role>.md` and follow them,
> including the inline-run rules in `common.md`. The helper is
> `<enact command>`; the project's main checkout is `<path>`. The bead is
> already claimed for you. Work unattended: nobody will answer questions.
> End with a short summary: outcome, each `staging` SHA you pushed, and any
> follow-up beads you filed.

Pass nothing else: no opinion of the work, no plan beyond the bead. Note
each pushed `staging` SHA from the summary or the bead's `enact_commits`.

## Slow CI

Before you finish, the last `staging` SHA delivered in this run (by you or a
subagent) must reach `master`. Each delivery is rebased onto the previous
one, so the newest SHA covers them all.

1. Title yourself 🔮 and run `enact ci --wait <sha>` (600000 ms tool
   timeout). Repeat on `waiting` until it reports `passed`.
2. On `failed`: `enact claim <steward bead>` for each listed Steward bead,
   title yourself 🚒 and run it as a Steward subagent. Then wait the same
   way on the SHA its fix pushed.

## Rules

- Use raw `bd ... --json` for reads and filing and `enact sync` afterward;
  `enact claim`, `close` and `release` sync themselves.
- The user is here, but do not stop to ask about things an agent can
  decide. Ask only for a genuine human decision or missing access.
- An emergency (corrupt Beads database, every push rejected, disk full, a
  core tool broken) stops the run: report the evidence to the user. Never
  request or start a Justiciar.
- A user stop: let a running subagent finish or interrupt it, leave its
  bead's state in a comment, release beads nobody is working, and report.
- Finish with a short report: what shipped (beads, SHAs), follow-ups filed
  and not run, the slow-CI result, and anything for the user to look at.

## Reference

The design rationale is in `$ENACT/docs/design.md`. `/enact-plan` is the
multi-session vizier for plan documents and unattended runs.
