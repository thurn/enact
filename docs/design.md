# Enact design decisions

Outcome of the design interview (2026-10-28). The original brief is the source
of truth for role definitions; this file records the decisions made on top.

## Audit decisions (2026-10-29)

These decisions supersede historical behavior. The implementation and live
validation evidence below track the findings from the
[desktop support audit](audit-2026-10-29.md).

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
- Default CI budget: fast checks around 1 minute, slow checks around 5
  minutes, overridable in a project's `AGENTS.md`. Wardens and Masons
  enforce it by refactoring slow tests to faster strategies or deleting
  them; slow tests are never kept "just in case".

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
  cross-machine race backs off. `enact push` appends `enact_commits` and
  repairs delivery receipts; `enact close` closes the bead and syncs.
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
  - Task prompt: smallest graph, one executor bead, no milestones, gates or
    Marshal; a prompt describing a large work item is treated as a plan.
    Review is none, inline or a Warden bead (below).
- Pre-flight: audit stoppage risks and resolve each with the user up front;
  refuse to start if any remain. Require a non-prompting permission mode.
  `git pull --ff-only` in ~/enact. Create the Marshal routine if missing,
  except for a small task-prompt run: hourly recovery is for multi-hour
  runs, and a small run must not delete a Marshal another run relies on.
- Small-run overhead (2026-10-29, after en-3cp): a one-feature task prompt
  took ~29 minutes for ~6 minutes of implementation. Each bead pays a cold
  start (start, title, re-read bead/epic/AGENTS.md, worktree, push, CI), and
  an executor's follow-up duplicated what the pending Warden then fixed.
  - Inline review: a `review:inline` label makes the executor run one
    fresh-context cold-review subagent on its diff before pushing (one
    session, one push, one CI run). A Warden bead is kept for large or
    risky changes or when the user asks for one.
  - Follow-ups are essential-only (real bugs, missing tests for risky
    behavior, design problems that will bite soon, broken tooling); every
    bead costs a session, and filing needs some friction. Mason no longer
    files lower-value ideas. Issues in code a not-yet-started Warden will
    review go to that Warden bead as comments; a Warden closes beads its
    fixes cover.
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
  gate; `enact next` closes eligible epics.
- Loop runs inside the user's original turn (Claude Desktop refuses
  `start_session` on turns started by automated events or other sessions):
  `enact next --wait 540` (unique vizier reservation, returns an action),
  spawn `start_session`/`create_thread`, record the ready `enact_session`,
  and repeat. Resume one yielded process through bounded tool waits; never
  create competing scheduler processes. Output restates the loop so it
  survives compaction.
- Selection scope: ready descendants of the epic plus unparented Steward beads.
  Capacity = open beads with `enact_vizier=<me>`; default parallelism 1.
- The vizier archives sessions of closed beads reported by `enact next`.
- All spawns go through the vizier, including the Justiciar
  (`enact justiciar request`); the requesting host's vizier spawns it.
- Termination: `enact next` reports done only after epic closure, required
  Stewards and promotion of every run delivery to green master. Peer claims
  and pending validation keep the run alive. A genuine deadlock requests a
  Justiciar; paused and blocked states retain unmet acceptance.
- Spawn prompt names `roles/common.md` and `roles/<role>.md` by absolute path,
  plus the bead and epic. Optional per-role model/effort from `.enact.toml`.

## Justiciar

- Fleet-wide, per-project lock: `refs/enact/justiciar` on origin, created with
  a create-only push; the commit records host, session, time, reason. Holder
  is printed to anyone who tries to acquire. Deleted on exit; the Marshal
  breaks it only after a routed lookup confirms the holder archived or
  absent. Age, silence and lookup failures do not prove abandonment.

## Marshal

- One per project per machine per host app: an hourly Claude Desktop scheduled
  task or Codex automation, with a lockfile against overlapping sweeps.
- Uses durable, bounded recovery permissions to nudge confirmed stalled
  workers and resume stopped viziers on capable hosts. Preserves paused and
  blocked state and unfinished work. Archives completed chats, restarts dead
  CI and pulls ~/enact. Unsupported recovery retains a one-time notice;
  Claude scheduled turns cannot start sessions.
