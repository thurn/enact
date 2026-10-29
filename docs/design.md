# Enact design decisions

Outcome of the design interview (2026-10-28). The original brief is the source
of truth for role definitions; this file records the decisions made on top.

## Audit decisions (2026-10-29)

These decisions supersede conflicting behavior below. They describe the
agreed target; the runtime skills and helper still need the changes listed
in [the desktop support audit](audit-2026-10-29.md).

- Audit against local project chats in the current desktop app, preserving
  Claude Desktop support. Identify supported tools and execution location
  explicitly rather than treating product names as capability guarantees.
- Invoking `/enact` (`$enact` in Codex) authorizes creating, messaging and
  archiving its role chats, and creating and removing its recurring Marshal
  task. Carry that authorization into the Marshal and worker prompts.
- A run finishes only when its delivered commits have passed slow CI and
  reached `master`, including any required Steward fixes. Workers may still
  close their beads after pushing. Bead closure alone is not run completion.
- The Justiciar retains broad emergency authority. Direct changes to
  `master` are exceptional recovery: explain why repairing staging and
  running CI cannot resolve the emergency, preserve the old SHA, and record
  the action. Normal completion still requires green slow CI.
- A replacement worker recovers an abandoned worktree after confirming the
  previous chat is no longer running. Preserve committed and uncommitted
  work, inspect it against current staging, and reuse what remains useful.
  On another machine, preserve the original work and restart from the latest
  pushed state.
- On a capable host, the Marshal automatically resumes an unexpectedly
  stopped vizier, with bounded retries and notification if recovery fails.
  Respect intentional stops and pauses. Hosts that cannot resume a vizier
  retain the manual-resume fallback.
- "Stop" pauses new work for the whole epic across machines and providers.
  Record the pause in the shared graph, let current workers finish, and
  require an explicit resume before any vizier starts more work.
- Every filed fix belongs to the current run. Larger projects may put fixes
  at the end, but there is no separate backlog. Raise the filing bar: if a
  finding is not worth fixing in this run, it is not worth filing.
- If the Justiciar exhausts recovery and progress truly requires new access
  or a human decision, mark the run blocked, preserve unfinished requirements
  and work, stop automatic retries, and notify the user with the exact need.
- If a configured model or reasoning level is unavailable, use the app's
  default and record the substitution. An explicitly required model remains
  a blocker rather than an implicit permission to substitute.

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
- Local `staging` and `master` track `origin/staging` and `origin/master`
  (as Tollgate does for local master): every helper fetch, push to staging,
  slow-CI pass and Marshal `enact status` fast-forwards them. A branch
  checked out in a worktree (normally `master` in the main checkout) moves
  with `git merge --ff-only` only when that checkout has no tracked changes;
  others move with a compare-and-swap `update-ref`. Diverged or dirty
  branches are left alone and reported (`local_branches`), never rebased:
  nobody commits on them.
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

- One user-invoked skill that scales with its input, `/enact <input>
  [parallelism]`. (A separate `/enact-intake` planning skill was tried and
  reverted: one entry point is simpler, and small tasks should not need a
  planning step.)
  - Bead: execute it. An epic is the run (joining another vizier's run needs
    nothing special); a plain bead is wrapped in a new epic, since follow-ups
    and `enact next` work by epic.
  - Plan document: full stoppage audit with the user, full decomposition.
  - Task prompt: smallest graph, one executor bead plus a Warden bead only
    for substantial changes, no milestones or gates; a prompt describing a
    large work item is treated as a plan.
- Pre-flight: audit stoppage risks and resolve each with the user up front;
  refuse to start if any remain. Require a non-prompting permission mode.
  `git pull --ff-only` in ~/enact. Create the Marshal routine if missing.
- `enact install` removes dangling links to retired skills
  (`enact-intake`).
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
- Reservations record `enact_spawn_provider`, and `enact status` lists only
  beads spawned by the caller's host app: a Claude Marshal cannot see Codex
  threads (it would release their beads as "session gone"), and a Codex run
  must not keep the Claude Marshal alive. Beads reserved before the field
  existed count for every app.

## Sage

- May edit ~/enact, but only through Enact's own flow: a worktree from
  `origin/staging` and `enact push`. The Enact repository is itself set up
  with Enact (Beads prefix `en`), so its slow CI advances its `master`, which
  is what every machine pulls.

## Archivist

- One Archivist bead ends every plan run (small task-prompt runs get none).
  It deletes the plan document if the repository tracks it, fixes links that
  would dangle, and edits the persistent docs to describe the shipped state
  only, minimally (YAGNI), within the project's existing doc architecture.
- `enact next` holds a `role:archivist` bead until it is the last open
  non-epic bead in the run epic. Planned dependency edges could not cover
  follow-ups filed mid-run, and tasks cannot depend on epics in Beads.
- Default architecture when a project has none: one project skill. Claude
  Code reads project skills only from `.claude/skills/`, Codex only from
  `.agents/skills/` (cwd up to the repository root). The content lives in
  `.agents/skills/<project>/SKILL.md`; `.claude/skills/<project>/SKILL.md` is
  a stub with the same frontmatter that points at it. No symlinks: git
  checks them out as plain files on Windows without developer mode. Large
  projects may add `references/` files next to the content; the skill, stub
  and references stay at five markdown files or fewer.

## Configuration

