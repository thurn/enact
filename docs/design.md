# Enact design decisions

## Marshal removed (2026-11-11)

The hourly scheduled Marshal sweep is gone. An audit of about 75 hourly
beryl sweeps over three days found it never nudged a worker, resumed a
vizier, released a reservation or started CI. Its only output was
archiving three finished chats (two more archive attempts were refused for
live work) and marking already-archived chats on their beads, at 1-4
minutes per run and once 18. The recorded session IDs for several sessions
also did not resolve, so it is doubtful it could have recovered a real
stall. Claude scheduled turns could never start sessions anyway, so on
Claude Desktop its vizier recovery was only ever a notice.

- Viziers no longer create or delete a scheduled task or automation.
  Archiving finished chats stays in `enact next`. A vizier whose turn ends
  waits for the user to write to it, then continues the same epic.
- Removed from the helper: `enact lock`, `enact run recover` and its
  attempt budgets, and `marshal_name`, `sweep_needed` and recovery history
  in `enact status`. `enact status` stays for setup, the Justiciar and
  `/enact-sweep`; `enact release --inactive --evidence` stays for handing
  off a confirmed inactive child. Legacy `enact_marshal_attempt_*` keys
  remain protected from `enact meta`.
- `skills/enact/roles/marshal.md` is a stub: existing scheduled tasks on
  every machine still point at it, so it tells them to delete themselves
  and do nothing else.
- Dated entries below that describe the Marshal record the retired design.

## Marshal status reachability (2026-11-03)

`enact status` evaluates delivery for every epic, including completed runs
whose commits may still await slow CI. On a busy project this previously
launched a separate `git merge-base --is-ancestor` process for each commit
and for repeated legacy Steward failure ranges. The Marshal saw no JSON for
minutes and ended its scheduled sweep before it could inspect run state.
For one status call, cache `git rev-list` reachability by tip and reuse it
across all epics. Other delivery callers use the same logic with a local
cache. A missing ref remains unreachable, as with the former ancestry check.

## Marshal sweep leases (2026-10-29)

Named sweep locks are session-owned leases. A short-lived mutation lock
using the existing OS-lock primitive serializes acquisition, stale
replacement and release within a clone. The lease JSON is published by
atomic replacement; the mutation guard inode stays permanent. CI's PID
lock protocol is unchanged. Sweep commands require a session identity,
and release checks the recorded owner against the current session aliases.
A missing lease can be released idempotently, but an unknown or different
owner cannot be released.

Staleness retains the existing file-mtime policy: acquisition may replace
a lease at 120 minutes by default, or the caller's `--stale-minutes` value.
Zero permits immediate takeover. This is expiry, not proof that the old
chat died, and it does not cancel work already running in that chat. After
takeover its release cannot delete the successor's lease. Old or malformed
lease files can still be recovered through expiry. Active commands must
use the updated helper; an already-running old helper cannot enforce this
protocol. Public CLI regressions cover concurrent fresh acquisition at the
publication boundary, stale takeover, owner-fenced release and recovery.

## Run contracts

The helper and role prompts enforce these contracts cooperatively; raw git
and Beads writes and unmodified older helpers remain outside that protocol.

- Audit against local project chats in the current desktop app, preserving
  Claude Desktop support. Identify supported tools and execution location
  explicitly rather than treating product names as capability guarantees.
- Invoking `/enact-plan` (`$enact-plan` in Codex) authorizes creating,
  messaging and archiving its role chats. Carry that authorization into
  worker prompts.
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
- An unexpectedly stopped vizier resumes when the user writes to it.
  Respect intentional stops and pauses.
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
- The slow-CI runner kills a slow check's whole process tree after
  `[checks] slow_timeout` seconds (default 3600) and reports a failure, so a
  hang (a stuck exec, a server never stopped) files a Steward bead instead of
  holding `master` back indefinitely. Budgets stay the project's to enforce.

## Git flow

- `staging` is a branch on origin. Worktrees are cut from `origin/staging`.
- `enact push` (used by executors *and* humans, replacing `git push origin
  master`): rebase onto `origin/staging`, merge `origin/master` if it is not an
  ancestor (backstop for direct-to-master pushes), run fast checks,
  fast-forward push `HEAD:staging`, retry on rejection, remove the worktree
  (or report `kept_worktree` if untracked files remain), then start the
  slow-CI runner. The order matters: a project's remove hook may save build
  caches that the runner's fresh worktree warms itself from, and a runner
  started first raced it to a cold build (2026-11-08, Beryl's first Windows
  run). The delivery receipt stays until the runner has started.
- `enact ci --detach`: one coalescing runner per machine (PID lock in the git
  common dir). Loop: fetch, test the newest `origin/staging` tip, repeat until
  the tip equals the last tested SHA; re-check after releasing the lock. No
  debounce.
  - Pass: fast-forward `origin/master` to the tested SHA (slow CI green).
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

