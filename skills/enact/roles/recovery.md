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
finish it. This is the authorized local handoff. On another host, the
new worktree starts from the commits the release pushed to `enact/<bead>`
(`recovered: true` again), or from staging if it pushed none. Uncommitted
edits never travel: `enact_recovery` records `uncommitted` when some stayed
behind, with the original host, path, branch, head and chat route. Never
delete that original work or edit an unassigned tree.

## When things go wrong

- **Only another machine can do or check it** (the failure came from that
  host or needs its OS, a device or its local logs): apply the
  `common.md` default for it. That is not impossible acceptance.
- **Genuinely impossible acceptance** (access or a decision only the user
  has): preserve the open requirement and request Justiciar recovery with
  the evidence. Do not close it as delivered or repeatedly release it for
  replacements. The Justiciar drops the bead from the run, open and intact,
  so the rest of the run goes on (`justiciar.md`).
- **Emergency** (the task store is gone or corrupt, the remote rejects
  every push, disk is full, a core tool is broken for everyone): run
  `enact justiciar request --reason "<what is broken, evidence>"`. Exit 3
  prints the existing holder; add your evidence as a comment on its bead if
  it names one, or `send_message` to its session if it is on this machine.
  Then keep doing whatever useful work remains, or wait: the vizier starts the
  Justiciar and it may message you. "Tests are broken" is never an emergency.
- **Resumed after an interruption** (usage limit, crash, a user's nudge):
  read the bead with the active CLI, then check `enact status` and your
  worktree. Continue only if you still own the bead and its run is active.
  Otherwise preserve all files
  and end the turn (⛔). A shared pause lets already running workers finish,
  but never authorizes a stopped worker to restart. Only explicit user
  resume clears paused or blocked state; never clear it as automatic
  recovery.