- Worktrees live in `.worktrees/` inside the main checkout by default, not in
  a sibling directory, so a project stays one directory. `enact setup` adds
  the root to `.gitignore`; `enact worktree create` falls back to
  `.git/info/exclude` for clones without that entry. The hidden name keeps
  most tools (pytest, unittest discovery, tsc globs) from descending into it;
  projects whose tools do not skip dot-directories should exclude it.
  Projects with an explicit `root` in `.enact.toml` keep it.
- `enact setup` ignores and untracks bd's audit log,
  `.beads/interactions.jsonl`. bd appends to it on every claim and close in
  the main checkout, which Enact never commits to, so a tracked copy kept
  `master` dirty and blocked fast-forwarding it. The issue history it
  duplicates lives in Dolt. Existing projects pick this up by re-running
  `enact setup` and committing the result.
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
  🎖️ Marshal, 🧱 Mason, 📖 Sage, 📜 Archivist; ✅ on completion.

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
- Slow CI (2026-10-29): the integration suite builds origin and two machines
  once per class and restores that snapshot to the same path before each
  test, since `bd init` and bootstrap dominated runtime. Tests of rarely used
  coordination paths (Justiciar, release, deadlock, steward priority) and the
  already unit-tested Archivist hold were deleted rather than made opt-in:
  every test runs in fast or slow checks or does not exist. The remaining
  cost is real: each `bd` call starts embedded Dolt (~0.7s) and `enact next`
  performs three syncs (~5s each).
- Codex: `agents/openai.yaml` sets `allow_implicit_invocation: false`. The
  Codex flow is written from documentation and is unverified.

## Housekeeping and validation

- Delete the old `enact` and `enact-audit` skills; uninstall Hive's skill
  links from ~/.llms/skills.
- Validate with pytest black-box tests (real git/bd/Dolt, file:// origin), a
  live demo run with real sessions in Claude Desktop, then push to
  github.com/thurn/enact master. Codex behavior is written from documentation
  and is unverified.

## Durable delivery and final promotion (2026-10-29)

- Before pushing staging, save a local delivery receipt in the git common
  directory. It contains the bead, run epics, delivered commits, previous
  commit history and CI intent. Only retire it after confirmed remote
  ancestry, published metadata and CI startup. `push`, `next` and `status`
  recover confirmed receipts; a successful push followed by a failed sync
  can be retried without new commits. Receipts survive worktree removal.
- `enact_commits` is an ordered, deduplicated history, including repeated
  pushes. Sync recovery unions and verifies metadata after remote-wins
  merges. CI launches an immutable helper snapshot in the git common
  directory, so self-delivery cannot delete the child's entry point.
- Assignments record `enact_run`; deliveries and CI failure beads record
  `enact_runs` (multiple runs may share a Steward). Repeated CI failures
  retain affected runs. Older beads infer run membership from the parent
  graph; legacy Stewards can be matched through their failing commit range.
- `next` reports `done` only after epic closure, no local assignments, closed
  required Stewards and all run commits being ancestors of remote master.
  It fetches before checking and restarts missing validation. Unrelated
  later staging commits and failures cannot prolong a green run. Workers
  still close after delivery; the vizier owns final promotion waiting.
- `status.pending_deliveries` retains closed epics needing delivery recovery.
  CI status includes the last outcome, and a passing check whose promotion
  push failed can retry promotion without rerunning the same check. Any
  machine's successful slow CI still suffices; no OS quorum was introduced.
- Warden recovery checks: retain a passing candidate in its own promotion
  receipt before attempting the master push. Newer failing checks cannot
  erase it. Retry that promotion independently of the staging tip, and use
  its tested commit as the failure baseline so unrelated later failures
  cannot hold an already-passing run. A peer's promotion of that candidate
  or a descendant also satisfies the receipt.
- A Steward's explicit run links may be incomplete when delivery metadata
  sync recovers after failure reporting. Always supplement those links from
  the failing range, including closed repairs, before deciding a run is done.

## CI lock publication and recovery (2026-10-29)

- New runners hold a nonblocking OS lock on a permanent `ci.lock.guard`
  file through PID publication, protected CI work and PID cleanup. POSIX
  uses `flock`; Windows uses an `msvcrt` byte lock. Never unlink the guard:
  replacing its inode would split ownership. Process death releases the OS
  lock without a timeout or another stale-lock election.
- Write the PID to a private file, close it, then hard-link it create-only
  as `ci.lock`. Existing helpers still read the same plain PID, without an
  empty publication window. A killed publisher may leave a harmless private
  temporary file; it cannot block the next runner. Hard-link support on the
  local repository filesystem is required; publication errors fail closed.
- Under the guard, recover complete markers only when their PID is dead
  and the observed file identity is unchanged. Respect live legacy owners.
  Empty, malformed and nonpositive legacy markers fail closed: an old
  writer paused before publication is indistinguishable from an abandoned
  empty file. Remove such a marker only after confirming no old runner can
  still publish it. New runners do not create this ambiguous state.
- Compatibility cannot repair unmodified legacy contenders that already
  observed a stale owner and later unlink a replacement without checking.
  The concurrency guarantee applies between updated runners; drain older
  contenders when upgrading. PID reuse remains conservatively busy.
- Deterministic spawned-process tests cover publication, competing stale
  recovery, death before and after publication, and legacy ownership. They
  run in the normal fast suite, which slow CI also includes.
