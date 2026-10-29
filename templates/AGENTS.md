## Enact

- This project can run large plans unattended with Enact (`/enact <plan>`);
  most work does not go through it. Fast and slow check commands are in
  `.enact.toml`.
- Never push straight to `master`. Deliver with
  `python3 ~/enact/scripts/enact.py push`, which rebases onto
  `origin/staging`, runs the fast checks, pushes to `staging` and starts slow
  CI. `master` advances only when slow CI passes.
- Work in a worktree cut from `origin/staging`
  (`python3 ~/enact/scripts/enact.py worktree create <name>`), not in the main
  checkout.
- Beads live in this repo (`bd`); publish bead changes with
  `python3 ~/enact/scripts/enact.py sync`.
- Enact roles in use: all (Executor, Warden, Steward, Mason, Sage).
- Review policy: one Warden bead per substantial implementation bead.
