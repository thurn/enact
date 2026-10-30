# Enact

Enact is a minimal agent orchestration framework for **large, unattended
project execution** from a desktop agent app (Claude Desktop, Codex
Desktop). You hand `/enact` a bead, a plan document or a task prompt; it
files what it needs as a [Beads](https://github.com/gastownhall/beads) graph
and runs every bead in a fresh top-level session for days, across machines
and model providers, synchronized through one GitHub remote.

Enact has no state of its own beyond the bead graph, no server and no CI
dependency: two skills, nine short role prompts and a stdlib Python helper.

## Layout

| Path | What |
| --- | --- |
| `skills/enact/SKILL.md` | The vizier (orchestrator) skill: `/enact <bead\|plan\|task>` |
| `skills/enact/roles/*.md` | Role prompts for spawned sessions |
| `skills/enact-setup/SKILL.md` | Machine and project setup: `/enact-setup` |
| `scripts/enact.py` | Helper: claims, sync, push, slow CI, locks |
| `templates/AGENTS.md` | Sample AGENTS.md section |
| `docs/design.md` | Design decisions and rationale |

## Roles

| Role | Emoji | Started by | Job |
| --- | --- | --- | --- |
| Vizier | 🔮 | the user (`/enact`) | Plans, then only spawns, archives and waits |
| Executor | ⚒️ | bead | Implements a bead and pushes it to staging |
| Warden | 🛡️ | bead | Fresh review and fixes of pushed work |
| Steward | 🚒 | slow-CI failure bead | Makes slow CI green again |
| Mason | 🧱 | bead (milestone gate) | Architecture audit, files beads |
| Sage | 📖 | bead (milestone gate) | Process and tooling audit, files beads |
| Archivist | 📜 | bead (end of a plan run) | Retires the plan, updates docs to the shipped state |
| Justiciar | 🔥 | `enact justiciar request` | Emergency authority, one per fleet |
| Marshal | 🎖️ | hourly scheduled task | Nudges stalled sessions, recovers work |

## How a run works

1. `/enact <input> [parallelism]` makes the session the vizier. It checks
   the permission mode, plans according to the input and, unless the run is
   a small task prompt, creates the hourly Marshal task:
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
   fast-forwards `master`; a failure files one P0 Steward bead per OS.
   Local `staging` and `master` are fast-forwarded to track the remote.
   A push outside any run, with no active vizier to pick up a Steward bead,
   prints `watch_ci`: that session waits with `enact ci --wait` and repairs
   a failure itself.

## Setup

```sh
git clone git@github.com:thurn/enact.git ~/enact
python3 ~/enact/scripts/enact.py install
```

Installation creates the canonical Codex directory `~/.agents/skills` and
links Claude skills when its configuration directory exists. It safely
migrates this checkout's standalone legacy Codex links and preserves shared
Claude roots and unrelated content.

Then run `/enact-setup` in each project. It writes `.enact.toml`:

```toml
master_branch = "master"
[checks]
fast = "scripts/check-fast"
slow = "scripts/check"
[worktree]
root = ".worktrees"   # inside the checkout; gitignored
# create = "scripts/enact-worktree create"   # full replacement hooks
# remove = "scripts/enact-worktree remove"
[roles.warden.claude]
effort = "max"
```

Requirements: git, Python 3.11+, Beads (`bd`) 1.2+ in its default embedded
Dolt mode, and the `dolt` CLI (used to resolve Beads merge conflicts).

## Helper commands

`python3 scripts/enact.py <command>` (`py -3` on Windows). Each prints JSON.

| Command | Purpose |
| --- | --- |
| `next --epic E --parallel N --wait S` | Vizier step: reserve and spawn, archive, wait, done, deadlock |
| `start BEAD` | Session takes over its reserved bead |
| `worktree create\|remove NAME` | Worktree from `origin/staging` (hookable) |
| `push [--bead B]` | Rebase, merge diverged master, fast checks, push to staging, start CI |
| `close BEAD --reason R` | Close, auto-close finished epics, sync |
| `claim BEAD`, `meta BEAD k=v`, `release BEAD` | Bead bookkeeping (always synced) |
| `sync` | Commit, merge (remote wins on conflict) and push Beads |
| `ci [--detach\|--status\|--wait SHA]` | Coalescing slow-CI runner; watch one delivery |
| `justiciar status\|request\|acquire\|release\|break` | Fleet-wide emergency lock |
| `status`, `lock acquire\|release NAME` | Marshal support |
| `install`, `setup` | Machine and project setup |

## Status

The Claude Desktop flow was exercised end to end. Bounded Codex Desktop
smokes verified local chat creation, identity, access, continuation and
archiving, paused automation creation/update/deletion, and actual scheduled
Marshal delivery after recovery. See
[design notes](docs/design.md#codex-desktop-smoke-receipts) for receipts and
limits. Other operating systems and indefinite scheduling persistence are
unverified; final run cleanup follows green delivery.
