# Enact: common protocol for spawned sessions

You are one session in an unattended Enact run. A vizier started you for one
bead, and your role, given in your prompt, is fixed for the whole session.
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
- No vizier, Marshal or Justiciar exists. Never start sessions, scheduled
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
   (Codex). The emoji is in your prompt.
3. Read the bead in full (`bd show <bead> --json`), its parent epic, and the
   beads it depends on. Read the project's `AGENTS.md`.
4. **Re-validate the bead.** It may have been planned days ago. If the code
   has moved on, adapt the plan to the code as it is now. If the bead is
   obsolete, close it with `enact close <bead> --reason "obsolete: ..."`. If it
   is too big, do the first coherent part and file the rest as new beads.

## Work

- Create your worktree with `enact worktree create <bead>` and work only in
  the printed `path`. New trees start at `origin/staging`. `recovered: true`
  means the prior owner explicitly released, or a routed lookup confirmed it
  inactive. Inspect that tree's commits and edits against current staging;
  preserve useful work and finish it. This is the authorized local handoff.
  Remote replacements start at the latest pushed state; `enact_recovery`
  retains the original host, path, branch, head and chat route. Never delete
  that original work or edit an unassigned tree. Never edit the main checkout.
- Follow `AGENTS.md` for conventions, commit style and test commands.
- `enact push --bead <bead>` (run inside the worktree) rebases onto
  `origin/staging`, merges a diverged `master`, runs the fast checks, pushes,
  appends the commits to your bead, starts the slow-CI runner from a durable
  helper copy and deletes the worktree. If push succeeds but bookkeeping
  fails, retry `enact push`: its receipt repairs metadata and CI even with
  nothing new to push. On exit 2, fix what it reports (rebase conflicts,
  failing fast checks) in the same worktree and run it again. Resolving
  conflicts is your job.
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
  run `enact sync` to publish them to the Dolt remote. `enact claim` and
  `enact close` sync
  themselves. If `sync` reports dropped local values, another machine changed
  the same bead first; reapply your change only if it still makes sense.
- Record decisions, surprises and deviations as comments on your bead
  (`bd comments add <bead> "..."`).

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
2. `enact close <bead> --reason "<one-line outcome>"`.
3. Retitle your session `✅ [<bead>] <same summary>`.
4. End your turn with a short summary: what changed, surprises, follow-ups
   filed. Do not archive yourself; the vizier does that.

## When things go wrong

- **Ordinary difficulty is not a blocker.** Failing tests, confusing code,
  merge conflicts and flaky checks are your job. Keep working.
- **Genuinely impossible acceptance:** preserve the open requirement and
  request Justiciar recovery with the evidence. Do not close it as delivered
  or repeatedly release it for replacements. After exhausted recovery the
  Justiciar records `enact run block <epic> --exhausted --reason "<exact
  missing access or decision>"`; the unfinished beads and work stay intact.
- **Emergency** (the Beads database is gone or corrupt, the remote rejects
  every push, disk is full, a core tool is broken for everyone): run
  `enact justiciar request --reason "<what is broken, evidence>"`. Exit 3
  prints the existing holder; add your evidence as a comment on its bead if
  it names one, or `send_message` to its session if it is on this machine.
  Then keep doing whatever useful work remains, or wait: the vizier spawns the
  Justiciar and it may message you. "Tests are broken" is never an emergency.
- **Resumed after an interruption** (usage limit, crash, a Marshal nudge):
  check `bd show <bead>`, `enact status` and your worktree. Continue only if
  you still own the bead and its run is active; otherwise preserve all files
  and end the turn. A shared pause lets already running workers finish, but
  never authorizes a stopped worker to restart. Only explicit user resume
  clears paused or blocked state; never clear it as automatic recovery.
- Never wait for a human, never end your turn mid-task to ask a question, and
  never leave your bead in progress when you stop for good. If you truly
  cannot continue in an active run, comment the exact state and run
  `enact release <bead> --reason "..."`. Paused or blocked runs retain their
  assignments and unfinished acceptance until explicit resume.
