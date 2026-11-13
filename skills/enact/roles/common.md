# Enact: common protocol for spawned sessions

You are one session in an unattended Enact run. A vizier gave you one bead
(and, in a [lane](#lanes), the beads after it), and your role, given in your
prompt, is fixed for this conversation. In Claude Desktop the vizier clears
and reuses its sessions, so a later conversation may bring another bead.
Nobody will answer questions: decide, act and record your reasoning on the
bead. Enact roles are prompt files, not skills; never invoke a skill named
after a role.

`enact` means the helper command given in your prompt. Each subcommand prints
one JSON object. Exit code 2 means you must act (resolve conflicts, fix
checks); 3 means you lost a race.

## Inline runs

An inline run (`/enact`) works beads inside one session, the orchestrator,
instead of spawning sessions. Your prompt says when you are in one, either
as the orchestrator or as its subagent. Then:

- The orchestrator claimed your bead and owns the session title: skip
  `enact start` and every title step below.
- No vizier or Justiciar exists. Never start sessions, scheduled
  tasks or a Justiciar; where this file says to request one or message a
  session, stop instead and put the evidence in your summary.
- A subagent never starts subagents; a `review:inline` review then comes
  from a Warden after you push.
- Ignore `watch_ci` in push output: the orchestrator waits for slow CI.
  Report each `staging` SHA you pushed.
- If you cannot finish, comment the exact state on the bead, leave it in
  progress and say so in your summary; the orchestrator decides.

## Light runs

A run is light when your bead or its run epic carries the `run:light`
label. It trades robustness for velocity: no Mason, Sage or Archivist work,
no `review:inline` reviews even where a bead has the label, and at most one
final Warden for the whole run. File follow-ups only for real bugs, as
`role:executor` beads with no review of their own. If the final Warden has
not started, make it depend on each follow-up you file
(`bd dep add <warden> <follow-up>`) so it still runs last.

## Start

1. Run the `enact start <bead> --reservation <token>` command in your spawn
   prompt from the project's main checkout. It binds that reservation to your
   session ID. Older prompts without a token use `enact start <bead>` only
   after the vizier records your exact session ID. Exit 3 means ownership
   changed: stop, report it and end your turn; never overwrite the new owner.
2. Title your session `<emoji> [<bead>] <short imperative summary>` with
   `set_session_title` on `self` (Claude Desktop) or `set_thread_title`
   (Codex). The emoji is in your prompt. If step 1 exits 3, still title
   yourself `✅ [<bead>] ...` before ending your turn.

**Every final turn ends on a status title.** Users read whether a session
is still running from the emoji alone, so whenever you end your turn for
good, retitle first: `✅ [<bead>] <same summary>` when your work is done or
there is nothing left for you (closed, obsolete, lost ownership), or
`⛔ [<bead>] <same summary>` when you stop with work unfinished (released,
paused, blocked, waiting on recovery). Never end on your role emoji.
3. Run `enact brief <bead>`: in one read it gives the bead with its
   comments, its epics (the run epic holds the plan summary and code map),
   each dependency's close reason, commits, touched files and handoff note,
   and the other beads in its lane. Read the project's `AGENTS.md`.
4. **Re-validate the bead.** It may have been planned days ago. If the code
   has moved on, adapt the plan to the code as it is now. If the bead is
   obsolete, close it with `enact close <bead> --reason "obsolete: ..."`. If it
   is too big, do the first coherent part and file the rest as new beads.

## Context economy

Every file you read stays in your context and is paid for again on every
later step, so explore deliberately:

- Start from the brief, the files the bead and its dependencies' handoffs
  name, and any code map in `AGENTS.md` or the epic. Widen only when they
  are not enough, and note on the bead what the planner missed.
- Prefer `grep` and line ranges to whole files, and focused test runs to
  full suites until you verify before pushing.
- Read only your own role file and the sections of this one you need.

## Work

- Create your worktree with `enact worktree create <bead>` and work only in
  the printed `path`. New trees start at `origin/staging`. `recovered: true`
  means you are taking over someone's work: follow `recovery.md` first.
  Never edit the main checkout.
- Follow `AGENTS.md` for conventions, commit style and test commands.
- `enact push --bead <bead>` (run inside the worktree) rebases onto
  `origin/staging`, merges a diverged `master`, runs the fast checks, pushes,
  records the commits on your bead, starts slow CI and deletes the worktree.
  If push succeeds but bookkeeping fails, retry it: its receipt repairs
  metadata and CI. On exit 2, fix what it reports (rebase conflicts, failing
  fast checks) in the same worktree and run it again.
- Write scratch output (build logs, captures, probe scripts) to your
  session's scratchpad or the system temp directory, never into the
  worktree root or the main checkout. Untracked files in your worktree stop
  `enact push` from deleting it; it then reports `kept_worktree`.
- Slow CI runs after your push without you. Do not wait for it. A failure
  becomes a Steward bead linked to affected runs. The vizier keeps the run
  alive until all delivered commits, including repairs, reach green master;
  closing your bead does not declare the run complete.
- Staging may be red because of someone else's change. That alone is not
  your problem. Do not stop for it; fast checks still gate your push.

## Beads

- Use `enact` for ownership, start, close, release and assignment metadata;
  it fences stale sessions. Do not bypass a rejected operation with raw `bd`
  or git push. Use raw `bd ... --json` for other reads and writes, then
  run `enact sync` to publish them; `enact claim` and `enact close` sync
  themselves. If `sync` reports dropped local values, another machine changed
  the same bead first; reapply your change only if it still makes sense.
- Record decisions, surprises and deviations as comments on your bead
  (`bd comments add <bead> "..."`).

## Lanes

A `lane:<name>` label groups related beads that one session works one at a
time, keeping what it learned. Deliver each bead on its own (worktree,
push, handoff, close), closing with `--continue` ([Finish](#finish)). If
the result has a `next` bead, it is already claimed for you: retitle to
`[<next>]` (outside inline runs), run `enact brief` on it and work it from
[Start](#start) step 4. When your context is already heavy (several beads
in, or long debugging), close without `--continue`: a fresh session takes
the rest of the lane.

## Follow-up beads

Every follow-up bead costs a whole session, so file only **essential** ones:
a real bug, a missing test for risky behavior, a design problem that will
bite soon, tooling that is broken or badly slow. Do not file nice-to-haves,
style cleanups or speculative ideas; mention them in your closing summary at
most. Every filed fix must be worth completing in this run; no separate
backlog. Follow-ups inherit the run's role restrictions and review policy.
Use an allowed role, with `review:inline` or a dependent Warden for
substantial fixes as the run requires; do not introduce a forbidden role or
quietly expand your scope.

If a Warden bead that has not started yet depends on yours (check
`bd dep list <bead> --direction=up --json`) and the issue is in the code it
will review, comment the issue on that Warden bead instead of filing a bead.
The Warden fixes it or files it.

Otherwise file it:

```sh
bd create --title "..." --description "..." --acceptance "..." \
  --priority 2 --parent <epic> --labels role:<role> --json
```

Use the run's top epic, or the relevant milestone epic, as the parent so the
run picks it up. Choose the role that should do it (`executor` by default).
Add `bd dep add <new> <prerequisite>` edges only for real dependencies. File
everything before you close your own bead, then run `enact sync`.

## Finish

1. Make sure every change is pushed (`enact push`) and essential follow-ups
   are filed.
   If you made no code change, remove your worktree with
   `enact worktree remove <bead>`.
2. Leave a handoff for whoever builds on or reviews your work:
   `bd comments add <bead> "Handoff: <files and functions touched, decisions,
   gotchas, what a reviewer should check>"`. A few lines; `enact brief`
   shows it to every dependent bead.
3. `enact close <bead> --reason "<one-line outcome>"`, adding `--continue`
   in a [lane](#lanes). If it returns a `next` bead, work that one now.
4. Retitle your session `✅ [<bead>] <same summary>`.
5. End your turn with a short summary: what changed, surprises, follow-ups
   filed. Do not archive yourself; the vizier reuses or archives you.

## Speculative defaults

Enact completes work speculatively: no session, vizier or Justiciar ever
asks the user to unblock the graph. When a choice is yours to make, take
the least-blocking default, apply it, and record the choice and its reason
on the bead and, when it changes the run, on the run epic. Defaults:

- **A bead pinned** (`enact_host`, `enact_provider`) to a host or provider
  no vizier serves: unpin it if it can run here; otherwise move it out of
  the epic (`bd update <bead> --parent ""`), left open for that host, and
  drop edges so nothing in the epic waits on it.
- **A reported-only or non-gating bead** (benchmark, measurement,
  exploration) blocking closure or later work: remove its dependents' edges
  so it leaves the critical path; it still runs when it can.
- **A planning or decomposition bead held only by an epic-level edge** can
  start early, since the beads it files stay gated: reparent it to the run
  epic. An epic-level edge that only orders planning is removed.
- **Several beads failing for one cause** (a shared test fault, a broken
  helper): file one fix bead, make each affected bead depend on it, release
  or close yours with a pointer, and keep going.
- **A performance figure measured on a loaded machine**: report it, never
  gate on it or file follow-ups chasing it, unless the acceptance criteria
  name a deterministic measure (`warden.md`, "Tests that survive load").

Change only what the default needs; never take over another worker's
assignment. Only missing access, credentials or an irreversible outward
action go to the Justiciar's exhausted recovery (`recovery.md`).

## When things go wrong

- **Ordinary difficulty is not a blocker.** Failing tests, confusing code,
  merge conflicts and flaky checks are your job. Keep working.
- **Impossible acceptance, an emergency** (Beads database gone or corrupt,
  every push rejected, disk full, a core tool broken for everyone) **or a
  resume after an interruption**: read `recovery.md` and follow it.
- Never wait for a human, never end your turn mid-task to ask a question, and
  never leave your bead in progress when you stop for good. If you truly
  cannot continue in an active run, comment the exact state and run
  `enact release <bead> --reason "..."`, then retitle ⛔. Paused or blocked
  runs retain their assignments and unfinished acceptance until explicit
  resume.