- One user-invoked skill that scales with its input, `/enact-plan <input>
  [parallelism]`. One entry point keeps small tasks lightweight.
  - Bead: execute it. An epic is the run (joining another vizier's run needs
    nothing special); a plain bead is wrapped in a new epic, since follow-ups
    and `enact next` work by epic.
  - Plan document: full stoppage audit with the user, full decomposition.
  - Task prompt: smallest graph, one executor bead, no milestones or
    gates; a prompt describing a large work item is treated as a plan.
    Review is none, inline or a Warden bead (below).
- Pre-flight: audit stoppage risks and resolve each with the user up front;
  refuse to start if any remain. Require a non-prompting permission mode.
  `git pull --ff-only` in ~/enact.
- Inline review: a `review:inline` label requires one fresh-context
  cold-review subagent before pushing. Use a Warden bead for large or risky
  changes or when the user requests one.
- Follow-ups are essential-only: real bugs, missing tests for risky behavior,
  imminent design problems or broken tooling. Each bead costs a session.
  Comment issues on a not-yet-started Warden that will review the code
  rather than duplicating its work; a Warden closes beads its fixes cover.
- `enact install` removes dangling links to retired skills
  (`enact-intake`).
- Decomposition: full graph up front or rolling milestones, at the vizier's
  discretion; "decompose milestone N" beads go to the executor role. Agents
  re-validate stale beads against current code.
- Warden default: one Warden bead per lane or milestone area, per bead only
  for risky changes (see Token economy); code-dependent work depends on the
  Warden bead.
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
  Parallelism comes only from the invocation or a later user message, never
  from bead, epic or plan text: the same graph can run at different widths,
  and a stored level would silently override the user. Viziers do not write
  it into beads and ignore it when present; `run register` metadata records
  the invocation's value for recovery only.
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
  is printed to anyone who tries to acquire. Deleted on exit; a session
  breaks it only after a routed lookup confirms the holder archived or
  absent. Age, silence and lookup failures do not prove abandonment.

## Marshal

Removed on 2026-11-11; see [Marshal removed](#marshal-removed-2026-11-11).
`enact status` still lists only beads spawned by the caller's host app: a
Claude session cannot see Codex threads. Legacy routing remains visible for
inspection; uncertainty never authorizes releasing ownership.

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
  the main checkout, so tracking it would block fast-forwarding `master`.
  The issue history it duplicates lives in Dolt. Existing projects pick this
  up by re-running `enact setup` and committing the result.
- `.enact.toml`: fast/slow check commands, `[worktree]` root plus optional
  full-replacement `create`/`remove` hooks (env: `ENACT_WORKTREE_PATH`,
  `ENACT_BRANCH`, `ENACT_BASE_SHA`, `ENACT_REPO`), optional
  `[roles.<role>]` model/effort per host.
- The check and hook commands are POSIX shell on every OS (2026-10-30). On
  Windows they run in Git for Windows' `bin/bash.exe` (found next to the
  `git` on PATH, else under `%ProgramFiles%\Git`; `ENACT_BASH` overrides),
  not in cmd.exe, so one `.enact.toml` serves macOS, Linux and Windows
  machines. Git for Windows is already required for git itself; the
  `bin/` launcher, unlike `usr/bin/bash.exe`, puts the MSYS tools on PATH.
- AGENTS.md: agent-facing prose bullets (enabled roles, review policy, how to
  use `enact push`).

## Packaging

- One stdlib-only `scripts/enact.py` (Python 3.11+), subcommands `push ci
  claim close sync next worktree justiciar install setup`, plus ownership,
  run-control, status and recovery commands shown by `--help`.
- Skills linked per machine into user-level skill dirs (symlinks; junctions on
  Windows). `enact-setup` per project: beads init, dolt remote, staging
  branch, `.enact.toml`, AGENTS.md bullets.
- Four skills: `enact-plan` (the vizier), `enact` (inline),
  `enact-setup` and `enact-sweep` (backlog sweep). Role files stay in `skills/enact/roles/` so spawn prompts
  and Marshal tasks written before the split keep working (the Marshal role
  is now a stub that retires those tasks).
- Titles: 🔮 Vizier, ⚒️ Executor, 🛡️ Warden, 🔥 Justiciar, 🚒 Steward,
  🧱 Mason, 📖 Sage, 📜 Archivist; ✅ on completion, ⛔ when
  stopped unfinished, 🔍 when a turn ends waiting on the user.
- Status titles are an accessibility contract (2026-11-01): the host's
  running indicator has poor contrast, so users read state from the emoji.
  Every session Enact starts or instructs (vizier, workers,
  Justiciar, inline runs, and ad hoc sessions following the AGENTS.md
  section) titles itself on start and ends every final turn on ✅ or ⛔,
  including early stops. Existing projects keep old AGENTS.md text, so
  `enact worktree create` and `enact push` without a bead also print a
  `title` notice, and `watch_ci` names the CI-wait emojis.

## Implementation contracts

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
- Claude Desktop workers inherit bypass permission mode, can retitle
  themselves, receive messages and be archived. The vizier spawns into its
  trusted project: an untrusted folder requires the user present.
