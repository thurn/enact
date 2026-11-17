# Enact

Enact is a minimal agent orchestration framework for **large, unattended
project execution** from a desktop agent app (Claude Desktop, Codex
Desktop). You hand `/enact-plan` a bead, a plan document or a task prompt;
it files the work in the
Enact ticket server and runs each task in a fresh top-level session across
machines and model providers. Code goes through a shared Git remote.

To clear a backlog, `/enact-sweep` finds stale beads (open work no live
run owns), closes the ones that shipped or no longer matter, and runs the
rest as a vizier in the same turn.

For smaller work, `/enact` runs a bead, an epic or a task prompt inside the
current session with the same roles, using subagents for an epic's beads,
and waits for slow CI before it finishes.

Enact has four skills, role prompts and a stdlib Python helper. The helper
runs checks and promotes green staging commits. The optional ticket server
stores the task graph and coordinates claims and leases.

## Layout

| Path | What |
| --- | --- |
| `skills/enact-plan/SKILL.md` | The vizier (orchestrator) skill: `/enact-plan <bead\|plan\|task>` |
| `skills/enact/SKILL.md` | Inline execution in one session: `/enact <bead\|epic\|task>` |
| `skills/enact/roles/*.md` | Role prompts for spawned sessions and subagents |
| `skills/enact-setup/SKILL.md` | Machine and project setup: `/enact-setup` |
| `skills/enact-sweep/SKILL.md` | Close stale beads, run the rest: `/enact-sweep` |
| `scripts/enact.py` | Helper: claims, sync, push, slow CI, locks |
| `cli/`, `server/` | Native `tk` client, compatibility shim and ticket Worker |
| `templates/AGENTS.md` | Sample AGENTS.md section |
| `docs/design.md` | Design decisions and rationale |

## Roles

| Role | Emoji | Started by | Job |
| --- | --- | --- | --- |
| Vizier | 🔮 | the user (`/enact-plan`) | Plans, then only spawns, archives and waits |
| Executor | ⚒️ | bead | Implements a bead and pushes it to staging |
| Warden | 🛡️ | bead | Fresh review and fixes of pushed work |
| Steward | 🚒 | slow-CI failure bead | Makes slow CI green again |
| Mason | 🧱 | bead (milestone gate) | Architecture audit, files beads |
| Sage | 📖 | bead (milestone gate) | Process and tooling audit, files beads |
| Archivist | 📜 | bead (end of a plan run) | Retires the plan, updates docs to the shipped state |
| Justiciar | 🔥 | `enact justiciar request` | Emergency authority, one per fleet |

## How a run works

1. `/enact-plan <input> [parallelism]` makes the session the vizier. It checks
   the permission mode and plans according to the input:
   - a **bead** runs as is (a plain bead is wrapped in an epic of its own);
   - a **plan document** is audited for anything that would need a human,
     those points are resolved with you, and it is filed as a full graph (an
     epic, milestones, role-labeled beads, Warden beads, Mason/Sage gate
     epics and a final Archivist bead);
   - a **task prompt** becomes one executor bead, reviewed by nobody, by a
     cold-review subagent inside the executor session, or by a Warden bead
     for large or risky changes, unless it describes a large work item.
2. The vizier then loops on `enact next --wait 540` inside the turn you
   started. Claude Desktop only lets a user-started turn start sessions. Each
   loop reserves the highest-priority ready bead, starts a session for it,
   records the session ID and archives sessions whose beads have closed.
3. Each session runs `enact start`, works in a worktree cut from
   `origin/staging`, delivers with `enact push` (rebase, fast checks, push to
   staging), files essential follow-up beads and closes its bead.
4. `enact push` starts a coalescing slow-CI runner on the machine. A pass
   fast-forwards `master`; a failure files one P0 Steward bead per OS,
   which viziers on that OS get first.
   Local `staging` and `master` are fast-forwarded to track the remote.
   A push outside any run, with no active vizier to pick up a Steward bead,
   prints `watch_ci`: that session waits with `enact ci --wait` and repairs
   a failure itself.

## Inline runs

`/enact <input> [parallelism]` never starts another session or a
Justiciar. A plain bead or task prompt (filed as one executor bead) is
implemented by the session itself; a large or risky change then gets a
Warden subagent. An epic runs each ready bead in a role subagent, one at a
time by default. Mason, Sage and Archivist run only when the epic already
contains their beads. The session retitles itself with the working role's
emoji and waits for its last delivery to reach `master`, running a Steward
subagent on failure. Plan documents go to `/enact-plan`.