- Small task-prompt runs neither create nor delete it.
- The vizier deletes only its identified Marshal when `enact status`
  reports `sweep_needed: false`. Other relevant active runs between
  assignments and pending final deliveries keep it alive.
- Reservations record `enact_spawn_provider`, and `enact status` lists only
  beads spawned by the caller's host app: a Claude Marshal cannot see Codex
  threads, and a Codex run must not keep the Claude Marshal alive. Legacy
  routing remains visible for inspection; uncertainty never authorizes
  releasing ownership.

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
  claim close sync next worktree justiciar install setup`, plus ownership,
  run-control, status and recovery commands shown by `--help`.
- Skills linked per machine into user-level skill dirs (symlinks; junctions on
  Windows). `enact-setup` per project: beads init, dolt remote, staging
  branch, `.enact.toml`, AGENTS.md bullets.
- Titles: 🔮 Vizier, ⚒️ Executor, 🛡️ Warden, 🔥 Justiciar, 🚒 Steward,
  🎖️ Marshal, 🧱 Mason, 📖 Sage, 📜 Archivist; ✅ on completion.

## Implementation notes

Findings and adjustments made while implementing (2026-10-28):

- Spawned sessions take their bead with `enact start`, which moves the
  assignee from a unique vizier reservation to their own session ID, fenced
  by the reservation token.
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
- Codex: `agents/openai.yaml` sets `allow_implicit_invocation: false`.
  Desktop validation is recorded below; it does not establish other hosts'
  capabilities.

## Housekeeping and validation

- Installation removes only obsolete links owned by this checkout and
  preserves unrelated skills.
- Validate with stdlib unittest black-box tests (real git/bd/Dolt,
  file:// origin) and bounded desktop smoke checks. Deliver through
  `enact worktree create`, commit and `enact push --bead`; slow CI alone
  advances master. Record tested hosts and remaining limits explicitly.

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

## Slow-check lifetime ownership (2026-10-29)

- The CI coordinator retains the OS guard, but delegates each check to a
  separate keeper process. Before authorizing that keeper through a private
  stdin pipe, atomically replace the plain `ci.lock` PID with its PID. A
  keeper that receives EOF without authorization exits without touching a
  worktree. Thus coordinator death before launch, between launch and PID
  publication, or between publication and authorization cannot leave a
  delayed child able to start unprotected work.
- An authorized keeper owns worktree creation, the synchronous slow
  command, worktree removal, promotion and durable result recording. It
  survives coordinator death and retains the live plain-PID lease, which
  both current and legacy contenders respect even after the coordinator's
  OS guard releases. Output goes to `ci-check.log`, independent of the
  coordinator's pipes. It records `ci-result.json` and `ci-last` itself, so
  completed orphan checks need not be repeated by a replacement runner.
- After all protected work, the keeper restores a live coordinator's PID
  before exiting; otherwise it leaves its own marker for stale recovery.
  This avoids exposing a dead keeper PID to legacy contenders while a live
  coordinator continues its loop. Status reports the lease owner's PID,
  which can differ from the coordinator PID returned by detached startup.
- This protects against loss of the coordinator, including its startup
  handoff. The keeper and synchronous command must be allowed to finish;
  forcibly killing the keeper while leaving its shell descendants alive
  is outside this protocol. Slow commands must wait for their protected
  work instead of deliberately detaching it. Existing legacy crash states
  still require draining old checks before upgrading.
- Normal slow CI includes a real-git, real-CLI regression with deterministic
  barriers before spawn, after spawn, after PID publication, after
  authorization, inside the running check, before worktree cleanup and
  before result recording. It kills only the coordinator, checks exclusion
  and worktree preservation through cleanup, then verifies recovery and a
  single completed check. Ownership must extend beyond command exit until
  the keeper finishes cleanup and records its outcome. Local execution
  evidence is macOS; Windows and Linux use portable stdlib paths but have
  not been executed in this change.

## Ownership and abandoned work (2026-10-29)

- Reservations identify host, provider and vizier session, with a fresh token
  carried in the spawn prompt. The token authorizes the first handoff; an
  already recorded child identity must also match. Existing sessions remain
  usable: a legacy reservation without a token requires its recorded child
  ID; a worker that
  already owns the bead can resume. Same-host operations serialize their
  check/write/sync sequence with the existing portable OS lock. Remote Beads
  conflicts retain remote-wins semantics and published ownership is checked.
- Close, metadata changes, release, worktree operations and push check current
  ownership after sync. Push infers a bead from its tree even when `--bead`
  is omitted, and checks again immediately before publishing. Ownership is a
  cooperative helper protocol: raw git/Beads writes and unmodified older
  helpers cannot be fenced. Upgrade resumed workers before they act. Recovery
  must first establish inactivity; it is not permission to preempt a live
  command between its final check and remote git publication.
- Actual creator routing uses `enact_spawn_host/provider/app/project`, apart
  from targeting `enact_host/provider`. App defaults to the provider and
  project to the main checkout; the vizier records an actual app/project ID
  when the desktop supplies one. Status surfaces routing uncertainty for old
  records. Missing provider/app/project, unsupported tools, transient failures
  and failed lookups never establish inactivity. A live vizier can backfill
  its known app/project route through `meta` before a Marshal handoff. The
  Marshal supplies the exact observed owner and child (or vizier for an
  unspawned reservation), a confirmed
  inactive state and successful lookup evidence when releasing another owner.
- Release retains worktree pointers and appends `enact_recovery` records with
  old ownership, route, host, path, branch, local head and inactivity evidence.
  The current worker may explicitly relinquish its own assignment. A new
  local owner reuses the verified abandoned git worktree without resetting,
  cleaning or rebasing it; all commits, tracked edits and untracked files
  survive. Inspect against staging before continuing. A remote replacement
  starts from staging and retains the original recovery record, including its
  original host; it cannot transfer unpublished files across machines.
- Worktree creation records its host/path/branch and can be retried. A
  never-assigned bead is claimed for direct CLI work; released beads must be
  reassigned first. Explicit removal refuses dirty or unpublished work,
  including branch commits whose checkout directory has disappeared, and
  successful delivery retains a tree with untracked artifacts. Durable
  delivery receipt replay may append factual already-pushed commits after
  reassignment; it does not authorize new publication by an old worker.
- Vizier routing writes and reservation releases require the token from the
  original spawn action. A vizier may reserve the same bead repeatedly, so
  its identity alone cannot distinguish a delayed response from the current
  assignment. Legacy reservations without tokens keep their existing flow.
  Marshal releases of reservations also supply the observed token.
- If a checkout disappears but its local branch survives, creation restores
  that branch without resetting its commits. Git rejects a branch still
  checked out elsewhere, including a locked worktree. Restoring a missing
  checkout reports the surviving head as its base; new branches still start
  at staging.


## Shared run holds and bounded recovery (2026-10-29)

- `run register` stores each vizier's host, provider, app, project, session,
  parallelism and invocation authorization on the epic. `next` also records
  its route for legacy callers, without inventing missing authorization.
  Registration never resumes a held run. Different viziers use separate
  metadata keys; remote-wins sync verifies every published update.
- `run pause` and `run block --exhausted` set shared control independently
  of Beads issue status. Claims, reservation starts, scheduling and Marshal
  recovery check holds on run links and ancestors. Workers already running
  may push and close under a hold. Automatic epic closure excludes held
  epics and their descendants; unmet acceptance remains open. Block a run
  only after Justiciar recovery cannot resolve missing access or a human
  decision. Do not replace impossible requirements with endless retries.
- Only `run resume --explicit --reason` clears a hold and creates a new
  recovery epoch. The flag attests an explicit user instruction, not a
  permission available to automatic recovery. Raw Beads writes and old
  helper versions can bypass this cooperative protocol; update participating
  peers before relying on fleet-wide pause enforcement.
- `run recover` records one attempt before a native message is sent. Per
  epoch it permits two nudges per worker session, three resume messages per
  vizier, and one fallback notice per vizier. Attempts are at least an hour
  apart, survive process death and sync, and retain evidence and timestamps.
  An uncertain send consumes its attempt. Only explicit resume resets the
  budget; apparent activity or a new sweep does not erase history. A
  successful routed lookup remains mandatory, and incapable hosts notify
  rather than pretending they can restart orchestration.
- `status.active_runs` includes registered runs before first assignment,
  between workers and after epic closure until final delivery completes.
  Legacy routes are inferred from recorded assignments for inspection and
  a fallback notice; automatic restart needs retained authorization.
  `sweep_needed` also accounts for running workers and unarchived chats;
  paused/blocked idle runs do not require automatic recovery. Pending
  deliveries keep validation alive. `staging` remains the remote tip SHA.
- `marshal_name` is stable for the absolute project path, host, provider and
  app, avoiding basename collisions. The scheduled standalone project sweep
  retains invocation authorization and its automation ID, stays quiet while
  unchanged, and is removed only when no relevant run or cleanup needs it.
  Desktop tool schemas and the available host capability decide how to
  perform scheduling and messaging; the helper never calls native tools.
- Warden review: release checks shared holds after syncing, so an earlier
  Marshal snapshot cannot clear an assignment after a peer has paused it.
  Held assignments remain intact until explicit resume. Reservation release
  still requires the exact observed token, including Marshal recovery.
- Marshal retention counts pending validation for this host/app's runs.
  Project-wide pending deliveries remain visible in status for diagnosis,
  but work belonging only to remote hosts or other apps cannot retain an
  otherwise idle local sweep. Cleanup and CI for local deliveries continue
  under a hold. Overlapping local recovery requests serialize and reserve
  only one nudge; uncertain sends still consume that durable reservation.

## Desktop session ids (2026-10-29)

- Claude Desktop's `start_session` returns the host session id
  (`local_...`, also `CLAUDE_CODE_HOST_SESSION_ID`), which the vizier records
  as `enact_session`; `CLAUDE_CODE_SESSION_ID` is a different CLI id. Spawned
  workers' `enact start` failed with exit 3 until they set `ENACT_SESSION` by
  hand (four of the playlist_survivors ps-tfn M1 sessions).
- Start, owner checks and release now treat the host id and the CLI id as
  names of one session, taking whichever the bead records. The default actor
  stays the CLI id, so live viziers keep their claims. `ENACT_SESSION`, when
  set, is still the only name.


## Desktop adapter and installation (2026-10-29)

- The adapter is prompt text using the tools supplied by the owning app;
  no Python wrapper for desktop APIs is needed. Codex selects a registered
  project by full path and host, creates local top-level chats, maps an
  authorized effort preference to `thinking`, and records ready chat IDs.
  Pending client IDs never become ownership identities. Defaults remain
  implicit unless the user requested model settings; unavailable preferred
  settings fall back with a bead comment, while required models block.
- Pre-flight verifies tools and local access before reservation. Long helper
  waits stay in one process with bounded continuation calls and mid-run
  steering. The loop retains created-chat receipts for its final UI output.
- Marshal discovery uses exact identity plus verified legacy aliases. Its
  saved ID belongs in the run's bead comments; creation, migration and
  deletion use the current app schema. A lookup error never authorizes a
  duplicate sweep. Retention follows all relevant active runs and delivery
  state, and bounded recovery still uses the helper's durable permissions.
- Installation always creates `~/.agents/skills`, including when only
  `.codex` exists. Claude is installed when its configuration directory
  exists. This checkout's standalone legacy Codex links are removed only
  after canonical installation succeeds; unrelated content and shared
  Claude roots remain intact. Roots resolving to one directory are handled
  once. No new links are installed in the legacy Codex directory.
- Official [skill discovery documentation][skill-discovery] confirms the
  canonical user path, symlink support, and that duplicate names are not
  merged. Tests exercise fresh installation, repeat installation, migration,
  conflicts and shared Claude roots through the CLI. They verify filesystem
  discovery candidates; app selector refresh remains an app-level check.
- Follow-ups inherit the run's restrictions and review policy and must be
  worth completing in the current run. There is no separate backlog.

[skill-discovery]: https://learn.chatgpt.com/docs/build-skills

### Codex desktop smoke receipts

On 2026-10-29, executor en-b2c.1.7 used the registered local project
`facd00e9-d913-430e-ad31-b727ae9a8c18` at `/Users/dthurn/enact` on jarvis.
The desktop tool host is `local`, distinct from the OS hostname.

- `create_thread` with local target and omitted model settings returned ready
  chat `01a18aa6-457d-7fa2-b882-3375bd485d98`, host `local`. The chat's
  environment reported the identical `CODEX_THREAD_ID`, correct checkout,
  unrestricted filesystem access and approval policy `never`. It read
  AGENTS.md, checked the Git root, and wrote/read/deleted a temporary receipt.
- `wait_threads`, `read_thread` and `list_threads` confirmed its completed
  turn and idle status. The first turn deliberately ended at a phase marker.
  `send_message_to_thread` resumed the same chat; the second turn confirmed
  its ID and local access and returned `SMOKE_RECOVERY_COMPLETE`.
- `set_thread_archived` returned `archived: true`; `list_archived_threads`
  independently listed that same ID. No repository changes or Beads claims
  were made by the smoke chat.
- This proves bounded local creation, access, inspection, messaging,
  continuation and archive cleanup. It simulates a stopped workflow, not an
  operating-system crash or forced app shutdown. Pending creation, rejected
  model settings, scheduled-turn spawning, Windows and Linux desktop tools
  were not exercised. Integration tests cover persistent recovery fencing
  and pause/budget behavior separately; normal run deliveries provide the
  real staging/slow-CI/promotion evidence.

- Automation lifecycle smoke created a paused standalone job with ID
  `enact-marshal-acd7df3c75f96b3dfda9-smoke`, inspected its saved TOML, updated
  its name while retaining the ID and paused state, then deleted it. The
  tool returned `deleteStatus: deleted`; saved-record absence confirmed it.
  The job never ran. The tool requires an explicit automation model/effort;
  this test used available `gpt-6.1-sol` / `low`.
- The previously reported active legacy sweep `enact-marshal-enact` was
  absent from saved automation records on inspection, despite an archived
  02:04 UTC sweep receipt. No legacy sweep was deleted by this executor.
  After a second complete saved-record scan found no Enact sweep, restored
  one active standalone job under the helper's stable identity
  `enact-marshal-acd7df3c75f96b3dfda9`, hourly at minute seven, using the same
  project and model/effort above. Creation and saved active state were
  verified. Its exact ID was recorded on the run for final cleanup; it must
  remain while `sweep_needed` is true. This tests scheduling configuration,
  not execution of a future hourly sweep.


### Desktop review and scheduling uncertainty (2026-10-29)

- Warden en-b2c.1.8 independently read both smoke-chat turns and its archive
  listing. The recorded ready identity, temporary local access check,
  deliberate turn termination, continuation and cleanup are supported.
  This remains a bounded continuation test, not a crash-recovery test.
- After the executor's verified active creation, the replacement Marshal's
  saved record disappeared by 03:13 UTC on 2026-10-30. Warden inspection
  confirmed neither that ID nor the legacy ID remained in the configured
  automation directory. The last archived legacy sweep at 02:04 UTC
  reported `sweep_needed: true`, retained the sweep and issued no deletion.
  The cause is unknown; no helper cleanup defect has been established.
- `automation_update(mode: "view")` returned only a rendered-card receipt,
  which does not establish existence or active scheduling. Creation and
  migration must verify the saved ID, project, prompt and active state.
  If a verified required sweep disappears, preserve the receipts and request
  Justiciar recovery; do not repeatedly recreate it or claim completion.
  Scheduling persistence and final cleanup remain open run requirements
  until recovery supplies evidence. The initial lifecycle smoke remains
  valid, but it does not establish continuing hourly execution.