- Tests use stdlib `unittest`, keeping the repository dependency-free. All
  tests run in configured fast or slow checks; coverage and isolation are
  described under Slow-check runtime below.
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
- Retain a passing candidate in its own promotion
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
- Release checks shared holds after syncing, so an earlier
  Marshal snapshot cannot clear an assignment after a peer has paused it.
  Held assignments remain intact until explicit resume. Reservation release
  still requires the exact observed token, including Marshal recovery.
- Marshal retention counts pending validation for this host/app's runs.
  Project-wide pending deliveries remain visible in status for diagnosis,
  but work belonging only to remote hosts or other apps cannot retain an
  otherwise idle local sweep. Cleanup and CI for local deliveries continue
  under a hold. Overlapping local recovery requests serialize and reserve
  only one nudge; uncertain sends still consume that durable reservation.

## Desktop session identities

Claude Desktop's `start_session` returns the host session ID
(`local_...`, also `CLAUDE_CODE_HOST_SESSION_ID`), which the vizier records
as `enact_session`. `CLAUDE_CODE_SESSION_ID` is a different CLI ID. Start,
owner checks and release accept both as aliases, using whichever the bead
records. The default actor stays the CLI ID to preserve live claims.
`ENACT_SESSION`, when set, is the only accepted identity.

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

Verified on 2026-10-29 on jarvis (macOS) with registered local project
`facd00e9-d913-430e-ad31-b727ae9a8c18` at `/Users/dthurn/enact`. The
desktop tool host is `local`, distinct from the OS hostname. Run beads
`en-b2c.1.7`, `en-b2c.1.8`, `en-bcw` and `en-b2c.2.2` retain full evidence.

- Chat `01a18aa6-457d-7fa2-b882-3375bd485d98` confirms local creation,
  identical `CODEX_THREAD_ID`, correct checkout, unrestricted filesystem
  access and approval policy `never`, plus a temporary write/read/delete.
  Inspection and a follow-up message confirm continuation after deliberate
  turn termination; archive response and archive listing confirm cleanup.
  This is a bounded continuation test, not an OS crash or forced shutdown.
- Paused standalone automation
  `enact-marshal-acd7df3c75f96b3dfda9-smoke` confirms creation, rename and
  deletion with saved-record absence. It never ran. No disposable smoke
  resources remain.
- Scheduled recovery chat `01a18ae0-f479-7bf2-9956-a43e9fa7baf6` confirms
  actual delivery: sweep lock acquisition/release, routed worker/vizier
  checks, retained sweep with `sweep_needed: true` and successful sync.
  Independent Sage inspection confirms the completed turn and archive
  listing. Saved automation `enact-marshal-acd7df3c75f96b3dfda9` is verified
  ACTIVE at inspection on hourly minute seven with `gpt-6.1-sol` / `low`.
  The run records
  this exact ID for final vizier cleanup when `sweep_needed` becomes false.
- Pending creation, rejected model settings, scheduled-turn spawning,
  Windows and Linux desktop tools are untested. Integration tests separately
  exercise persistent recovery, ownership, holds and retry budgets. Real
  run deliveries provide staging/slow-CI/promotion evidence. No OS quorum or
  indefinite scheduling persistence is claimed.

### Scheduling evidence and recovery

Configuration, scheduled delivery and final cleanup require separate
evidence. A rendered `automation_update(mode: "view")` card does not prove
existence; verify the saved ID, project, prompt and active state. A saved
active record proves configuration; an observed scheduled chat proves
delivery at that time. Final cleanup belongs to the vizier after delivery
and run completion.

The cause of the earlier live-sweep disappearance remains unknown; receipts
establish no helper deletion defect. If a required verified sweep vanishes,
preserve evidence and request Justiciar recovery. Reconcile exact identity
and active-run need, make one supported restoration and verify scheduled
delivery. Return any temporary test cadence to hourly minute seven. Do not
blindly recreate jobs or hand-edit scheduler databases.

## Slow-check runtime (2026-10-29, revised 2026-10-30)

- Keep all scenarios in the normal fast+slow checks. Independent integration
  scenarios take work from a queue in up to eight lanes, bounded by CPU count.
  Twelve lanes measured slower than eight because of contention. Start the
  longest scenarios first to avoid a serial tail. Each lane
  creates its own bare origin, clones and absolute-path snapshot, then uses
  unittest's usual fixture lifecycle. Database prefixes are unique per lane and
  temporary Dolt configurations disable telemetry. A lane never edits another
  lane's repository or database. CI/check subprocess races remain real.
- Lane results are replayed on the runner thread, including setup/teardown
  errors, subtest failures, skips and durations. Fast regressions exercise
  concurrent fixture isolation and failure reporting. Focused test names still
  run directly and sequentially for debugging; no test is opt-in. Policy
  stories (desktop identity, reservation callbacks, legacy routing, archive
  scheduling, run holds with recovery budgets and explicit resume, and the
  Marshal release/stale-worker rejection matrix) run through the public CLI
  parser and commands against a small in-memory Beads port in the fast suite.
  Real Beads dependency readiness, including a higher-priority blocked
  dependent, rides along with the shared-pause scenario. Persistence,
  synchronization, racing claims, filesystem recovery, a cross-clone pause
  fence and delivery checks continue to use real git, Beads and Dolt.
