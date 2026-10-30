## Enact

`enact` below means `python3 ~/enact/scripts/enact.py` (`py -3` on Windows).

- **Every change goes through a worktree and CI**, whether it is made by a
  human, an ad hoc agent session or an Enact run:
  1. From the main checkout, `enact worktree create <short-name>`. Work only
     in the printed path; never edit or commit in the main checkout.
  2. Commit there, following this project's commit conventions.
  3. Run `enact push` in the worktree. It rebases onto `origin/staging`
     (merging `master` if someone bypassed staging), runs the fast checks,
     pushes to `staging`, starts slow CI in the background and removes the
     worktree. On exit code 2, fix the reported conflict or failing check in
     the same worktree and run it again.
  4. If its output has `watch_ci`, no Enact run is around to repair a
     slow-CI failure: follow it (`enact ci --wait <sha>` until it passes;
     on failure, claim and fix the Steward bead) before finishing.
- Never commit to, or push directly to, `master` or `staging`. `master`
  advances only when slow CI passes on `staging`; a slow-CI failure files a
  P0 Steward bead. `enact ci --status` shows the runner and its log. Enact
  fast-forwards local `staging` and `master` to match the remote, so keep
  the main checkout clean.
- Fast and slow check commands are in `.enact.toml`.
- Beads live in this repo (`bd`); publish bead changes with `enact sync`.
- Work can run unattended with Enact (`/enact <bead|plan|task>`); most work
  does not go through it.
- Enact roles in use: all (Executor, Warden, Steward, Mason, Sage, Archivist).
- Review policy: one Warden bead per substantial implementation bead.
