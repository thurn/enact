---
name: enact-sweep
description: Sweep a project's Beads graph for stale beads (open work no run
  or session owns), close the ones that are done or no longer relevant, then
  run the rest unattended as an Enact vizier. User-invoked only.
disable-model-invocation: true
---

# Enact sweep

You are the 🧹 **sweeper**. You find the project's stale beads, close the
ones that no longer matter, and then become the 🔮 vizier for the rest, in
this same turn.

The invocation may add:

- **dry**: report the triage only; close nothing, run nothing;
- **older than N days**: only beads not updated for N days;
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
levels). Run every command from the project's main checkout.

## 1. Pre-flight

1. Title yourself `🧹 Bead sweep`. Run `git -C "$ENACT" pull --ff-only`
   (ignore failure when offline).
2. Unless **dry**, do the vizier's pre-flight checks now, while the user is
   here: project setup and permission mode (enact-plan Phase 1, steps 2
   and 3). Stop on a failure before you close anything.
3. `enact sync`.

## 2. Find stale beads

A bead is **stale** when it is open and nothing will work on it: no
assignee, and no live run owns it. Age alone does not make a bead stale. A
bead filed an hour ago under a dead run is stale; a week-old bead queued in
a live run is not.

1. List candidates: `bd list --status open --limit 0 --json`. Apply
   **older than N days** to `updated_at` if given.
2. Find the live runs: `enact status` lists this host's runs in
   `active_runs`. A run counts as live when its `control.state` is
   `active` and the host's inspect tool shows its vizier session still
   exists. A run whose vizier is gone is dead: nothing will resume it, so
   its beads are candidates (this sweep becomes their new vizier).
3. Read each candidate's epic chain (`bd show <epic> --json`, following
   `parent`). Skip a bead when its chain includes:
   - an epic that is live by step 2;
   - an open epic with an `enact_vizier_route_*` key for another host or
     provider. You cannot see that run's liveness, so list it in the report
     and leave it alone.
4. Skip, and report only, beads labeled `role:steward` (slow-CI repair
   belongs to whoever is watching CI) and beads that have open children
   (the children are triaged; the epic follows them).
5. Report the number of `deferred` beads, which the user parked on purpose.
   Do not triage them.

What remains is the stale set. If it is empty, report that, retitle
`✅ Bead sweep` and end.

## 3. Triage

Sort each stale bead into one of four verdicts:

- **done**: the work has shipped. Cite the commit or the code that does it.
- **obsolete**: superseded, made moot by a later decision, or its code is
  gone. Cite what replaced it.
- **keep**: still valid and still wanted.
- **human**: still valid, but it needs a person (a decision, credentials,
  manual QA, an outside party). The stoppage audit in enact-plan defines
  these.

For each bead, `enact brief <id>` gives its text, comments, epics and
handoffs. Check it against the code: `git log --oneline --since=<created>`
filtered by the bead ID and the files it names, and `rg` for the functions
and behavior it describes. A bead whose sibling beads all closed is usually
done or obsolete. Ask whether a strong engineer joining today would still
do this work.

For more than about eight beads, fan the reading out to read-only
subagents, about five beads each, on a cheaper model (the project's
`[tiers.light]` model when `.enact.toml` has one). Give each subagent the
bead IDs, the verdict definitions above and the project's test commands.
Ask for a verdict, one line of evidence (a hash or `file:line`) and, for
**keep**, what in the bead's context packet is now wrong: renamed files,
helpers that moved, steps already done. Subagents never write to Beads.
You decide on every close.

## 4. Act

Show the user a table: bead, title, verdict, evidence. Under **dry**, stop
here, retitle `✅ Bead sweep` and end.

1. **done / obsolete**: `bd close <id> --reason "Sweep <date>: <verdict>.
   <evidence>" --json`. Then run `bd epic close-eligible --json` to close
   epics left with only closed children.
2. **keep**: when the triage found the context packet wrong, add the
   corrections with `bd comments add <id> "Sweep <date>: ..."` so the
   worker does not rediscover them.
3. **human**: ask the user now (retitle `🔍 Bead sweep` while you wait),
   one question at a time with your recommendation, the way enact-plan's
   stoppage audit does. A resolved bead becomes **keep**, with the answer
   added as a comment. Defer an unresolved one (`bd defer <id>`) and list
   it in the final report.
4. `enact sync`.

## 5. Run the rest

If no **keep** beads remain, retitle `✅ Bead sweep`, report and end.

Otherwise, read `$ENACT/skills/enact-plan/SKILL.md` and follow it as the
vizier, with the **keep** bead IDs as its input ("several beads") and the
invocation's parallelism, **light** and role instructions. Answer its
questions from here instead of asking again:

- Title the new run epic `Bead sweep <date>`. Its description lists the
  kept beads and each one's triage note.
- When a kept bead already has a parent epic, move it into the sweep epic.
  Do not run the old epic. Close an old epic left with no open children
  (`--reason "Sweep <date>: remaining beads moved to <sweep epic>"`).
- Group beads that touch the same code into one `lane:<name>`. Label
  mechanical ones `tier:light`, or assign every bead `tier:sonnet`,
  `tier:opus` or `tier:fable` when the user asks for models by name
  (enact-plan, "Build the graph").
- You already did the stoppage audit in step 4.

From then on you are the vizier: retitle `🔮 [<epic>] Bead sweep` and run
its loop to the end. Its `✅` and `⛔` titles replace this skill's.

## Final report

Keep it short: the counts per verdict, the closed beads with one-line
reasons, the deferred beads and why, the beads you left to other hosts'
runs, and the run epic if one started.
