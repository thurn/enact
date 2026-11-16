---
name: enact-setup
description: Set up the Enact orchestration framework on this machine and in
  the current project (skills, task backend, staging branch, .enact.toml,
  AGENTS.md).
disable-model-invocation: true
---

# Enact setup

Prepare this machine and the current project for `/enact` and `/enact-plan`
runs. `ENACT` is the Enact repository clone: resolve this skill's base
directory with `realpath` and go up two levels (conventionally `~/enact`).
The helper is `python3 "$ENACT/scripts/enact.py"` (`py -3` on Windows),
written `enact` below.

## 1. Machine

1. Check for `git`, Python 3.11+ and Cargo. The installer builds the
   managed `tk` client. Check for `bd` (Beads 1.2+) and `dolt` if the
   project uses Beads, including an enrolled project before cutover.
   If the project is already configured for tickets, see the
   [enrollment instructions](../../README.md#ticket-enrollment-and-cutover).
2. `git -C "$ENACT" pull --ff-only`.
3. `enact install` links `enact`, `enact-plan`, `enact-setup` and
   `enact-sweep` into `~/.agents/skills` (created even on a
   fresh machine) and Claude's skills directory when its config directory exists.
   It uses symlinks or Windows directory junctions, removes this checkout's
   standalone legacy Codex links, and preserves shared Claude directories
   and unrelated entries. `CODEX_HOME` and `CLAUDE_CONFIG_DIR` are honored.
   It refuses to overwrite unrelated entries: if it reports a conflict,
   show it to the user and ask before removing anything. Verify each skill
   appears once in the intended app's selector after discovery refresh.

## 2. Project

Run from the project's main checkout.

1. **Remote.** The project needs a Git remote for code. With the default
   configuration that is `origin`, where Enact uses `origin/staging`.
   Beads also uses `refs/dolt/data` there. Ticket enrollment uses the
   GitHub remote configured for the ticket project.
2. **Branches.** Find the integration branch (`master` or `main`). Enact
   promotes to it only after the slow checks pass.
3. **Checks.** Find the project's fast checks (about 60 seconds: lint, type
   check, unit tests) and slow checks (the full suite). Look in `AGENTS.md`,
   `README`, `Makefile`, `package.json`, `scripts/` and CI configuration. If
   unclear, ask the user; do not guess. If there is no fast/slow split,
   propose one.
4. **Run** `enact setup --master <branch> --fast "<cmd>" --slow "<cmd>"`.
   If initializing Beads, add `--prefix <short beads prefix>`. Setup
   writes `.enact.toml` if missing, ignores the worktree root and the
   Beads audit log, and creates `origin/staging` if missing.
   - **Beads:** Setup initializes or bootstraps the local database and
     syncs it when it has a remote. `bd init` can commit the tracked
     `.beads/` files on the current branch.
   - **Enrolled project:** Setup registers the machine token against the
     configured remote and posts a machine event. If `enact backend`
     reports `server`, setup needs no local Beads database. Before cutover,
     setup still initializes or bootstraps Beads.
5. **Worktree hooks.** Ask whether new worktrees need anything special
   (reusing build caches, APFS clones, warm worktree pools). If so, write
   `create`/`remove` hook scripts and add them to `[worktree]` in
   `.enact.toml`. The hooks get `ENACT_WORKTREE_PATH`, `ENACT_BRANCH` (empty
   means detached), `ENACT_BASE_SHA` and `ENACT_REPO`, and fully replace
   `git worktree add`/`remove`. Create must still leave a git worktree of
   this repository at `ENACT_WORKTREE_PATH`: slow CI's sweep deletes other
   directories under the root unless they are hidden or hold worktrees.
6. **AGENTS.md.** Add an "Enact" section adapted from
   `$ENACT/templates/AGENTS.md` to the project's conventions and voice. Its
   rule that **every change**, not only Enact runs, goes through
   `enact worktree create`, a commit in the worktree and `enact push` (and so
   through fast checks and slow CI) is required: keep it prominent, and
   replace any existing instruction to commit or push to the main branch
   directly. Ask the user about anything you are unsure of, such as role
   restrictions or the review policy.
7. **Commit** the setup changes to `.enact.toml`, `.gitignore` and
   `AGENTS.md`, plus any tracked `.beads/` changes from Beads setup.
   Deliver with `enact push`, which puts the commit on staging. Slow CI
   promotes it to the integration branch when it passes.

## 3. Verify

- `enact status` succeeds.
- The configured remote has the staging branch.
- Run `enact backend`. On `server`, use the returned executable to run
  `tk list --json`. On `beads`, verify `bd list --json` and
  `git ls-remote origin refs/dolt/data` (use the configured remote).

Report what you set up, and remind the user that `/enact` runs a bead, an
epic or a task prompt inline in one session, and `/enact-plan` with a bead,
a plan document or a task prompt starts an unattended multi-session run.
Claude Desktop `/enact-plan` runs need bypass or auto permission mode so
unattended sessions never stop at a prompt.
