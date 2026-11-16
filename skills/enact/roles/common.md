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
(`dep add <warden> <follow-up>`) so it still runs last.

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
- Send reads together. Every request rereads your whole context, so three
  requests that each read one file cost about three times one request that
  reads all three. Work out every file and search you need, then send them
  in one message as parallel tool calls or one shell command.

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
  fast checks) in the same worktree and run it again. A ticket server outage
  (exit 4) or daily limit (exit 5) leaves the branch and any delivery receipt
  in place. Retry the push after one or five minutes, respectively.
- Write scratch output (build logs, captures, probe scripts) to your
  session's scratchpad or the system temp directory, never into the
  worktree root or the main checkout. Untracked files in your worktree stop
  `enact push` from deleting it; it then reports `kept_worktree`.
- Slow CI runs after your push without you. Do not wait for it, unless
  push printed `watch_ci`: then your vizier has stopped renewing its lease,
  nobody else will repair a failure, and you follow the notice before
  finishing. A failure becomes a Steward bead linked to affected runs. The
  vizier keeps the run alive until all delivered commits, including
  repairs, reach green master; closing your bead does not declare the run
  complete.
- Wait on a long job (a benchmark, a VM or remote run, a sweep, slow CI) in
  the foreground, or as a background command your host tracks and whose
  exit starts your next turn (Claude: Bash with `run_in_background`; for a
  job on another machine, a background loop that polls until it ends).
  Never end your turn on a job nothing will report: no notice wakes you
  when it finishes, and the vizier looks at idle sessions only after 15
  minutes without a bead update. Another bead's work landing is not a job
  to wait on: release instead ("Speculative defaults").
- Staging may be red because of someone else's change. That alone is not
  your problem. Do not stop for it; fast checks still gate your push.

## Task backend

Run `enact backend` from the project's checkout. For `backend: server`,
use the returned executable path wherever these instructions say `tk`.
For `backend: beads`, use `bd` on PATH. Use the same `enact` commands on
both backends for ownership, start, close, release and assignment metadata
so a stale session cannot bypass a fence.

| Task | Ticket server | Beads |
| --- | --- | --- |
| Read | `tk show ID --json` | `bd show ID --json` |
| List open work | `tk list --ancestor EPIC --json` | `bd list --parent EPIC --limit 0 --json` (recurse through child epics) |
| Create | `tk create --title "..." --body-file FILE --json` | `bd create --title "..." --description "..." --json` |
| Comment | `tk comment ID "..." --json` | `bd comments add ID "..." --json` |
| Move or detach | `tk update ID --parent EPIC --json` (empty string to detach) | `bd update ID --parent EPIC --json` (empty string to detach) |
| Add or remove a label | `tk update ID --add-label LABEL --json` or `--remove-label LABEL` | `bd label add ID LABEL --json` or `bd label remove ID LABEL --json` |
| Add or remove an edge | `tk dep add ID ON --json` or `tk dep remove ID ON --json` | `bd dep add ID ON --json` or `bd dep remove ID ON --json` |

Both create commands accept `--acceptance`, `--type`, `--priority`,
`--parent` and `--labels`. Native create returns the new ID in
`results[0].id`. Native IDs are unprefixed. Migrated prefixed IDs still
work as aliases. Use the returned ID and `parent` field for graph work.
Reparenting can make the ID's dotted path differ from its parent.
Native `show` returns an object with `type` and `edges` in both
directions (`src` depends on `dst`). Beads' JSON uses `issue_type`, and
`bd dep list ID --direction=up --json` lists dependents.

Ticket writes publish immediately. On Beads, run `enact sync` after direct
`bd` writes. If it reports dropped local values, reapply the change only
if it still makes sense on the current graph. Older prompts can use the
compatibility shim throughout the transition.

Record decisions, surprises and deviations as comments on your bead.

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

A problem that stops you verifying your own bead is never a nice-to-have.
When a tool, checker or test harness defect means a check your acceptance
relies on cannot run (a name mismatch, an unreachable scenario, a broken
fixture), fix it in your bead if it is within reach, even when it touches
several call sites. If it is genuinely separate work, file a follow-up bead
whose acceptance includes running the checks it unblocks, and record on your
bead which checks went unrun and why. Never park it in a handoff or closing
summary: nothing acts on those, so the gap silently becomes permanent.