- Embedded databases using the default remote can synchronize through the Dolt
  CLI without repeatedly opening Beads' schema engine. Preserve the existing
  commit/pull/push order for new tips, local ownership lock, remote-wins
  conflict reporting, retries and ownership verification after publication.
  Commit failures other than an empty working set abort before pull/push.
  Initial databases may have no remote branch yet; publication uses an ordinary
  push, never a forced database push.
- The Dolt CLI does not wait for the embedded store: while another process has
  it open (any bd command, even a read) it opens read-only and every write
  fails with "database is read only". bd waits instead, and bd's workspace
  gate cannot be borrowed (an exclusive holder makes bd fail fast), so every
  direct Dolt call retries that error with jittered backoff for up to 60 s.
  On Windows, Dolt's file lock keeps it from opening a held store at all
  ("fslock ... being used by another process"), which is retried the same
  way (2026-11-08). Other failures are not retried. A real scenario syncs
  while a held store is released.
- Within an ownership lock, reuse identical Beads show/list reads across helper
  instances. Every write attempt and every synchronization clears those reads;
  leaving the lock discards them. Cache raw JSON so each caller receives
  independent objects. Unlocked observers query Beads directly. Verification
  after publication therefore reads fresh values, including ownership and shared
  holds.
- An embedded sync still commits and pulls before comparing local and remote
  commit hashes. Equal hashes need no redundant push. A clean working set alone
  proves nothing: a real regression verifies that an already-committed
  unpublished bead reaches the other clone.
- Server mode, missing Dolt CLI/identity, and nondefault project remotes keep
  the Beads synchronization path. Tests explicitly configure temporary Dolt
  identities rather than relying on the developer's home directory; a real
  synchronization regression also covers the identity-free fallback.
- Recognize missing-remote-branch diagnostics case-insensitively while
  preserving the actual branch name in Dolt commands. A real two-clone
  scenario covers first publication of a mixed-case branch.
- Scenarios named `test_sync_*` synchronize through the production git-backed
  Dolt remote. All other scenarios share a local file Dolt remote, still real
  Dolt pull/push, because each git-transport push costs seconds and every
  command syncs two or three times. Both transports are snapshotted once per
  lane. The CI-lifetime scenario also takes a lane instead of running first.
- Dolt contends under parallel load on macOS: even read-only `dolt sql` calls
  run about 2.4x slower with eight lanes, while process spawning scales.
  Runtime therefore follows total scenario work, not lane count; six lanes
  matched eight and fourteen were much slower. Budget new real-Dolt checks
  by commands issued, and prefer the fast policy port for refusal matrices.
- For isolated timings, pass a fully qualified scenario name to
  `python3 -m unittest -v`. Python 3.12+ accepts `--durations 0` on the
  normal fast+slow command. Concurrent scenario durations overlap and must
  not be summed as suite wall time.
- Enact's own slow budget is 2-3 minutes (see `AGENTS.md`). On 2026-10-30,
  jarvis, macOS/Python 3.14, bd 1.3.2, the normal fast+slow command passed
  all 72 tests in about 157 seconds.
- Earlier, at `e4e1359` on jarvis, macOS/Python 3.14: Sage normal checks
  passed all 68 tests in 273.385 seconds; separate fast checks passed 36
  tests in 4.580 seconds. Independent Warden normal checks passed in
  264.752 seconds. These support the near-five-minute budget on this host,
  not a runtime guarantee for every host or load. Linux and Windows
  execution are unverified; any machine's passing CI can promote master.

## Slow-CI ownership outside runs (2026-10-30)

- The runner is a detached process and cannot start chats, so a Steward
  bead filed while no vizier loops would sit unworked. A plain prompt in an
  Enact project therefore owns its own slow CI.
- `enact push` prints `watch_ci` when slow CI started, the pushed bead (if
  any) was not reserved by a vizier, and no registered run on any machine is
  unfinished and unheld. A vizier's worker never waits: its run owns repair,
  including after a pause.
- `enact ci --wait <sha>` blocks up to `--timeout` (540 s) and returns
  `passed` once the SHA reaches master, `failed` with the Steward beads whose
  failing range covers it (unassigned or held by this session), or
  `waiting`. Stewards held by another session count as in repair, and
  only this OS's Stewards end the wait: any machine's pass promotes master. While
  waiting it restarts a dead runner when staging is untested.
- On `failed` the session claims the Steward bead and follows the Steward
  role, then watches its repair the same way. Instructions live in the push
  output, so existing project AGENTS.md files need no change.

## Inline skill (2026-10-30)

- `/enact` became inline execution; the vizier moved to `/enact-plan`.
  Many tasks need no unattended fleet, and spawning sessions, a Marshal
  and per-session titles costs more than the work.
