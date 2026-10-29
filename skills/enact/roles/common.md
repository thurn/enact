# Enact: common protocol for spawned sessions

You are one session in an unattended Enact run. A vizier started you for one
bead, and your role, given in your prompt, is fixed for the whole session.
Nobody will answer questions: decide, act and record your reasoning on the
bead. Enact roles are prompt files, not skills; never invoke a skill named
after a role.

`enact` means the helper command given in your prompt. Each subcommand prints
one JSON object. Exit code 2 means you must act (resolve conflicts, fix
checks); 3 means you lost a race.

## Start

1. Run `enact start <bead>` from the project's main checkout. It moves the
   bead from the vizier's reservation to your session ID and prints the bead.
   Exit 3 means it belongs to someone else: report that and end your turn.
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
  the printed `path`. It is cut from the newest `origin/staging`. Never edit
  the main checkout, and never touch another session's worktree.
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

- Use raw `bd ... --json` for reads and writes, then run `enact sync` to
  publish them to the Dolt remote. `enact claim` and `enact close` sync
  themselves. If `sync` reports dropped local values, another machine changed
  the same bead first; reapply your change only if it still makes sense.
- Record decisions, surprises and deviations as comments on your bead
  (`bd comments add <bead> "..."`).

## Follow-up beads

Before closing, always ask: **did I discover issues that merit a follow-up
bead?** Bugs outside your scope, missing tests, tech debt, a design problem,
slow tooling, a misleading doc. File each one rather than ignoring it or
quietly expanding your scope:

```sh
bd create --title "..." --description "..." --acceptance "..." \
  --priority 2 --parent <epic> --labels role:<role> --json
```

Use the run's top epic, or the relevant milestone epic, as the parent so the
run picks it up. Choose the role that should do it (`executor` by default).
Add `bd dep add <new> <prerequisite>` edges only for real dependencies. File
everything before you close your own bead, then run `enact sync`.

## Finish

1. Make sure every change is pushed (`enact push`) and follow-ups are filed.
   If you made no code change, remove your worktree with
   `enact worktree remove <bead>`.
2. `enact close <bead> --reason "<one-line outcome>"`.
3. Retitle your session `✅ [<bead>] <same summary>`.
4. End your turn with a short summary: what changed, surprises, follow-ups
   filed. Do not archive yourself; the vizier does that.

## When things go wrong

- **Ordinary difficulty is not a blocker.** Failing tests, confusing code,
  merge conflicts and flaky checks are your job. Keep working.
- **Genuinely impossible bead** (contradicts the code, depends on something
  that does not exist): file replacement beads that describe the real work,
  comment why, then close with `--reason "replaced by ..."`.
- **Emergency** (the Beads database is gone or corrupt, the remote rejects
  every push, disk is full, a core tool is broken for everyone): run
  `enact justiciar request --reason "<what is broken, evidence>"`. Exit 3
  prints the existing holder; add your evidence as a comment on its bead if
  it names one, or `send_message` to its session if it is on this machine.
  Then keep doing whatever useful work remains, or wait: the vizier spawns the
  Justiciar and it may message you. "Tests are broken" is never an emergency.
- **Resumed after an interruption** (usage limit, crash, a Marshal nudge):
  check `bd show <bead>` and `git status` in your worktree, then continue
  where you left off.
- Never wait for a human, never end your turn mid-task to ask a question, and
  never leave your bead in progress when you stop for good. If you truly
  cannot continue, comment the exact state on the bead and run
  `enact release <bead> --reason "..."` so another session can pick it up.
