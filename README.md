# Enact

Enact is a minimal agent orchestration framework for **large, unattended
project execution** from a desktop agent app (Claude Desktop, Codex
Desktop). `/enact-intake` audits a plan with you and files it as a
[Beads](https://github.com/gastownhall/beads) graph; `/enact` then runs every
bead in a fresh top-level session for days, across machines and model
providers, synchronized through one GitHub remote.

Enact has no state of its own beyond the bead graph, no server and no CI
dependency: three skills, eight short role prompts and a stdlib Python
helper.

## Layout

| Path | What |
| --- | --- |
| `skills/enact-intake/SKILL.md` | Plan audit and bead filing: `/enact-intake <plan>` |
| `skills/enact/SKILL.md` | The vizier (orchestrator) skill: `/enact <epic>` |
| `skills/enact/roles/*.md` | Role prompts for spawned sessions |
| `skills/enact-setup/SKILL.md` | Machine and project setup: `/enact-setup` |
| `scripts/enact.py` | Helper: claims, sync, push, slow CI, locks |
| `templates/AGENTS.md` | Sample AGENTS.md section |
| `docs/design.md` | Design decisions and rationale |

## Roles

| Role | Emoji | Started by | Job |
| --- | --- | --- | --- |
| Vizier | 🔮 | the user (`/enact <epic>`) | Only spawns, archives and waits |
| Executor | ⚒️ | bead | Implements a bead and pushes it to staging |
| Warden | 🛡️ | bead | Fresh review and fixes of pushed work |
| Steward | 🚒 | slow-CI failure bead | Makes slow CI green again |
| Mason | 🧱 | bead (milestone gate) | Architecture audit, files beads |
| Sage | 📖 | bead (milestone gate) | Process and tooling audit, files beads |
| Justiciar | 🔥 | `enact justiciar request` | Emergency authority, one per fleet |
| Marshal | 🎖️ | hourly scheduled task | Nudges stalled sessions, recovers work |

## How a run works

1. `/enact-intake <plan>` audits the plan for anything that would need a
   human, resolves those points with you, and files the graph (an epic,
   milestones, role-labeled beads, Warden beads and Mason/Sage gate epics).
   It ends by printing the `/enact <epic>` line to run.
2. `/enact <epic> [parallelism]` makes the session the vizier. It asks
   nothing: it checks the permission mode, creates the hourly Marshal task,
   then loops on `enact next --wait 540` inside the turn you started.
   Claude Desktop only lets a user-started turn start sessions. Each loop
   reserves the highest-priority ready bead, starts a session for it, records
   the session ID and archives sessions whose beads have closed.
3. Each session runs `enact start`, works in a worktree cut from
   `origin/staging`, delivers with `enact push` (rebase, fast checks, push to
   staging), files follow-up beads and closes its bead.
4. `enact push` starts a coalescing slow-CI runner on the machine. A pass
   fast-forwards `master`; a failure files one P0 Steward bead per OS.

## Setup

```sh
git clone git@github.com:thurn/enact.git ~/enact
python3 ~/enact/scripts/enact.py install
```

Then run `/enact-setup` in each project. It writes `.enact.toml`:

```toml
master_branch = "master"
[checks]
fast = "scripts/check-fast"
slow = "scripts/check"
[worktree]
root = "../{repo}-worktrees"
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
| `ci [--detach\|--status]` | Coalescing slow-CI runner |
| `justiciar status\|request\|acquire\|release\|break` | Fleet-wide emergency lock |
| `status`, `lock acquire\|release NAME` | Marshal support |
| `install`, `setup` | Machine and project setup |

## Status

The Claude Desktop flow was exercised end to end. The Codex Desktop
instructions follow its documented tools but have not been run.
