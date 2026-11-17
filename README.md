# Enact

Enact is a minimal agent orchestration framework for **large, unattended
project execution** from a desktop agent app (Claude Desktop, Codex
Desktop). You hand `/enact-plan` a bead, a plan document or a task prompt;
it files the work in [Beads](https://github.com/gastownhall/beads) or the
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

Then run `/enact-setup` in each project. It writes `.enact.toml`:

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

Requirements: Git and Python 3.11+. Install Cargo to build the managed
`tk` client. The Beads backend also uses `bd` 1.2+ in its default embedded
Dolt mode and the `dolt` CLI to resolve Beads merge conflicts.

### Working with tasks

Run `enact backend` in the project. On `beads`, use `bd` and publish
direct edits with `enact sync`. On `server`, use the returned `tk` path.
Ticket writes are immediate. The ticket dashboard and readable `tk` output
show IDs like `Tq44s` and `Tq44s.7`. Pass these IDs, bare IDs, or legacy Beads
IDs to `tk`. Dashboard links, `tk --json`, and `tk export` use the same `T`
prefix. Dashboard descriptions and `tk` reads show resolved ticket references
with the same prefix. Exports preserve the original prose for a faithful
restore. Use `enact claim`, `start`, `meta`, `release` and `close` for ownership
and assignment on both backends.

New server-backed plans file milestone audits directly under the milestone
with `tk create --last`. The server manages readiness and parent closure.
Beads plans use review gate epics, and imported gates retain their edges.
See the [task command reference](skills/enact/roles/common.md#task-backend).

### Local ticket verification

Use Node 22.12+, Cargo and Python 3.11+ from an Enact checkout. The Worker
and CLI tests start local simulators with isolated state. They require no
Cloudflare login.

```sh
npm --prefix server ci
npm --prefix server test
cargo test --locked --manifest-path cli/Cargo.toml
python3 -m unittest -q tests.test_unit
```

With real `bd` and `dolt` on `PATH`, run the tests normally included in
slow CI. These exercise frozen Dolt audit history, both project cutovers,
and Enact coordination and backup/restore with temporary Git remotes.

```sh
cargo test --locked --manifest-path cli/Cargo.toml -- --ignored
```

### Ticket enrollment and cutover

Beads is authoritative for live Beryl and Enact. Both repositories ship
the Worker coordinates as commented configuration. With no active
`[tickets]` table, Enact uses Beads and the optional `bd` shim passes
through to the original executable.

Beryl epic `by-vaexw` tracks production activation. Native Windows
verification (`by-r61mr.38`) gates Worker deployment and fleet enrollment
(`by-r61mr.22`). The operator stages Beryl's import (`by-r61mr.24`), then
switches Beryl (`by-r61mr.25`) and Enact (`by-r61mr.26`) separately.
The legacy publisher and data stay available through the rollout and
retention period. Their removal is tracked in `by-r61mr.29` and
`by-r61mr.31`.

For an enrolled project, configure
`[tickets].project` and `[tickets].url` in the main checkout’s
`.enact.toml`. Use the Worker root URL. All linked worktrees read this same
`[tickets]` table, including worktrees created before enrollment. Other
Enact settings, including check commands, come from the current worktree
when it has an `.enact.toml`.

Use `install` to obtain the managed `tk` path and pass it to
`scripts/install_bd_shim.py TK REAL_BD ENTRY`. Pass the actual Beads
executable and the absolute path of a `bd` entry (or `bd.exe` on Windows)
in a directory ahead of it on `PATH`. The installer saves the original
beside the shim as `bd-real`. Use `ENACT_SHIM_HELPER` and `ENACT_SHIM_PYTHON`
if the shim should run a different Enact helper or Python interpreter
during the pause.

Run `setup` from each enrolled project on each machine with the shim on
`PATH`. Setup posts an authenticated `machine` event after it succeeds.
Use a stable, distinct `ENACT_HOST` per machine if the hostname is
unsuitable. Machine tokens are kept outside the checkout. `tk register`
rotates the shared local token, so run `tk register --ensure` again in the
other enrolled projects after a rotation. If a new registration reaches the
Worker during its one-minute GitHub lookup cooldown, rerun setup after the
cooldown expires.

With the default backend, the shim and Enact both use the project's
cutover state. They use Beads in `before`, wait during `paused`, and use
tickets in `after`. `[tickets] backend = "server"` directs the Enact
helper to use the ticket API immediately. Leave that override unset when
preparing a fleet cutover. Older prompts can keep invoking `bd` through
`PATH` throughout the transition. New prompts use `enact backend` to
choose the active CLI.

Run the migration from the enrolled project's checkout. Start with Beryl
(`by`), then repeat for Enact (`en`). The commands below use the Enact
checkout's `scripts/cutover.py`. Install the dependencies for local replay
with `npm ci` and `npm run pretest` in Enact's `server/` directory.

1. Run `python3 /path/to/enact/scripts/cutover.py preimport` while Beads is
   authoritative. It backs up the local Dolt working set, pulls the remote
   into that private copy, and imports closed tickets first. On exit 5,
   rerun as the UTC write budget becomes available. Each run imports only
   differences and reports its progress.
2. Prepare a Beryl compatibility corpus with `start.jsonl.gz` and
   `calls.jsonl.gz` from the same recording window. Use it for both
   projects. See [recording Beads calls](docs/design.md#recording-beads-calls-for-the-ticket-cutover-2026-11-16).
   The cutover runs the replay locally with Miniflare and requires a corpus
   recorded within three days. Post a `tk event machine '{"enrolled":true}'`
   from each enrolled machine shortly before starting. For Beryl, move viewers
   to the ticket dashboard and stop the Scotty export pollers.
3. Run `python3 /path/to/enact/scripts/cutover.py start --archive NEW_DIRECTORY
   --replay-corpus CORPUS --machine HOST1 --machine HOST2 --machine HOST3`.
   For Beryl, add `--scotty-stopped`. Idle peers can run `tk drain` from
   their enrolled checkout during the pause. Normal shim or Enact commands
   also drain on first observing the pause. The command gives the fleet and
   final checks up to five minutes, then returns the project to Beads if
   it cannot switch. If the operator process is interrupted and leaves a
   pause active, run `cutover.py abort --archive DIRECTORY` with the same
   archive. The archive identifies that specific pause.
4. After a successful switch, keep the archive and verify the shim, Enact
   and dashboard against the ticket server. One hour after the pause began,
   run `cutover.py reconcile --archive DIRECTORY`. It pulls Dolt again
   and imports only changes since the archived comparison. A conflict
   names the affected tickets and leaves the late snapshot in the archive
   for reconciliation. Keep legacy source data for the rollout retention
   window.

## Helper commands

`python3 scripts/enact.py <command>` (`py -3` on Windows). Each prints JSON.

| Command | Purpose |
| --- | --- |
| `next --epic E --parallel N --wait S` | Vizier step: reserve and spawn, archive, wait, done, deadlock |
| `start BEAD` | Session takes over its reserved bead |
| `worktree create\|remove NAME` | Worktree from `origin/staging` (hookable) |
| `worktree prune` | Delete dead worktrees (slow CI does this on each run) |
| `push [--bead B]` | Rebase, merge diverged master, fast checks, push to staging, start CI |
| `close BEAD --reason R [--continue]` | Close the task and any finished epics. With `--continue`, claim the next task in the lane |
| `brief BEAD` | One-read working context: bead, epics, dependency handoffs, lane |
| `claim BEAD`, `meta BEAD k=v`, `release BEAD` | Ownership and assignment bookkeeping on either backend |
| `backend` | Report `beads` or `server`, with the managed `tk` path on `server` |
| `sync` | Commit, merge and push Beads, with remote values winning conflicts. Ticket writes need no sync |
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