## Setup

```sh
git clone git@github.com:thurn/enact.git ~/enact
python3 ~/enact/scripts/enact.py install
```

Installation creates the canonical Codex directory `~/.agents/skills` and
links Claude skills when its configuration directory exists. It safely
migrates this checkout's standalone legacy Codex links and preserves shared
Claude roots and unrelated content.

Install Rust with Cargo on `PATH` before running `install`. Enact builds
`tk` in the machine's user cache and reports the executable's path in its
JSON output. Ticket commands rebuild it when the CLI sources change.
Set `ENACT_TK` to an absolute executable path to use a prebuilt client.
For projects configured with `[tickets]`, `setup` runs `tk register --ensure`
against the configured Git remote. The first enrollment creates a machine
token. Enrollment in other projects registers the same token's hash in
each project's remote.

Configure `.enact.toml` before running `/enact-setup` in each project:

```toml
master_branch = "master"
[checks]
fast = "scripts/check-fast"
slow = "scripts/check"
# slow_timeout = 3600   # seconds before the runner kills a hung slow check
# slow_min_gap = 600    # least seconds between one slow run ending and the next starting
# slow_tree = "ci-runner"   # reuse one slow-CI worktree, keeping build directories
[worktree]
root = ".worktrees"   # inside the checkout; gitignored
# create = "scripts/enact-worktree create"   # full replacement hooks
# remove = "scripts/enact-worktree remove"
# slots = 20   # reuse slot-01..slot-20 for worktrees, keeping build directories
# [push]
# queue = true   # check in the push's tree, then land batches through a merge queue
# check_trees = 3   # run each push's fast check in reused check-1..check-3 trees
[roles.warden.claude]
effort = "max"
[tiers.light.claude]  # beads labeled tier:light (mechanical work)
model = "sonnet"
# tier:fable, tier:opus and tier:sonnet are built in for Claude: no entry
```

Requirements are Git, Python 3.11+, and Cargo for the managed `tk` client.

### Working with tickets

Configure `[tickets].project` and `[tickets].url` in the main checkout's
`.enact.toml`. Use the Worker root URL. Linked worktrees inherit the project
and URL and keep their own check commands. Run `enact setup` from each project
to register this machine. Run `enact backend` for the managed `tk` path.

Ticket writes publish immediately. Use `enact claim`, `start`, `meta`,
`release` and `close` for assignment and ownership. The CLI and dashboard
show IDs like `Tq44s.7`. Bare IDs and migrated IDs remain valid aliases.
See the [task commands](skills/enact/roles/common.md#task-backend).

### Local ticket verification

Use Node 22.12+, Cargo and Python 3.11+ from an Enact checkout. The Worker
and CLI tests use isolated simulators and require no Cloudflare login.

```sh
npm --prefix server ci
npm --prefix server test
cargo test --locked --manifest-path cli/Cargo.toml -- --include-ignored
python3 -m unittest -q tests.test_unit
```

## Helper commands

`python3 scripts/enact.py <command>` (`py -3` on Windows). Each prints JSON.

| Command | Purpose |
| --- | --- |
| `next --epic E --parallel N --wait S` | Vizier step: reserve and spawn, archive, wait, done, deadlock |
| `start BEAD` | Session takes over its reserved bead |
| `worktree create\|remove NAME` | Worktree from `origin/staging` (hookable) |
| `worktree prune` | Delete dead worktrees (slow CI does this on each run) |
| `push [--ticket B]` | Rebase, merge diverged master, fast checks, push to staging, start CI |
| `close BEAD --reason R [--continue]` | Close the task and any finished epics. With `--continue`, claim the next task in the lane |
| `brief BEAD` | One-read working context: bead, epics, dependency handoffs, lane |
| `claim BEAD`, `meta BEAD k=v`, `release BEAD` | Ownership and assignment bookkeeping |
| `backend` | Report the managed `tk` path |
| `ci [--detach\|--status\|--wait SHA]` | Coalescing slow-CI runner; watch one delivery |
| `justiciar status\|request\|acquire\|release\|break` | Per-machine emergency lock or lease |
| `status` | This machine's runs, workers, CI and deliveries |
| `install`, `setup` | Machine and project setup |

## Status

The Claude Desktop flow was exercised end to end. Bounded Codex Desktop
smokes verified local chat creation, identity, access, continuation and
archiving. See [design notes](docs/design.md#codex-desktop-smoke-receipts)
for receipts and limits. Other operating systems are unverified; final run
cleanup follows green delivery.
