# Enact design decisions

Outcome of the design interview (2026-10-28). The original brief is the source
of truth for role definitions; this file records the decisions made on top.

## Git flow

- `staging` is a branch on origin. Worktrees are cut from `origin/staging`.
- `enact push` (used by executors *and* humans, replacing `git push origin
  master`): rebase onto `origin/staging`, merge `origin/master` if it is not an
  ancestor (backstop for direct-to-master pushes), run fast checks,
  fast-forward push `HEAD:staging`, retry on rejection, start the slow-CI
  runner, remove the worktree.
- `enact ci --detach`: one coalescing runner per machine (PID lock in the git
  common dir). Loop: fetch, test the newest `origin/staging` tip, repeat until
  the tip equals the last tested SHA; re-check after releasing the lock. No
  debounce.
  - Pass: fast-forward `origin/master` to the tested SHA (master = slow CI green).
  - Fail: at most one open P0 Steward bead per project per OS, unparented;
    later failures add a comment with the new range and log excerpt.
- Machines run slow CI independently; any machine's pass advances master.
- Executors keep pushing while staging is red.

## Beads

- Embedded Dolt mode (verified: works from linked worktrees and under
  concurrent writes). Remote is origin `refs/dolt/data`.
- `enact sync`: dolt commit, pull, push, retry. On a true conflict the remote
  wins and the helper prints the dropped local values.
- `enact claim`: pull, verify open+unassigned, claim, push; the loser of a
  cross-machine race backs off. `enact close` records `enact_commits`, closes,
  syncs.
- Role is a label `role:<name>`; unlabeled beads are executor beads.
- Targeting metadata: `enact_host=<hostname>` / `enact_provider=claude|codex`
  restricts which vizier may pick a bead (this is the "mailbox").
- Bookkeeping metadata: `enact_vizier`, `enact_session`, `enact_archived`,
  `enact_commits`.

## Vizier

- Split into two user-invoked skills: `/enact-intake <plan>` (the stoppage
  audit with the user, decomposition, bead filing; prints the `/enact` line)
  and `/enact <epic> [parallelism]` (runtime checks, then the loop; never asks
  the user anything). Several viziers may run `/enact` on one epic.
- Intake: audit stoppage risks and resolve each with the user up front;
  refuse to file if any remain. `/enact` runtime checks (per machine, no
  questions): `git pull --ff-only` in ~/enact, project set up, non-prompting
  permission mode, create the Marshal routine if missing.
- Decomposition: full graph up front or rolling milestones, at the vizier's
  discretion; "decompose milestone N" beads go to the executor role. Agents
  re-validate stale beads against current code.
- Warden default: one Warden bead per substantial implementation bead (small
  related beads batched); code-dependent work depends on the Warden bead.
- Milestones are child epics. Mason/Sage audits are "gate" epics blocking the
  next milestone epic (beads only lets epics block epics; a blocked epic hides
  its children from `bd ready`). Important findings become children of the
  gate; `enact wait` runs `bd epic close-eligible`.
- Loop runs inside the user's original turn (Claude Desktop refuses
  `start_session` on turns started by automated events or other sessions):
  `enact next` (claims as `vizier:<host>`, returns a bead or "wait"), spawn
  `start_session`/`create_thread`, record `enact_session`, then foreground
  `enact wait` (≤ ~9 min) and repeat. Wait output restates the loop so it
  survives compaction.
- Selection scope: ready descendants of the epic plus unparented Steward beads.
  Capacity = open beads with `enact_vizier=<me>`; default parallelism 1.
- The vizier archives sessions of closed beads reported by `enact wait`.
- All spawns go through the vizier, including the Justiciar
  (`enact justiciar request`); the requesting host's vizier spawns it.
- Termination: epic closed and nothing in flight = final report. Open but
  nothing ready and peers hold claims = keep waiting. Open, nothing ready,
  nobody holds claims (or no change for 6h) = request a Justiciar.
- Spawn prompt names `roles/common.md` and `roles/<role>.md` by absolute path,
  plus the bead and epic. Optional per-role model/effort from `.enact.toml`.