If a Warden bead that has not started yet depends on yours, check the
incoming edges as described in [Task backend](#task-backend). If the Warden
will review the affected code, comment on its bead instead of filing one.
The Warden fixes it or files it.

Otherwise create it with a description, verifiable `--acceptance`,
`--priority 2`, and `--labels role:<role>` (`executor` by default).
Use `--parent` to put it under the run epic or relevant milestone.
Add `dep add <new> <prerequisite>` edges where work needs a prerequisite.
Publish as described in [Task backend](#task-backend) before closing.

## Finish

1. Make sure every change is pushed (`enact push`) and essential follow-ups
   are filed.
   If you made no code change, remove your worktree with
   `enact worktree remove <bead>`.
2. Leave a comment starting with `Handoff:` for whoever builds on or reviews
   your work. Give the files and functions touched, decisions and what a
   reviewer should check. `enact brief` shows it to every dependent bead.
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
  the epic by setting its parent to an empty string. Leave it open for that
  host and drop edges so nothing in the epic waits on it.
- **A bead only another machine can do or check**: its failure came from
  that host (its description, log or CI metadata says so), or it needs that
  OS, a device or hardware this host lacks, or that host's local logs. Do
  not try to reach that machine or wait for it. If the failure is the
  machine's own environment (a broken toolchain, a full disk), say so on
  the bead. Pin it with `enact meta <bead> enact_host=<host>`, comment
  why, and `enact release` it; the pin rule above then applies.
- **A reported-only or non-gating bead** (benchmark, measurement,
  exploration) blocking closure or later work: remove its dependents' edges
  so it leaves the critical path; it still runs when it can.
- **A planning or decomposition bead held only by an epic-level edge** can
  start early, since the beads it files stay gated: reparent it to the run
  epic. An epic-level edge that only orders planning is removed.
- **Several beads failing for one cause** (a shared test fault, a broken
  helper): file one fix bead, make each affected bead depend on it, release
  or close yours with a pointer, and keep going.
- **Your bead needs another bead's work that has not reached staging**: its
  change, its fix for a check your push fails, or a Steward's repair. Do not
  wait for it in your session: a watch loop on `origin/staging` holds a run
  slot doing nothing, often for an hour. Commit what you have to your
  branch, add the edge (`dep add <yours> <theirs>`), comment on your bead
  what you need from it and where you stopped, and
  `enact release <bead> --reason "waits on <theirs>"`. Release pushes your
  commits to `enact/<bead>` on the remote, so the next session resumes from
  them on any host (`recovery.md`); uncommitted edits stay behind. If
  release fails because that push was rejected, fix the cause it reports
  and release again; never push the branch yourself. The vizier starts
  your bead again once `<theirs>` closes. If the work you need has no
  bead, file one first. The exception is a bead that is closed already:
  its commits are on staging, so rebase (`enact push` does) instead of
  waiting.
- **A performance figure measured on a loaded machine**: report it, never
  gate on it or file follow-ups chasing it, unless the acceptance criteria
  name a deterministic measure (`warden.md`, "Tests that survive load").

Change only what the default needs; never take over another worker's
assignment. Only missing access, credentials, a decision only the user can
make or an irreversible outward action go to the Justiciar (`recovery.md`),
which drops that one bead from the run rather than holding the run.

## When things go wrong

- **Ordinary difficulty is not a blocker.** Failing tests, confusing code,
  merge conflicts and flaky checks are your job. Keep working.
- **Impossible acceptance, an emergency** (task store gone or corrupt,
  every push rejected, disk full, a core tool broken for everyone) **or a
  resume after an interruption**: read `recovery.md` and follow it.
- Never wait for a human, never end your turn mid-task to ask a question, and
  never leave your bead in progress when you stop for good. If you truly
  cannot continue in an active run, comment the exact state and run
  `enact release <bead> --reason "..."`, then retitle ⛔. Paused or blocked
  runs retain their assignments and unfinished acceptance until explicit
  resume.
