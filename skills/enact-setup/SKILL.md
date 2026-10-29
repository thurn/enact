---
name: enact-setup
description: Set up the Enact orchestration framework on this machine and in
  the current project (skills, Beads, staging branch, .enact.toml, AGENTS.md).
disable-model-invocation: true
---

# Enact setup

Prepare this machine and the current project for `/enact` runs. `ENACT` is
the Enact repository clone: resolve this skill's base directory with
`realpath` and go up two levels (conventionally `~/enact`). The helper is
`python3 "$ENACT/scripts/enact.py"` (`py -3` on Windows), written `enact`
below.

## 1. Machine

1. Check the tools: `git`, Python 3.11+, `bd` (Beads 1.2+), and `dolt`, which
   Enact uses to resolve Beads merge conflicts. Report anything missing with
   install instructions and stop.
2. `git -C "$ENACT" pull --ff-only`.
3. `enact install` links the `enact` and `enact-setup` skills into the
   user-level skill directories that exist (`~/.claude/skills`,
   `~/.codex/skills`, `~/.agents/skills`), using symlinks or, on Windows,
   directory junctions. It refuses to overwrite unrelated entries: if it
   reports a conflict, show it to the user and ask before removing anything.

## 2. Project

Run from the project's main checkout.

1. **Remote.** The project needs an `origin` remote on GitHub (or any git
   host). Enact keeps code on `origin/staging` and Beads data on
   `refs/dolt/data` in the same remote.
2. **Branches.** Find the integration branch (`master` or `main`). Enact
   promotes to it only after the slow checks pass.
3. **Checks.** Find the project's fast checks (about 60 seconds: lint, type
   check, unit tests) and slow checks (the full suite). Look in `AGENTS.md`,
   `README`, `Makefile`, `package.json`, `scripts/` and CI configuration. If
   unclear, ask the user; do not guess. If there is no fast/slow split,
   propose one.
4. **Run** `enact setup --prefix <short beads prefix> --master <branch>
   --fast "<cmd>" --slow "<cmd>"`. It writes `.enact.toml` (if missing),
   initializes Beads (if missing; `bd init` commits `.beads/` on the current
   branch) or bootstraps the local Beads database from the remote on a fresh
   clone, creates `origin/staging` from the integration branch (if missing)
   and pushes the Beads database. On every further machine, cloning and
   running `enact setup` is all the project setup needed.
5. **Worktree hooks.** Ask whether new worktrees need anything special
   (reusing build caches, APFS clones, warm worktree pools). If so, write
   `create`/`remove` hook scripts and add them to `[worktree]` in
   `.enact.toml`. The hooks get `ENACT_WORKTREE_PATH`, `ENACT_BRANCH` (empty
   means detached), `ENACT_BASE_SHA` and `ENACT_REPO`, and fully replace
   `git worktree add`/`remove`.
6. **AGENTS.md.** Add an "Enact" section adapted from
   `$ENACT/templates/AGENTS.md` to the project's conventions and voice. Ask the
   user about anything you are unsure of, such as role restrictions or the
   review policy.
7. **Commit** `.enact.toml`, the tracked `.beads/` files and `AGENTS.md`, then
   deliver with `enact push`, which puts the commit on staging. Slow CI
   promotes it to the integration branch when it passes.

## 3. Verify

- `enact status` succeeds.
- `bd list --json` works.
- `git ls-remote origin staging refs/dolt/data` shows both refs.

Report what you set up, and remind the user that `/enact <plan>` starts a
run. Claude Desktop runs need bypass or auto permission mode so unattended
sessions never stop at a prompt.
