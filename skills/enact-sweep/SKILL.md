---
name: enact-sweep
description: Sweep a project's task graph for stale tickets (open work no run
  or session owns), close the ones that are done or no longer relevant, then
  run the rest unattended as an Enact vizier. User-invoked only.
disable-model-invocation: true
---

# Enact sweep

You are the 🧹 **sweeper**. You find the project's stale tickets, close the
ones that no longer matter, and then become the 🔮 vizier for the rest, in
this same turn.

The invocation may add:

- **dry**: report the triage only; close nothing, run nothing;
- **older than N days**: only tickets not updated for N days;
- a parallelism level, **light**, role restrictions or other instructions,
  passed on to the vizier unchanged.

## Why the run starts in this turn

Claude Desktop only lets a session start other sessions during a turn the
user started. A scheduled task, a wakeup or another session's message
cannot start a run. So "schedule a run" means the vizier starts its session
pool here, right after the sweep, in the turn the user started with
`/enact-sweep`. After that it waits in the background. On Codex Desktop the
vizier starts fresh chats as work becomes ready and waits in the foreground.

## Paths

`ENACT` and the helper (`enact` below) are as in
`$ENACT/skills/enact-plan/SKILL.md` (this skill's base directory, up two
levels). Run every command from the project's main checkout. Follow
[Task backend](../enact/roles/common.md#task-backend) for task commands.

## 1. Pre-flight

1. Title yourself `🧹 Bead sweep`. Run `git -C "$ENACT" pull --ff-only`
   (ignore failure when offline).
2. Unless **dry**, do the vizier's pre-flight checks now, while the user is
   here: project setup and permission mode (enact-plan Phase 1, steps 2
   and 3). Stop on a failure before you close anything.
3. Run `enact backend`.

## 2. Find stale tickets

A ticket is **stale** when it is open and nothing will work on it: no
assignee, and no live run owns it. Age alone does not make a ticket stale. A
ticket filed an hour ago under a dead run is stale; a week-old ticket queued in
a live run is not.

1. List candidates with `tk list --status open --json` and keep unassigned work
   and apply **older than N days** to `updated_at` if given.
2. Find the live runs: `enact status` lists this host's runs in
   `active_runs`. A run counts as live when its `control.state` is
   `active` and the host's inspect tool shows its vizier session still
   exists. A run whose vizier is gone is dead: nothing will resume it, so
   its tickets are candidates (this sweep becomes their new vizier).
3. Read each candidate's epic chain with `show --json` on the active CLI,
   following `parent`. Skip a ticket when its chain includes:
   - an epic that is live by step 2;
   - an open epic with an `enact_vizier_route_*` key for another host or
     provider. You cannot see that run's liveness, so list it in the report
     and leave it alone.
4. Skip, and report only, tickets labeled `role:steward` (slow-CI repair
   belongs to whoever is watching CI) and tickets that have open children
   (the children are triaged; the epic follows them).
5. Skip and count parked work. This includes tasks with status `deferred`
   and, on the server, open tickets whose `defer_until` is still in the
   future.

What remains is the stale set. If it is empty, report that, retitle
`✅ Bead sweep` and end.

## 3. Triage

Sort each stale ticket into one of four verdicts:

- **done**: the work has shipped. Cite the commit or the code that does it.
- **obsolete**: superseded, made moot by a later decision, or its code is
  gone. Cite what replaced it.
- **keep**: still valid and still wanted.
- **human**: still valid, but it needs a person (a decision, credentials,
  manual QA, an outside party) or a machine or device no vizier here will
  serve. The stoppage audit in enact-plan defines these.

For each ticket, `enact brief <id>` gives its text, comments, epics and
handoffs. Check it against the code: `git log --oneline --since=<created>`
filtered by the ticket ID and the files it names, and `rg` for the functions
and behavior it describes. A ticket whose sibling tickets all closed is usually
done or obsolete. Ask whether a strong engineer joining today would still
do this work.

For more than about eight tickets, fan the reading out to read-only
subagents, about five tickets each, on a cheaper model (the project's
`[tiers.light]` model when `.enact.toml` has one). Give each subagent the
ticket IDs, the verdict definitions above and the project's test commands.
Ask for a verdict, one line of evidence (a hash or `file:line`) and, for
**keep**, what in the ticket's context packet is now wrong: renamed files,
helpers that moved, steps already done. Subagents only read the task store.
You decide on every close.

## 4. Act

Show the user a table: ticket, title, verdict, evidence. Under **dry**, stop
here, retitle `✅ Bead sweep` and end.

1. **done / obsolete**: Claim with `enact claim <id>`, then run
   `enact close <id> --reason "Sweep <date>: <verdict>. <evidence>"`.
   On exit 3, leave the task to its current owner. Finished parent epics
   close through the ticket server.
2. **keep**: when the triage found the context packet wrong, add the
   corrections in a comment beginning with `Sweep <date>:` so the worker
   does not rediscover them.
3. **human**: ask the user now (retitle `🔍 Bead sweep` while you wait),
   one question at a time with your recommendation, the way enact-plan's
   stoppage audit does. A resolved ticket becomes **keep**, with the answer
   added as a comment. Park unresolved work with
   `tk update ID --status deferred --if-assignee "" --json`. List it in the final report.

## 5. Run the rest

If no **keep** tickets remain, retitle `✅ Bead sweep`, report and end.

Otherwise, read `$ENACT/skills/enact-plan/SKILL.md` and follow it as the
vizier, with the **keep** ticket IDs as its input ("several tickets") and the
invocation's parallelism, **light** and role instructions. Answer its
questions from here instead of asking again:

- Title the new run epic `Bead sweep <date>`. Its description lists the
  kept tickets and each one's triage note.
- When a kept ticket already has a parent epic, move it into the sweep epic.
  Do not run the old epic. If moving its last open child leaves it open,
  claim it and close it with
  `--reason "Sweep <date>: remaining tickets moved to <sweep epic>"`.
- Group tickets that touch the same code into one `lane:<name>`. Label
  mechanical ones `tier:light`, or assign every ticket `tier:sonnet`,
  `tier:opus` or `tier:fable` when the user asks for models by name
  (enact-plan, "Build the graph").
- You already did the stoppage audit in step 4.

From then on you are the vizier: retitle `🔮 [<epic>] Bead sweep` and run
its loop to the end. Its `✅` and `⛔` titles replace this skill's.

## Final report

Keep it short: the counts per verdict, the closed tickets with one-line
reasons, the deferred tickets and why, the tickets you left to other hosts'
runs, and the run epic if one started.