- `/enact` never starts sessions, scheduled tasks or a Justiciar. A plain
  bead or task prompt is implemented by the session itself; an epic runs
  each bead in a fresh subagent, sequential by default so the title names
  the working role. Plan documents and large prompts go to `/enact-plan`.
- Review follows the vizier's policy: Executor only for routine changes,
  Executor plus a Warden subagent on its own bead for large or risky ones.
  Subagents cannot start subagents, so an Executor subagent's
  `review:inline` becomes a Warden after its push.
- Mason, Sage and Archivist run only for beads an epic already contains;
  the inline skill never files them.
- Subagents share the orchestrator's session ID: it claims with
  `enact claim`, they push and close. No helper changes were needed.
- The session waits on `enact ci --wait` for its newest delivery before
  finishing and runs Steward subagents on failure; emergencies and
  deadlocks are reported to the user instead of a Justiciar.

## Light runs (2026-10-30)

- The invocation keyword **light**, for `/enact` and `/enact-plan`, trades
  robustness for velocity. It drops Mason, Sage and Archivist work and caps
  review at one final Warden bead depending on every implementation bead,
  skipped when the whole run is one routine change. `review:inline` labels
  are ignored. Plans still get the stoppage audit, which unattended
  execution needs at any pace.
- The run records the mode as a `run:light` label on the run epic (or on an
  inline task prompt's beads). `bd create --parent` inherits parent labels,
  so follow-ups carry it for free, and workers learn the mode from the
  graph rather than from their spawn prompt.
- In an existing epic, unclaimed Mason, Sage, Archivist and Warden beads are
  closed as skipped rather than left open: an open gate child would block
  the next milestone and any open bead keeps the epic from closing. The
  final Warden covers what the skipped Wardens would have reviewed.
- Follow-ups in a light run are real bugs only, with no review of their
  own; the filer makes the final Warden depend on them so it stays last.
- No helper changes: the mode is policy applied by skills and role files.

## Clarifying task prompts (2026-11-01)

- `/enact` given a task prompt starts by asking the user a few clarifying
  questions, one at a time, as needed to understand its scope. A bead was
  already scoped when it was filed; a prompt is often a sentence, and the
  user is present at the start of an inline run but may not be later.

## Waiting on beads (2026-11-01)

- `enact wait <bead>...` blocks up to `--timeout` (540 s) until every named
  bead is closed and returns `closed`, or `waiting` with the open beads, so
  `/enact` can honor "start this once ps-myz closes". It syncs Beads each
  poll, like `ci --wait`, because another machine may close the bead.
- A dependency edge is not enough: `enact claim` ignores blockers, an epic
  loop stops as deadlocked when only outside work blocks it, and edges do
  not cross Beads graphs. The waiter runs from the project that owns the
  beads. An unknown ID fails at once instead of waiting forever.

## Worktree sweep (2026-11-02)

- playlist_survivors had collected 21 worktree directories (32 GB) because
  cleanup only ran on the happy path: a push from a clean tree, an explicit
  `worktree remove`, a CI check that exited normally. Leftovers were
  killed CI runs' trees, trees pushed with stray files, `bin`/`obj`
  directories a build server recreated after git deleted the tree, and
  agents' scratch logs.
- The slow-CI runner sweeps every time it takes its lock: per machine,
  after every push, and off every session's critical path. Under the lock
  no check is live, so every `ci-*` tree goes, including ones git left
  locked "initializing". `enact worktree prune` runs the same sweep by hand
  (for projects without slow CI), minus `ci-*` trees.
- An `enact/<bead>` tree goes only when its bead is closed and the tree has
  no uncommitted, untracked or unpublished (not on `origin/staging`) work.
  An unclosed bead, a branch outside `enact/` (hand-made, Codex) and a
  locked tree are never touched, nor are loose files.
- A directory under the root that git does not track goes after an hour
  untouched, unless it is hidden, holds registered trees (a hook's pool) or
  has its own `.git` (another repository, or a checkout that lost its
  registration and may still hold edits git can no longer report). Hooks
  must still leave registered git worktrees, as push and recovery already
  require. The hour keeps the sweep from racing a tree being created.
- `remove_worktree` deletes what a build server recreates while git deletes
  the tree, once git no longer tracks it; the sweep retries anything still
  in use. Push reports `kept_worktree` instead of silently keeping a tree
  with untracked files, and agents keep scratch output out of their
  worktree and the worktree root.

## Stale failures and load-sensitive checks (2026-10-30)

- A playlist_survivors postmortem found no failure spiral: the coalescing
  runner kept master within about one slow-CI cycle of staging (median 8
  minutes), and a single red episode explained the largest gaps. Pausing
  pushes while red or bisecting every batch would cost more throughput
  than it saves.
- Two of six Steward beads there were stale: a Steward pushed its repair
  and closed, then the runner finished an older tip without the repair and
  filed a new bead, whose Steward found it already fixed. A failure is now
  not filed when staging gained, after the failing tip, a commit recorded in
  `enact_commits` of a Steward bead for this OS. The runner's loop retests
  the newer tip and files or comments only if that fails too. The condition
  is a landed repair rather than "staging moved on", so a real failure is
  never deferred indefinitely by a steady stream of pushes.
- Half of the failures were timing gates tripping under the load of the
  agents sharing the machine. Warden reviews block tests whose verdict
  depends on machine speed, Masons file existing ones as important, and
  Stewards replace the timing dependence instead of loosening thresholds.
- A test that fails while the product behaves correctly is a false
  positive, which is strong evidence it is worthless. Stewards very
  seriously consider deleting such a test rather than repairing it, and
  repair it only when it guards behavior nothing else covers.

## Token economy (2026-11-07)

- With prompt caching, a session costs roughly its context size times its
  turns. The expensive part is each fresh session rediscovering the code it
  touches, paid again for every bead, Warden and cold reviewer, while the
  planner had already read that code. The changes below move knowledge
  forward instead of rediscovering it, without giving up fresh-context
  review.
- **Lanes.** A `lane:<name>` label groups related beads of one role that one
  session works in priority order, delivering and closing each on its own.
  `enact close --continue` claims the lane's next ready bead for the same
  session under the ownership lock, copying the run, vizier, routing and
  session metadata with a fresh reservation token, so the vizier counts it
  as in flight and the Marshal can route recovery to it. The scheduler
  starts no session on a lane bead while another bead of that lane is
  assigned, and neither `enact next` nor `enact status` lists a session for
  archiving while it holds an open bead. A worker that stops chaining just
  closes without `--continue`, and the rest of the lane goes to a fresh
  session. Lanes need no ordering edges, so they add no deadlock risk.
  Wardens are never in lanes. `/enact` accepts several bead IDs as a lane it
  works itself.
- **Context packets.** Bead descriptions name the files, helpers and tests
  to touch and what need not be read; the run epic holds a short code map;
  beads are sized by code locality rather than topic. The Archivist keeps a
  project code map current.
- **Handoffs and `enact brief`.** Workers leave a `Handoff:` comment before
  closing. `enact brief <bead>` returns, in one read, the bead with its
  comments, its epic chain, each blocking dependency's close reason,
  commits, touched files and latest handoff, and its lane. It replaces
  several verbose `bd show` calls at every session start; it does not sync,
  since `start` and `claim` just did.
- **Reviews.** One Warden per lane or milestone area by default, a per-bead
  Warden only for risky changes. Wardens and inline cold reviewers keep a
  fresh context for independence but read the touched files and what they
  call rather than the codebase. The inline cold review runs on a cheaper
  model unless the change is risky.
- **Tiers.** A `tier:<name>` label selects `[tiers.<name>.<provider>]`
  model settings in `.enact.toml`, overriding the role's. The vizier labels
  mechanical beads `tier:light`; new projects get `light = sonnet` for
  Claude. Project configuration counts as a user request for model
  settings, so Codex applies spawn `settings` too.
- **Lean shared protocol.** Recovery, takeover and emergency procedures
  moved from `common.md` to `roles/recovery.md`, read only when needed.

## Backlog sweep (2026-11-08)

- `/enact-sweep` triages stale beads and runs the survivors. A bead is
  stale when it is open, unassigned and outside every live run. Ownership
  decides, not age: a fresh bead under a dead run is stale, while an old
  bead queued in a live run is not. `bd stale` measures only age, so it
  would close queued work.
- Verdicts are done, obsolete, keep and human, each with evidence. Read-only
  subagents on the light tier triage large backlogs, and only the sweeper
  writes to Beads. Closes are reversible (`bd reopen`), so the invocation
  authorizes them without a confirmation step.
- Human beads are settled while the user is present, the same way as the
  stoppage audit, or deferred. An unattended run never starts on one.
- The run starts in the sweep's own turn, because Claude Desktop lets only
  a user-started turn start sessions. A scheduled task cannot launch a
  vizier. Kept beads move into one `Bead sweep <date>` epic and go to
  enact-plan as "several beads".

## Models by name (2026-11-10)

- `tier:opus` and `tier:sonnet` are built-in tiers: for the Claude provider
  they select that model with no entry in `.enact.toml`. A user asked a plan
  to "assign beads to opus or sonnet based on complexity, default opus", and
  the only way to pin the default was a project-level tier added for the
  occasion.
- A project's own `[tiers.<name>.<provider>]` still wins, so it can remap a
  built-in tier or map it for another provider. A provider with no mapping
  keeps its role's settings, as with any unknown tier.
- The vizier assigns every bead a named tier only when the invocation or
  AGENTS.md asks for models by name. Otherwise it labels mechanical beads
  `tier:light` and leaves the rest on the user's default, as before, so
  existing projects and runs do not change.
- The policy goes in the run epic, not on it as a label: children inherit an
  epic's labels, and a bead with two tier labels takes whichever is listed
  first.
- `tier:fable` joined them the same day. A user auditing a plan asked to
  "tag the biggest design challenges to fable": the beads whose design every
  later bead builds on. The vizier uses it only when asked, since the other
  two already cover "mechanical" and "everything else".

## Required models (2026-11-10)

- A `tier:fable` bead spawned by an Opus vizier ran on Opus:
  `start_session`'s `model` is silently ignored above the caller's own
  model. The user caught it from the session list.
- The vizier now starts such a child on a holding prompt, switches it with
  `set_session_model`, confirms with `get_session` and only then sends the
  Enact prompt. The holding turn is one trivial reply on the vizier's
  model; the worker's whole bead runs on the tier's.
- A Claude spawn action that names a model carries `model_required` to
  preserve these steps across compaction. The worker checks its model before
  claiming the reservation. The vizier blocks the run if it cannot set the
  required model.
- The Codex adapter passes configured model and effort at launch. Codex
  workers cannot reliably identify their own model from the prompt, so they
  claim their reservations without a model self-check.

## Machines on different OSes (2026-11-11)

- Beryl runs Enact on a Mac and a Windows PC. A Windows-only slow-CI failure
  sat unworked for hours: its Steward had no target, so a Mac vizier could
  take a failure it cannot reproduce, no vizier ran on the PC to take it,
  and the Mac session that pushed the commits got `passed` from `ci --wait`
  once the Mac's own run promoted master.
- A Steward goes first to a machine that can reproduce it: its
  `enact_ci_host`, or any machine on its `enact_ci_os`. After
  `STEWARD_HANDOFF_HOURS` (2) unclaimed, any machine's vizier may take it:
  the cause is often plain from the log, and an open Steward holds its runs.
  An explicit `enact_host` still wins, and Stewards without CI metadata
  route as before.
- `ci --wait` adds `other_os_stewards` to every result when another OS's
  open Steward covers the SHA. Its state is unchanged, since another OS
  failing never stopped this one promoting. `watch_ci` tells the session to
  repair it if the log makes the cause plain, and otherwise to comment its
  analysis and name the bead in its final reply.
- No timer runs slow CI. A machine tests staging only from its own pushes,
  viziers, waits and Marshal, so a peer's commits reach its tests when it
  next works, and each OS's failures are fixed as they appear. A scheduled
  poll (`enact ci --schedule`, added and removed the same day) spent the
  machine on suites nobody was waiting for.
- `enact push` fast-forwards the Enact checkout to its master first (only
  on master, clean, fast-forward; `ENACT_NO_SELF_UPDATE` turns it off), so
  machines without a vizier or Marshal pulling Enact do not drift. It takes
  effect from the next command.
- Promotion is unchanged: any machine's pass advances master. An OS quorum
  would hold master whenever one machine is asleep or off, and every
  project's master would then depend on its least available machine. A
  peer OS's failure is repaired through its Steward instead.

## Many sessions on one host (2026-11-11)

- A push holds the ownership lock only for its bookkeeping: the first
  ownership check, then the final check, the push to staging and the delivery
  record. It once held it through the rebase and the fast check, so with a
  dozen sessions on one machine every claim, close, meta and sync waited out
  other sessions' checks and failed "ownership operation busy" after 180 s,
  and a plain `enact sync` took 100 s of wall time for 6 s of work. The remote
  still arbitrates pushes: one that loses the race is rejected and
  re-integrates. Two sessions may now find one delivery receipt; the first to
  lock it finishes it and the other skips it.
- Sync compacts an embedded Beads store whose Dolt journal passed 64 MiB with
  `dolt gc`, which rewrites storage but not history. Dolt never compacts the
  journal itself: a week of writes grew Beryl's to 410 MB, and each bd
  command, which opens the store afresh, spent about a second reading it
  (0.2 s after the gc). `bd gc` is not used: it also squashes commits that the
  remote and other clones already have.
- After a push loses the race to staging and re-integrates, its fast check
  runs again with `ENACT_CHECKED` naming the commit the last pass checked.
  At about twenty pushes an hour a two-minute check often loses the race, and
  rerunning all of it repeated work the new commits could not affect: a
  project's check may compare the trees and rerun only what they reach.

## Waiting on the user (2026-11-12)

- A turn that ends on a question or decision for the user ends on 🔍, not
  ⛔. A session titled ⛔ while it only waited for an answer read as a
  failure to the user who was meant to answer it.
- 🔍 applies only where a user is present: the vizier's pre-flight and
  stoppage audit, inline runs, sweeps and ad hoc sessions following
  AGENTS.md. Spawned workers and the Justiciar never ask, so they keep ✅
  and ⛔.

## Session pool and background waits (2026-11-12)

- Claude Desktop now caps a session at ten sessions started over its life,
  and still lets only a user-started turn start one. A vizier that started
  one session per bead stopped after ten beads.
- A live probe from turns woken by a background task (no user, no
  side-session exception) showed `start_session` refused, while
  `clear_session`, `set_session_model` and `send_message` on an idle child
  worked, after thirteen automatic wakes in a row. The cleared child saw no
  earlier messages and ran on the newly set model.
- So a Claude Desktop vizier starts a fixed pool (`parallel + 1`, the spare
  for a Justiciar, at most ten) in its first turn and hands each bead to an
  idle member: clear, set model and effort, send the spawn prompt. It never
  starts sessions after pre-flight. A lost member is replaced only in a
  turn the user starts (`action: pool-empty` when none remain).
- Because nothing after pre-flight needs a user-started turn, the vizier no
  longer holds one turn open with foreground `enact next` calls. It runs
  `enact next` in the background and ends its turn; the exit notification
  wakes it. Remote Control messages and session reports no longer break a
  run. Codex keeps its foreground loop.
- The pool is machine-local state per vizier (`pool-<vizier>.json`:
  members, sessions started, members resting). Which members are busy comes
  from the graph: an open bead bound by `enact_session` or reserved by
  `enact_pool_session`, or the Justiciar lock's `pool_session`. The
  reservation names the member, so its worker may run `enact start` before
  the vizier records the binding, and no other session can take it.
- A member that refuses `clear_session` (still finishing its turn, pinned,
  open on screen) rests for a minute; an archived or missing one is
  dropped. Members rotate so work spreads over the pool. Members are
  archived only when the run is done.
- `enact next` holds a per-vizier lock, since two overlapping background
  waits could hand out the same member.
- The scheduler only reads pool state for Claude. Older Claude viziers
  without a pool keep the foreground loop. Once a Claude vizier has
  started a pool it never falls back to `start_session`. Codex keeps fresh
  chats per spawn (including Justiciar), per-chat launch settings, and the
  540-second foreground wait. The Codex Desktop tools can send a follow-up
  with a model override but expose no operation equivalent to Claude's
  `clear_session`. Fresh chats give each assignment its own context and
  history.

## Speculative defaults instead of questions (2026-11-13)

- A Beryl run stalled four times on questions the vizier put to the user:
  a reported-only benchmark pinned to a Windows host with no vizier, which
  held the epic's final beads; ten pilot beads failing on one shared test
  fault; a single-chain graph that wanted restructuring for parallelism;
  and a performance budget measured on a loaded machine that kept filing
  follow-ups. The user's rule: orchestration completes work speculatively
  by relaxing blocking constraints, so none of these should reach them.
- Once the loop starts nobody asks the user. Whoever holds the choice (a
  worker, a Warden, the Justiciar on a deadlock) applies the least-blocking
  default from `common.md` "Speculative defaults" and records it on the bead
  and the run epic. The vizier still never reads beads; its only graph
  action stays a Justiciar request.
- The stoppage audit asks the user only about human-only steps (access,
  credentials, payments, irreversible outward actions, outside people).
  Agent-decidable points become recorded defaults in the run epic.
- Planners avoid serializing edges: edges go on the bead that needs a
  result rather than on its epic, reported-only beads get no dependents,
  pins are reserved for beads that cannot run elsewhere and gate nothing,
  and decomposition beads sit under the run epic with no dependencies,
  since the milestone epic keeps the beads they file gated.
- `enact next` flags a pin nobody serves: when nothing else can proceed and
  every ready bead it skipped is pinned to a host or provider with no vizier
  route registered on the run, it returns `deadlock` with a `stranded` list
  instead of waiting for the six-hour stale check. Running viziers already
  handle `deadlock` by requesting a Justiciar, so the change is compatible.
  Registration is the liveness proxy: a registered vizier that died still
  falls back to the stale check.

## Lane and tier defaults (2026-11-13)

- A user asked how nine beads filed outside a run should be executed to
  spend the fewest tokens. The answer was already Enact's lane design, but
  `/enact-plan` applied it only to beads it filed itself: given an existing
  epic with children it filed nothing and ran one session per bead, or one
  session per lane however the lanes had been labelled.
- The reasoning, now stated in the skill: a session pays once to learn its
  code and on every turn to re-read its whole context. One session per bead
  relearns shared code. One session for many beads carries finished beads
  through every later turn and runs all of them on the costliest bead's
  model. The second cost is the larger, so lanes stay at two to four beads.
- The vizier therefore runs a "lane and tier pass" over the unclaimed beads
  of any epic or bead list it is given, using the files their descriptions
  name. It does not read code for this, so the pass stays inside pre-flight
  and the loop is unchanged.
- A lane has one tier. The session keeps the model its first bead started
  on, so the nine beads above, labelled by shared code alone, would have run
  Sonnet-tier beads on Opus or an Opus-tier design on Sonnet. A bead of
  another tier leaves the lane and takes an ordering edge where it edits the
  same code non-additively.
- A bead likely to fill a session (a design, a rewrite) stays out of lanes,
  for the same reason a worker with a heavy context closes without
  `--continue`.
- Lane labels are not put on epics, like tier labels: children inherit an
  epic's labels, which would join unrelated beads into one lane.
- No helper change. The scheduler still treats a lane as "one session at a
  time"; the rules only change which beads share a label.