## Justiciar

- Fleet-wide, per-project lock: `refs/enact/justiciar` on origin, created with
  a create-only push; the commit records host, session, time, reason. Holder
  is printed to anyone who tries to acquire. Deleted on exit; the Marshal
  breaks it if the holder is archived or silent for more than 2h.

## Marshal

- One per project per machine per host app: an hourly Claude Desktop scheduled
  task or Codex automation, with a lockfile against overlapping sweeps.
- Nudges stalled sessions, releases beads whose sessions are gone, archives
  leftovers, restarts a dead CI runner, breaks stale Justiciar locks, pulls
  ~/enact, and push-notifies the user if the vizier's turn has died (it cannot
  respawn one on Claude Desktop).
- The vizier deletes the Marshal task when its run finishes, unless
  `enact status` still shows working beads on this machine (another epic in
  flight), in which case that run's vizier deletes it later.

## Sage

- May edit ~/enact directly and push to master.

## Configuration

- `.enact.toml`: fast/slow check commands, `[worktree]` root plus optional
  full-replacement `create`/`remove` hooks (env: `ENACT_WORKTREE_PATH`,
  `ENACT_BRANCH`, `ENACT_BASE_SHA`, `ENACT_REPO`), optional
  `[roles.<role>]` model/effort per host.
- AGENTS.md: agent-facing prose bullets (enabled roles, review policy, how to
  use `enact push`).

## Packaging

- One stdlib-only `scripts/enact.py` (Python 3.11+), subcommands `push ci
  claim close sync next wait worktree justiciar install setup`.
- Skills linked per machine into user-level skill dirs (symlinks; junctions on
  Windows). `enact-setup` per project: beads init, dolt remote, staging
  branch, `.enact.toml`, AGENTS.md bullets.
- Titles: 🔮 Vizier, ⚒️ Executor, 🛡️ Warden, 🔥 Justiciar, 🚒 Steward,
  🎖️ Marshal, 🧱 Mason, 📖 Sage; ✅ on completion.

## Implementation notes

Findings and adjustments made while implementing (2026-10-28):

- Spawned sessions take their bead with `enact start`, which moves the
  assignee from the vizier's `vizier:<host>` reservation to their own session
  ID.
- Dolt conflicts are row-level in practice (`updated_at` changes on every
  write), so any concurrent edit of one bead conflicts. `bd sql` is not
  available in embedded mode, so `enact sync` resolves conflicts with the
  `dolt` CLI directly on `.beads/embeddeddolt/<db>`. That makes `dolt` a hard
  requirement.
- `bd init` runs with `--skip-agents --skip-hooks` (Enact's AGENTS.md section
  replaces bd's injected instructions, Claude/Codex hooks and git hooks) and
  commits `.beads/` itself. Fresh clones use `bd bootstrap`.
- bd stores `key=true` metadata as a JSON boolean; the helper normalizes it.
- `enact next --wait` remembers, in a machine-local file, which archive
  entries it has already reported, so a session that refuses archiving
  (still busy) does not make the vizier spin. It is retried at the end of
  each wait.
- The live demo confirmed that Claude Desktop sessions started with
  `start_session` inherit bypass permission mode, can retitle themselves,
  accept `send_message`, and can be archived by the vizier. Spawning into a
  folder the app has never trusted fails without the user present; the
  vizier always spawns into its own (trusted) project.
- A Marshal run as a scheduled task released a bead whose session did not
  exist and push-notified about an epic with no live vizier.
- Tests use stdlib `unittest` rather than pytest, keeping the repository
  dependency-free.
- Codex: `agents/openai.yaml` sets `allow_implicit_invocation: false`. The
  Codex flow is written from documentation and is unverified.

## Housekeeping and validation

- Delete the old `enact` and `enact-audit` skills; uninstall Hive's skill
  links from ~/.llms/skills.
- Validate with pytest black-box tests (real git/bd/Dolt, file:// origin), a
  live demo run with real sessions in Claude Desktop, then push to
  github.com/thurn/enact master. Codex behavior is written from documentation
  and is unverified.
