# Enact: recovery and emergencies

Read this only when `common.md` sends you here: you took over someone's
worktree, cannot meet your acceptance, hit an emergency, or were resumed
after an interruption. In an inline run (`common.md`), wherever this file
says to request a Justiciar or message a session, stop instead and put the
evidence in your summary.

## Taking over a worktree

`enact worktree create` printed `recovered: true`: the prior owner
explicitly released, or a routed lookup confirmed it inactive. Inspect that
tree's commits and edits against current staging; preserve useful work and
finish it. This is the authorized local handoff. Remote replacements start
at the latest pushed state; `enact_recovery` retains the original host,
path, branch, head and chat route. Never delete that original work or edit
an unassigned tree.

## When things go wrong

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
- **Resumed after an interruption** (usage limit, crash, a user's nudge):
  check `bd show <bead>`, `enact status` and your worktree. Continue only if
  you still own the bead and its run is active; otherwise preserve all files
  and end the turn (⛔). A shared pause lets already running workers finish,
  but never authorizes a stopped worker to restart. Only explicit user
  resume clears paused or blocked state; never clear it as automatic
  recovery.
