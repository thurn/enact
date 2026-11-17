# Enact design decisions

## Marshal removed (2026-11-11)

The hourly scheduled Marshal sweep is gone. An audit of about 75 hourly
beryl sweeps over three days found it never nudged a worker, resumed a
vizier, released a reservation or started CI. Its only output was
archiving three finished chats (two more archive attempts were refused for
live work) and marking already-archived chats on their tickets, at 1-4
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
and tickets writes and unmodified older helpers remain outside that protocol.

- Audit against local project chats in the current desktop app, preserving
  Claude Desktop support. Identify supported tools and execution location
  explicitly rather than treating product names as capability guarantees.
- Invoking `/enact-plan` (`$enact-plan` in Codex) authorizes creating,
  messaging and archiving its role chats. Worker prompts do not restate it
  (2026-11-13): it changed no behaviour.
- A run finishes only when its delivered commits have passed slow CI and
  reached `master`, including any required Steward fixes. Workers may still
  close their tickets after pushing. ticket closure alone is not run completion.
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
  hang (a stuck exec, a server never stopped) files a Steward ticket instead of
  holding `master` back indefinitely. Budgets stay the project's to enforce.
- `[checks] slow_min_gap` (seconds, default 0) is the least time between the end
  of one slow-CI run and the start of the next. The runner waits it out while
  holding its lock, then refetches `staging`, so at a high commit rate the
  commits that arrive meanwhile share one run instead of causing more runs. A
  project caps the CPU a run may hold in its own check command; Enact only
  bounds how often runs start.

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
  - Fail: at most one open P0 Steward ticket per project per OS, unparented;
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

## Ticket operations

- The ticket service owns the task graph and coordination. Each checkout
  configures its project and service URL in `.enact.toml`.
- `enact claim` atomically claims a ticket. The loser of a cross-machine race
  backs off. `enact push` appends `enact_commits` and repairs delivery
  receipts. `enact close` publishes the closure immediately.
- Role is a `role:<name>` label. Unlabeled tickets are executor tasks.
- `enact_host` and `enact_provider` restrict which vizier may select work.
  `enact_vizier`, `enact_session`, `enact_archived`, and `enact_commits`
  retain the execution record.

## Vizier

- One user-invoked skill that scales with its input, `/enact-plan <input>
  [parallelism]`. One entry point keeps small tasks lightweight.
  - ticket: execute it. An epic is the run (joining another vizier's run needs
    nothing special); a plain ticket is wrapped in a new epic, since follow-ups
    and `enact next` work by epic.
  - Plan document: full stoppage audit with the user, full decomposition.
  - Task prompt: smallest graph, one executor ticket, no milestones or
    gates; a prompt describing a large work item is treated as a plan.
    Review is none, inline or a Warden ticket (below).
- Pre-flight: audit stoppage risks and resolve each with the user up front;
  refuse to start if any remain. Require a non-prompting permission mode.
  `git pull --ff-only` in ~/enact.
- Inline review: a `review:inline` label requires one fresh-context
  cold-review subagent before pushing. Use a Warden ticket for large or risky
  changes or when the user requests one.
- Follow-ups are essential-only: real bugs, missing tests for risky behavior,
  imminent design problems or broken tooling. Each ticket costs a session.
  Comment issues on a not-yet-started Warden that will review the code
  rather than duplicating its work; a Warden closes tickets its fixes cover.
- `enact install` removes dangling links to retired skills
  (`enact-intake`).
- Decomposition: full graph up front or rolling milestones, at the vizier's
  discretion; "decompose milestone N" tickets go to the executor role. Agents
  re-validate stale tickets against current code.
- Warden default: one Warden ticket per lane or milestone area, per ticket only
  for risky changes (see Token economy); code-dependent work depends on the
  Warden ticket.
- Milestones are child epics. Mason/Sage audits are "gate" epics blocking the
  next milestone epic (tickets only lets epics block epics; a blocked epic hides
  its children from `tk ready`). Important findings become children of the
  gate; `enact next` closes eligible epics.
- Loop runs inside the user's original turn (Claude Desktop refuses
  `start_session` on turns started by automated events or other sessions):
  `enact next --wait 540` (unique vizier reservation, returns an action),
  spawn `start_session`/`create_thread`, record the ready `enact_session`,
  and repeat. Resume one yielded process through bounded tool waits; never
  create competing scheduler processes. Output restates the loop so it
  survives compaction.
- Selection scope: ready descendants of the epic plus unparented Steward tickets.
  Capacity = open tickets with `enact_vizier=<me>`; default parallelism 1.
  Parallelism comes only from the invocation or a later user message, never
  from ticket, epic or plan text: the same graph can run at different widths,
  and a stored level would silently override the user. Viziers do not write
  it into tickets and ignore it when present; `run register` metadata records
  the invocation's value for recovery only.
- The vizier archives sessions of closed tickets reported by `enact next`.
- All spawns go through the vizier, including the Justiciar
  (`enact justiciar request`); the requesting host's vizier spawns it.
- Termination: `enact next` reports done only after epic closure, required
  Stewards and promotion of every run delivery to green master. Peer claims
  and pending validation keep the run alive. A genuine deadlock requests a
  Justiciar; paused and blocked states retain unmet acceptance.
- Spawn prompt names `roles/common.md` and `roles/<role>.md` by absolute path,
  plus the ticket and epic, and asks for both reads and `enact start` in the
  first message: each extra request rereads the whole context (2026-11-15:
  runs of read-only requests were about a quarter of Beryl's weekly spend).
  The files stay out of the prompt itself because the vizier would have to
  write them out as output tokens on every spawn. Optional per-role model/effort from `.enact.toml`.

## Justiciar

- One lock per machine and project: `refs/enact/justiciars/<host>` on origin,
  created with a create-only push; the commit records host, session, time,
  reason (see [One Justiciar per machine](#one-justiciar-per-machine-2026-11-13)).
  Holder is printed to anyone who tries to acquire. Deleted on exit; a session
  breaks it only after a routed lookup confirms the holder archived or
  absent. Age, silence and lookup failures do not prove abandonment.

## Marshal

Removed on 2026-11-11; see [Marshal removed](#marshal-removed-2026-11-11).
`enact status` still lists only tickets spawned by the caller's host app: a
Claude session cannot see Codex threads. Legacy routing remains visible for
inspection; uncertainty never authorizes releasing ownership.

## Sage

- May edit ~/enact, but only through Enact's own flow: a worktree from
  `origin/staging` and `enact push`. The Enact repository is itself set up
  with Enact (tickets prefix `en`), so its slow CI advances its `master`, which
  is what every machine pulls.

## Archivist

- One Archivist ticket ends every plan run (small task-prompt runs get none).
  It deletes the plan document if the repository tracks it, fixes links that
  would dangle, and edits the persistent docs to describe the shipped state
  only, minimally (YAGNI), within the project's existing doc architecture.
- `enact next` holds a `role:archivist` ticket until it is the last open
  non-epic ticket in the run epic. Planned dependency edges could not cover
  follow-ups filed mid-run, and tasks cannot depend on epics in tickets.
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
- `.enact.toml`: fast/slow check commands, `[worktree]` root plus optional
  full-replacement `create`/`remove` hooks (env: `ENACT_WORKTREE_PATH`,
  `ENACT_BRANCH`, `ENACT_BASE_SHA`, `ENACT_REPO`), optional
  `[roles.<role>]` model/effort per host.
- Worktree slots (2026-11-13): `[worktree] slots = N` gives ticket worktrees N
  fixed directories, `slot-01` to `slot-NN`, instead of a new directory per
  ticket. Cargo's fingerprints include the worktree path, so a reused path
  skips recompiling the workspace crates; on Beryl a build plus a test build
  took 115 s in a reused path against 224 s in a new one. `worktree create`
  hands a ticket the first slot parked on a detached HEAD, resets it and checks
  the ticket's branch out; ignored files stay. Releasing (push, `worktree
  remove`, the sweep) detaches the slot at staging, deletes the branch and
  keeps the directory, without running the `remove` hook. The ticket's
  `enact_worktree` pointer names the slot, and a slot belongs to the ticket
  whose `enact_branch` is checked out in it. Default 0 keeps the old
  behavior. Slots never shrink the build directory, so a project that enables
  them must prune it itself.
- Ad hoc trees in slots (2026-11-15): `enact worktree create <name>` without a
  ticket takes a slot too, checking out `enact/<name>`; `create` and `remove`
  with the same name find the slot by that branch. Of the free slots, the one
  parked at the newest staging tip is taken, because its build is the closest.
  Beryl's fast check had its Rust test step killed at the 180 s limit in 48 of
  52 runs over a day, because each change ran in a new path where rustc
  ignored the incremental caches; most of those trees were ad hoc. A recovery
  record pointing at a slot now matches a tree only on the same branch, since
  later occupants share the path.
- Stopped rebases keep their slot (2026-11-15): a slot whose git dir holds
  `rebase-merge`, `rebase-apply`, `CHERRY_PICK_HEAD`, `MERGE_HEAD` or
  `REVERT_HEAD`, or that has unmerged paths, is not free even though its HEAD
  is detached. Its owner is read from the rebase's `head-name` file, since
  `rev-parse --abbrev-ref HEAD` prints `HEAD` until the rebase ends. Before
  this, a push stopped on rebase conflicts left its slot looking parked, and
  three times in one day on Beryl another session checked its own branch out
  there within a minute, discarding the conflict resolution in progress. A
  slot abandoned mid-rebase stays busy until someone aborts or finishes it;
  the pool then makes another slot or a named directory, which costs a cold
  build, never someone's work.
- Slots that hold work are never handed out (2026-11-15): a detached slot is
  also busy when it has uncommitted or untracked files, or when a ticket that is
  not closed records it (`enact_worktree` on this host) and its `enact_branch`
  still exists, since release deletes that branch. `claim_slot` no longer
  resets or cleans the slot it takes, because a free slot is already clean.
  `enact push` exits 2 when the tree's branch is not the ticket's
  `enact_branch`. Before this, after an app restart on Beryl, a slot whose
  owner was mid-rebase was handed to a second ticket (the rebase guard had not
  reached `master` yet); the owner's rebase then finished on the wrong branch
  and an amend rewrote the other ticket's commit.
- Only released slots are free (2026-11-15): `release_slot` writes
  `enact-released` in the slot's git dir with the commit it parked the slot
  at, and a slot is free only while that file names its current HEAD;
  `claim_slot` deletes the file when it takes the slot and logs why it
  skipped each busy one. A detached HEAD alone no longer counts, since a
  bisect or a manual `git checkout --detach` by the session still working
  there detaches it too. On Beryl a session that had pushed kept bisecting
  in its old slot after Enact handed it on, moving the new owner's HEAD and
  deleting their uncommitted files as strays. Slots released before the
  marker existed count as released while their HEAD reflog ends with
  release's own reset and staging checkout, so live pools keep working.
  Push and `worktree remove` print `slot_released`, saying the path may
  now belong to another session and must not be used again.
- Reused slow-CI tree (2026-11-14): `[checks] slow_tree = "<name>"` makes
  every slow-CI run check its tip out, detached, in one worktree of that name
  instead of a new `ci-<tip>` tree that is removed afterwards. The reset,
  checkout and `clean -fd` keep ignored files, so build directories carry
  over. Rust's incremental caches are bound to the checkout's absolute path:
  on Beryl, rebuilding the tests after a one-function change to the base
  crate took 18 CPU-seconds in the tree that built them and 544 in a copy at
  another path. The tree is never removed after a run (its processes are
  still reaped), so the `remove` hook does not run for it; a project that
  shares its build directory with other trees must save it from the slow
  check itself. The sweep keeps it. Default unset keeps the old behavior.
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
  claim close next worktree justiciar install setup`, plus ownership,
  run-control, status and recovery commands shown by `--help`.
- Skills linked per machine into user-level skill dirs (symlinks; junctions on
  Windows). `enact-setup` per project: ticket project configuration, machine registration, staging
  branch, and AGENTS.md instructions.
- Installed skills: `enact-plan` (the vizier), `enact` (inline),
  `enact-setup` `enact-sweep` (backlog sweep), and `status-dashboard` (the live ticket UI). Role files stay in `skills/enact/roles/` so spawn prompts
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
  `enact worktree create` and `enact push` without a ticket also print a
  `title` notice, and `watch_ci` names the CI-wait emojis.

## Implementation contracts

- Spawned sessions take their ticket with `enact start`, which moves the
  assignee from a unique vizier reservation to their own session ID, fenced
  by the reservation token.
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
- Validate with stdlib unittest black-box tests (real Git clones and the Worker simulator) and bounded desktop smoke checks. Deliver through
  `enact worktree create`, commit and `enact push --ticket`; slow CI alone
  advances master. Record tested hosts and remaining limits explicitly.

## Durable delivery and final promotion (2026-10-29)

- Before pushing staging, save a local delivery receipt in the git common
  directory. It contains the ticket, run epics, delivered commits, previous
  commit history and CI intent. Only retire it after confirmed remote
  ancestry, published metadata and CI startup. `push`, `next` and `status`
  recover confirmed receipts; a successful push followed by a failed sync
  can be retried without new commits. Receipts survive worktree removal.
- `enact_commits` is an ordered, deduplicated history, including repeated
  pushes. Sync recovery unions and verifies metadata after remote-wins
  merges. CI launches an immutable helper snapshot in the git common
  directory, so self-delivery cannot delete the child's entry point.
- Assignments record `enact_run`; deliveries and CI failure tickets record
  `enact_runs` (multiple runs may share a Steward). Repeated CI failures
  retain affected runs. Older tickets infer run membership from the parent
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
  already owns the ticket can resume. Same-host operations serialize their
  check/write/sync sequence with the existing portable OS lock. Remote tickets
  conflicts retain remote-wins semantics and published ownership is checked.
- Close, metadata changes, release, worktree operations and push check current
  ownership after sync. Push infers a ticket from its tree even when `--bead`
  is omitted, and checks again immediately before publishing. Ownership is a
  cooperative helper protocol: raw git/tickets writes and unmodified older
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
  survive. Inspect against staging before continuing. Release pushes the
  worktree's commits that are not on staging to its branch on the remote,
  with a lease so it never overwrites another host's commits, before it
  gives up ownership; a rejected push fails the release and keeps the
  assignment (2026-11-16: two Beryl tickets released on one host were redone
  from scratch on another). A remote replacement starts from that pushed
  branch and keeps the original recovery record, including its original
  host; uncommitted files cannot cross machines and are only flagged
  `uncommitted`. Delivery deletes the pushed branch, since its rebased
  commits never become ancestors of staging.
- Worktree creation records its host/path/branch and can be retried. A
  never-assigned ticket is claimed for direct CLI work; released tickets must be
  reassigned first. Explicit removal refuses dirty or unpublished work,
  including branch commits whose checkout directory has disappeared, and
  successful delivery retains a tree with untracked artifacts. Durable
  delivery receipt replay may append factual already-pushed commits after
  reassignment; it does not authorize new publication by an old worker.
- Vizier routing writes and reservation releases require the token from the
  original spawn action. A vizier may reserve the same ticket repeatedly, so
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
  of tickets issue status. Claims, reservation starts, scheduling and Marshal
  recovery check holds on run links and ancestors. Workers already running
  may push and close under a hold. Automatic epic closure excludes held
  epics and their descendants; unmet acceptance remains open. Block a run
  only after Justiciar recovery cannot resolve missing access or a human
  decision. Do not replace impossible requirements with endless retries.
- Only `run resume --explicit --reason` clears a hold and creates a new
  recovery epoch. The flag attests an explicit user instruction, not a
  permission available to automatic recovery. Raw tickets writes and old
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
owner checks and release accept both as aliases, using whichever the ticket
records. The default actor stays the CLI ID to preserve live claims.
`ENACT_SESSION`, when set, is the only accepted identity.

## Desktop adapter and installation (2026-10-29)

- The adapter is prompt text using the tools supplied by the owning app;
  no Python wrapper for desktop APIs is needed. Codex selects a registered
  project by full path and host, creates local top-level chats, maps an
  authorized effort preference to `thinking`, and records ready chat IDs.
  Pending client IDs never become ownership identities. Defaults remain
  implicit unless the user requested model settings; unavailable preferred
  settings fall back with a ticket comment, while required models block.
- Pre-flight verifies tools and local access before reservation. Long helper
  waits stay in one process with bounded continuation calls and mid-run
  steering. The loop retains created-chat receipts for its final UI output.
- Marshal discovery uses exact identity plus verified legacy aliases. Its
  saved ID belongs in the run's ticket comments; creation, migration and
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
desktop tool host is `local`, distinct from the OS hostname. Run tickets
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

## Slow-check runtime

The fast suite uses policy fixtures for scheduling and refusal matrices.
Slow CI also runs real Git clones with the local Worker simulator, native
CLI protocol checks, registration, backup and restore, and coordinator
crash recovery. The fixtures isolate authentication and ticket storage from
production. Run the commands in `.enact.toml` to exercise the configured
fast and slow suites.

## Slow-CI ownership outside runs (2026-10-30)

- The runner is a detached process and cannot start chats, so a Steward
  ticket filed while no vizier loops would sit unworked. A plain prompt in an
  Enact project therefore owns its own slow CI.
- `enact push` prints `watch_ci` when slow CI started, the pushed ticket (if
  any) was not reserved by a vizier, and no registered run on any machine is
  unfinished and unheld. A vizier's worker never waits: its run owns repair,
  including after a pause.
- `enact ci --wait <sha>` blocks up to `--timeout` (540 s) and returns
  `passed` once the SHA reaches master, `failed` with the Steward tickets whose
  failing range covers it (unassigned or held by this session), or
  `waiting`. Stewards held by another session count as in repair, and
  only this OS's Stewards end the wait: any machine's pass promotes master. While
  waiting it restarts a dead runner when staging is untested.
- On `failed` the session claims the Steward ticket and follows the Steward
  role, then watches its repair the same way. Instructions live in the push
  output, so existing project AGENTS.md files need no change.

## Inline skill (2026-10-30)

- `/enact` became inline execution; the vizier moved to `/enact-plan`.
  Many tasks need no unattended fleet, and spawning sessions, a Marshal
  and per-session titles costs more than the work.
- `/enact` never starts sessions, scheduled tasks or a Justiciar. A plain
  ticket or task prompt is implemented by the session itself; an epic runs
  each ticket in a fresh subagent, sequential by default so the title names
  the working role. Plan documents and large prompts go to `/enact-plan`.
- Review follows the vizier's policy: Executor only for routine changes,
  Executor plus a Warden subagent on its own ticket for large or risky ones.
  Subagents cannot start subagents, so an Executor subagent's
  `review:inline` becomes a Warden after its push.
- Mason, Sage and Archivist run only for tickets an epic already contains;
  the inline skill never files them.
- Subagents share the orchestrator's session ID: it claims with
  `enact claim`, they push and close. No helper changes were needed.
- The session waits on `enact ci --wait` for its newest delivery before
  finishing and runs Steward subagents on failure; emergencies and
  deadlocks are reported to the user instead of a Justiciar.

## Light runs (2026-10-30)

- The invocation keyword **light**, for `/enact` and `/enact-plan`, trades
  robustness for velocity. It drops Mason, Sage and Archivist work and caps
  review at one final Warden ticket depending on every implementation ticket,
  skipped when the whole run is one routine change. `review:inline` labels
  are ignored. Plans still get the stoppage audit, which unattended
  execution needs at any pace.
- The run records the mode as a `run:light` label on the run epic (or on an
  inline task prompt's tickets). `tk create --parent` inherits parent labels,
  so follow-ups carry it for free, and workers learn the mode from the
  graph rather than from their spawn prompt.
- In an existing epic, unclaimed Mason, Sage, Archivist and Warden tickets are
  closed as skipped rather than left open: an open gate child would block
  the next milestone and any open ticket keeps the epic from closing. The
  final Warden covers what the skipped Wardens would have reviewed.
- Follow-ups in a light run are real bugs only, with no review of their
  own; the filer makes the final Warden depend on them so it stays last.
- No helper changes: the mode is policy applied by skills and role files.

## Clarifying task prompts (2026-11-01)

- `/enact` given a task prompt starts by asking the user a few clarifying
  questions, one at a time, as needed to understand its scope. A ticket was
  already scoped when it was filed; a prompt is often a sentence, and the
  user is present at the start of an inline run but may not be later.

## Waiting on tickets (2026-11-01)

- `enact wait <ticket>...` blocks up to `--timeout` (540 s) until every named
  ticket is closed and returns `closed`, or `waiting` with the open tickets, so
  `/enact` can honor "start this once ps-myz closes". It syncs tickets each
  poll, like `ci --wait`, because another machine may close the ticket.
- A dependency edge is not enough: `enact claim` ignores blockers, an epic
  loop stops as deadlocked when only outside work blocks it, and edges do
  not cross tickets graphs. The waiter runs from the project that owns the
  tickets. An unknown ID fails at once instead of waiting forever.

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
- An `enact/<ticket>` tree goes only when its ticket is closed and the tree has
  no uncommitted, untracked or unpublished (not on `origin/staging`) work.
  An unclosed ticket, a branch outside `enact/` (hand-made, Codex) and a
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
- Two of six Steward tickets there were stale: a Steward pushed its repair
  and closed, then the runner finished an older tip without the repair and
  filed a new ticket, whose Steward found it already fixed. A failure is now
  not filed when staging gained, after the failing tip, a commit recorded in
  `enact_commits` of a Steward ticket for this OS. The runner's loop retests
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
  touches, paid again for every ticket, Warden and cold reviewer, while the
  planner had already read that code. The changes below move knowledge
  forward instead of rediscovering it, without giving up fresh-context
  review.
- **Lanes.** A `lane:<name>` label groups related tickets of one role that one
  session works in priority order, delivering and closing each on its own.
  `enact close --continue` claims the lane's next ready ticket for the same
  session under the ownership lock, copying the run, vizier, routing and
  session metadata with a fresh reservation token, so the vizier counts it
  as in flight and the Marshal can route recovery to it. The scheduler
  starts no session on a lane ticket while another ticket of that lane is
  assigned, and neither `enact next` nor `enact status` lists a session for
  archiving while it holds an open ticket. A worker that stops chaining just
  closes without `--continue`, and the rest of the lane goes to a fresh
  session. Lanes need no ordering edges, so they add no deadlock risk.
  Wardens are never in lanes. `/enact` accepts several ticket IDs as a lane it
  works itself.
- **Context packets.** ticket descriptions name the files, helpers and tests
  to touch and what need not be read; the run epic holds a short code map;
  tickets are sized by code locality rather than topic. The Archivist keeps a
  project code map current.
- **Handoffs and `enact brief`.** Workers leave a `Handoff:` comment before
  closing. `enact brief <ticket>` returns, in one read, the ticket with its
  comments, its epic chain, each blocking dependency's close reason,
  commits, touched files and latest handoff, and its lane. It replaces
  several verbose `tk show` calls at every session start; it does not sync,
  since `start` and `claim` just did.
- **Reviews.** One Warden per lane or milestone area by default, a per-ticket
  Warden only for risky changes. Wardens and inline cold reviewers keep a
  fresh context for independence but read the touched files and what they
  call rather than the codebase. The inline cold review runs on a cheaper
  model unless the change is risky.
- **Tiers.** A `tier:<name>` label selects `[tiers.<name>.<provider>]`
  model settings in `.enact.toml`, overriding the role's. The vizier labels
  mechanical tickets `tier:light`; new projects get `light = sonnet` for
  Claude. Project configuration counts as a user request for model
  settings, so Codex applies spawn `settings` too.
- **Lean shared protocol.** Recovery, takeover and emergency procedures
  moved from `common.md` to `roles/recovery.md`, read only when needed.

## Backlog sweep (2026-11-08)

- `/enact-sweep` triages stale tickets and runs the survivors. A ticket is
  stale when it is open, unassigned and outside every live run. Ownership
  decides, not age: a fresh ticket under a dead run is stale, while an old
  ticket queued in a live run is not. An age-only query measures only age, so it
  would close queued work.
- Verdicts are done, obsolete, keep and human, each with evidence. Read-only
  subagents on the light tier triage large backlogs, and only the sweeper
  writes to tickets. Closes are reversible (`tk reopen`), so the invocation
  authorizes them without a confirmation step.
- Human tickets are settled while the user is present, the same way as the
  stoppage audit, or deferred. An unattended run never starts on one.
- The run starts in the sweep's own turn, because Claude Desktop lets only
  a user-started turn start sessions. A scheduled task cannot launch a
  vizier. Kept tickets move into one `Bead sweep <date>` epic and go to
  enact-plan as "several tickets".

## Models by name (2026-11-10)

- `tier:opus` and `tier:sonnet` are built-in tiers: for the Claude provider
  they select that model with no entry in `.enact.toml`. A user asked a plan
  to "assign tickets to opus or sonnet based on complexity, default opus", and
  the only way to pin the default was a project-level tier added for the
  occasion.
- A project's own `[tiers.<name>.<provider>]` still wins, so it can remap a
  built-in tier or map it for another provider. A provider with no mapping
  keeps its role's settings, as with any unknown tier.
- The vizier assigns every ticket a named tier only when the invocation or
  AGENTS.md asks for models by name. Otherwise it labels mechanical tickets
  `tier:light` and leaves the rest on the user's default, as before, so
  existing projects and runs do not change.
- The policy goes in the run epic, not on it as a label: children inherit an
  epic's labels, and a ticket with two tier labels takes whichever is listed
  first.
- `tier:fable` joined them the same day. A user auditing a plan asked to
  "tag the biggest design challenges to fable": the tickets whose design every
  later ticket builds on. The vizier uses it only when asked, since the other
  two already cover "mechanical" and "everything else".

## Required models (2026-11-10)

- A `tier:fable` ticket spawned by an Opus vizier ran on Opus:
  `start_session`'s `model` is silently ignored above the caller's own
  model. The user caught it from the session list.
- The vizier now starts such a child on a holding prompt, switches it with
  `set_session_model`, confirms with `get_session` and only then sends the
  Enact prompt. The holding turn is one trivial reply on the vizier's
  model; the worker's whole ticket runs on the tier's.
- A Claude spawn action that names a model carries `model_required` to
  preserve these steps across compaction. The vizier blocks the run if it
  cannot set the required model. Workers no longer self-check their model
  (2026-11-13): the vizier's `get_session` confirmation suffices, and the
  check cost every prompt a paragraph. Spawn prompts likewise drop the
  restated role-skill and unattended notices, which `common.md` already
  carries, and pool members get a fixed one-line `background`.
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
  route as before. (Superseded on 2026-11-13: see "Vizier leases".)
- `ci --wait` adds `other_os_stewards` to every result when another OS's
  open Steward covers the SHA. Its state is unchanged, since another OS
  failing never stopped this one promoting. `watch_ci` tells the session to
  repair it if the log makes the cause plain, and otherwise to comment its
  analysis and name the ticket in its final reply.
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

## Concurrent delivery

Ticket mutations use atomic batches and preconditions. Git arbitrates
staging pushes. A rejected push re-integrates and repeats the fast check,
with `ENACT_CHECKED` naming the commit that passed previously so the
project can restrict its checks to the additional changes.

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
  one session per ticket stopped after ten tickets.
- A live probe from turns woken by a background task (no user, no
  side-session exception) showed `start_session` refused, while
  `clear_session`, `set_session_model` and `send_message` on an idle child
  worked, after thirteen automatic wakes in a row. The cleared child saw no
  earlier messages and ran on the newly set model.
- So a Claude Desktop vizier starts a fixed pool (`parallel + 1`, the spare
  for a Justiciar, at most ten) in its first turn and hands each ticket to an
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
  from the graph: an open ticket bound by `enact_session` or reserved by
  `enact_pool_session`, or the Justiciar lock's `pool_session`. The
  reservation names the member, so its worker may run `enact start` before
  the vizier records the binding, and no other session can take it.
- A member that refuses `clear_session` (still finishing its turn, pinned,
  open on screen) rests for a minute; an archived or missing one is
  dropped. Members rotate so work spreads over the pool. Members are
  archived only when the run is done.
- Clear before changing the model (2026-11-13): in a Beryl run two of four
  members could never be cleared again ("still has live work") while idle.
  The vizier had batched `clear_session` with `set_session_model`, so when
  a clear was refused the model change still applied to a session holding
  its conversation. Tested with throwaway sessions: such a model change
  waits for the session's next turn and blocks clearing until then; a
  model change after a clear, an effort change, and Remote Control do not.
  So hand-outs run one call at a time, change the model only after a
  successful clear, and a stuck member is freed by sending it a one-line
  message and clearing after that turn.
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

## Codex event waits (2026-11-13)

- A Beryl Codex vizier spent many model continuations holding a foreground
  `enact next --wait 540` call while its worker chat ran. The helper already
  blocked, but Codex's bounded command waits woke the model about once a
  minute; repeated status narration added tokens without changing work.
- `enact next` now includes `inflight_threads` for a Codex vizier's bound,
  open tickets. It reads these IDs from the graph already fetched for
  scheduling. Claude results and the Claude background-command loop are
  unchanged. The Codex reminder describes its event wait after compaction.
- The Codex vizier makes an immediate `enact next --wait 0` scheduling pass.
  On `wait` with active bound chats, it uses `wait_threads` with cursors for
  up to five minutes and then makes another immediate scheduling pass. The
  host wakes it when a worker turn ends or needs attention; the timeout
  catches ticket, remote-host and CI changes and renews the 20-minute local
  vizier lease. At most eight chats fit one wait; rotate when there are more.
- A worker can close a ticket before its turn ends. The next dependent spawn
  may then lag by up to five minutes. A chat that has already completed but
  still owns an open ticket cannot wake another event wait. With no active
  bound chat, Codex falls back to `enact next --wait 540`, preserving CI and
  remote-host progress and existing recovery checks. `enact next` remains
  the only authority for claiming, spawning and archiving work.

## Speculative defaults instead of questions (2026-11-13)

- A Beryl run stalled four times on questions the vizier put to the user:
  a reported-only benchmark pinned to a Windows host with no vizier, which
  held the epic's final tickets; ten pilot tickets failing on one shared test
  fault; a single-chain graph that wanted restructuring for parallelism;
  and a performance budget measured on a loaded machine that kept filing
  follow-ups. The user's rule: orchestration completes work speculatively
  by relaxing blocking constraints, so none of these should reach them.
- Once the loop starts nobody asks the user. Whoever holds the choice (a
  worker, a Warden, the Justiciar on a deadlock) applies the least-blocking
  default from `common.md` "Speculative defaults" and records it on the ticket
  and the run epic. The vizier still never reads tickets; its only graph
  action stays a Justiciar request.
- The stoppage audit asks the user only about human-only steps (access,
  credentials, payments, irreversible outward actions, outside people).
  Agent-decidable points become recorded defaults in the run epic.
- Planners avoid serializing edges: edges go on the ticket that needs a
  result rather than on its epic, reported-only tickets get no dependents,
  pins are reserved for tickets that cannot run elsewhere and gate nothing,
  and decomposition tickets sit under the run epic with no dependencies,
  since the milestone epic keeps the tickets they file gated.
- `enact next` flags a pin nobody serves: when nothing else can proceed and
  every ready ticket it skipped is pinned to a host or provider with no vizier
  route registered on the run, it returns `deadlock` with a `stranded` list
  instead of waiting for the six-hour stale check. Running viziers already
  handle `deadlock` by requesting a Justiciar, so the change is compatible.
  Registration is the liveness proxy: a registered vizier that died still
  falls back to the stale check.

## Lane and tier defaults (2026-11-13)

- A user asked how nine tickets filed outside a run should be executed to
  spend the fewest tokens. The answer was already Enact's lane design, but
  `/enact-plan` applied it only to tickets it filed itself: given an existing
  epic with children it filed nothing and ran one session per ticket, or one
  session per lane however the lanes had been labelled.
- The reasoning, now stated in the skill: a session pays once to learn its
  code and on every turn to re-read its whole context. One session per ticket
  relearns shared code. One session for many tickets carries finished tickets
  through every later turn and runs all of them on the costliest ticket's
  model. The second cost is the larger, so lanes stay at two to four tickets.
- The vizier therefore runs a "lane and tier pass" over the unclaimed tickets
  of any epic or ticket list it is given, using the files their descriptions
  name. It does not read code for this, so the pass stays inside pre-flight
  and the loop is unchanged.
- A lane has one tier. The session keeps the model its first ticket started
  on, so the nine tickets above, labelled by shared code alone, would have run
  Sonnet-tier tickets on Opus or an Opus-tier design on Sonnet. A ticket of
  another tier leaves the lane and takes an ordering edge where it edits the
  same code non-additively.
- A ticket likely to fill a session (a design, a rewrite) stays out of lanes,
  for the same reason a worker with a heavy context closes without
  `--continue`.
- Lane labels are not put on epics, like tier labels: children inherit an
  epic's labels, which would join unrelated tickets into one lane.
- No helper change. The scheduler still treats a lane as "one session at a
  time"; the rules only change which tickets share a label.

## Removing a worktree ends its processes

A fuzz run started in a ticket's worktree kept a core busy for four and a half
hours after the ticket closed and the tree was deleted, because nothing reads the
result of a process whose directory is gone. `remove_worktree` and the sweep
now end every process whose working directory or executable is inside the tree
(SIGTERM, then SIGKILL after three seconds) before deleting it, never the
calling process or its ancestors. The processes found by `lsof` (or `/proc`)
are reported as `ended_processes` in the JSON of `worktree remove`, `push` and
the sweep. On Windows reaping is skipped and logged.

## Merge queue (2026-11-13)

Beryl's fast check takes 95 s at the median and 240 s at the 90th
percentile. With optimistic pushes, a push whose check another push beat to
staging rebases and checks again, so at 30 commits an hour most pushes would
check twice or more. `[push] queue = true` replaces that with a queue per host.

- `enact push` verifies ownership, rebases in its own tree as before (a
  conflict there is exit 2, as before), then writes an entry to
  `<git common dir>/enact/queue` and waits for a verdict file.
- No daemon. Whichever waiting push holds `queue.lock` (the CI runner's OS
  lock) is the integrator. It runs batches until its own entry has a verdict,
  then releases the lock, and another waiting push takes over. If it dies,
  the OS frees the lock and the next waiter runs the entries still without a
  verdict. Entries whose push process died are dropped, since nobody is left
  to finish their delivery. An integrator that died after pushing left
  delivery receipts tagged with the entry's id, and the next integrator turns
  those into `landed` verdicts instead of replaying the commits.
- Batch: up to `[push] batch` entries (default 8), oldest first, replayed in
  order onto `origin/staging` in a persistent detached worktree,
  `<root>/.merge-queue`, whose build directories stay warm. The first entry
  whose base is the staging tip fast-forwards and keeps its SHAs; later ones
  are cherry-picked. An entry that does not apply gets a `conflict` verdict
  and its push re-integrates in its own tree, where the conflict surfaces as
  exit 2 the way it always did.
- No fill wait. Batches form on their own while the previous batch's check
  runs, which is when pushes pile up; a wait would only delay a lone push.
- A failing check splits the batch: entries whose changed files the output
  names (else the later half) are set aside, the rest are checked and land
  together, then the set-aside entries are checked on their own. One bad
  branch in five costs three checks. A lone failing entry gets the check's
  output back, and its push prints it and exits 2.
- The integrator re-checks ownership under the ownership lock and writes each
  entry's delivery receipt before pushing. The waiting push then does the
  ticket bookkeeping, worktree removal and slow-CI start exactly as before;
  results gain `queue: {batch, checks}`.
- The check reads `.enact.toml` from the combined tree and is killed after
  `[push] timeout` seconds (default 1800), since a hung check would hold
  every push on the host.
- A local integrator takes `refs/enact/staging-lock` before checking and
  keeps its three-minute lease fresh while the check runs. Integrators on
  other hosts wait for that lease; if its holder dies, they take it over
  after it expires. The remote ref changes with compare-and-swap, so only
  its holder may release it. This prevents checks on separate hosts from
  repeatedly racing to advance `staging`.
- The integrator remembers the last passing batch check even if it loses the
  staging lease. Once it reacquires the lease, it skips the check if the
  staging base, ordered batch and resulting Git tree match. This also covers
  a cherry-pick that rebuilt the same tree with new commit timestamps.
- If staging advanced and the batch is the same, the queue retries with
  `ENACT_CHECKED` set to the passing candidate. A project may skip check
  lanes the new staging commits cannot reach, as direct pushes already do.
  A different batch uses only the individual branch checks it came with.
  The queue keeps retrying genuine staging races. A push rejected while
  staging is unchanged reports the remote error.
- A branch that merged a diverged master, and `--skip-checks`, take the old
  direct path: the integrator replays commits one by one and cannot replay a
  merge.
- Off by default, so projects opt in.

## Each push checks its own tree before queueing (2026-11-14)

- With the queue running every fast check, a batch's check was the host's
  bottleneck. On 2026-11-14 Beryl's took 150-300 s per batch in the queue's
  tree, against about 64 s for the same change in the branch's own warm
  tree, and seven pushes waited up to 25 minutes behind one another.
- Now `enact push` runs the full fast check in its own tree first, beside
  other pushes' checks, and queues only a branch that passed. Its queue entry
  records the commit that passed (`checked`).
- The queue runs the check on a batch with `ENACT_CHECKED` set to every
  entry's checked commit, separated by spaces. A project may then rerun only
  the lanes that an entry's own change and the rest of the merged tree (the
  batch's other branches and the commits that landed since its check) both
  reach. A project that reads `ENACT_CHECKED` as one commit finds no such
  commit and runs every lane, as before. An entry from an older client has
  no checked commit, and then the batch's check gets no `ENACT_CHECKED`.
- A lone branch that fast-forwards onto the staging tip it was checked on
  lands with no second check: the queue's tree is the tree that passed.
- A queued push that comes back with a conflict re-integrates and checks
  again in its own tree with `ENACT_CHECKED` naming what passed before.

## One Justiciar per machine (2026-11-13)

- The Justiciar lock was fleet-wide (`refs/enact/justiciar`). A hold from
  one machine (often a Codex run left behind) blocked emergencies on every
  other machine, and only that machine's session could message the holder.
  Emergencies are usually local (disk, tools, a deadlocked epic this host's
  vizier drives), and a vizier only ever spawns its own host's Justiciar.
- Now each machine has its own lock, `refs/enact/justiciars/<host>`, still on
  origin so holders stay visible and acquisition stays atomic. The ref lives
  under a new namespace because git cannot hold both `refs/enact/justiciar`
  and refs below it.
- Compatibility: a legacy `refs/enact/justiciar` lock counts as its own
  host's lock, so a Justiciar already holding it can still transition and
  release it; other hosts ignore it.
- Justiciars on different machines may work at once. They coordinate shared
  state (Git remote, ticket service, `master`) through incident tickets.

## Adopting existing tickets (2026-11-13)

A plan session moved a ticket out of another open epic into its own run and
blocked it on a deep ticket there, leaving the first epic's remaining ticket
waiting on work far outside it: `enact next` reported a deadlock on a run
nobody had touched. The session had read the ticket with output cut short, so
it never saw the parent or the dependent, and its graph view predated the
other epic claiming the ticket.

The plan skill now treats existing tickets as owned by whoever filed them:
read the full ticket (parent and dependents) right before moving or wiring
it, ask the user before taking a ticket from another open epic or blocking a
ticket another epic waits on, and check at the end of planning that no other
epic now waits on the run. This stays in the skill rather than the helper:
moves and edges use `tk`, and the judgement of when to reference a ticket
instead of adopting it needs the plan's context.

## Vizier leases (2026-11-13)

- Beryl's `master` sat for three hours while slow CI failed and nobody
  repaired it. `enact push` leaves slow CI to a vizier when `vizier_active`
  is true, and that only asked whether some epic carried a route
  (`enact_vizier_route_*`, written once by `run register`) and was open or
  had incomplete delivery. Routes outlive their viziers, and delivery can
  stay incomplete for good once Steward ranges fold into a run, so it was
  true with no vizier running anywhere.
- A vizier now renews a lease, `.git/enact/vizier-<session>.json` holding
  its epic and time, on every poll of `enact next`, and drops it when the
  run ends (`done`) or is paused or blocked. A route is alive when it was
  registered on this host and its vizier's lease names that epic and is
  younger than `VIZIER_LEASE_SECONDS` (20 minutes). `next` polls every
  minute while it waits, so the time to live only has to cover the gaps
  while a vizier acts on a step. A route with no lease, such as one from
  before leases existed, is not alive.
- `vizier_active` is: some run that is not paused or blocked has a live
  route. Routes on other hosts are ignored, and the epic's status and
  delivery no longer matter. Liveness is local because each machine
  repairs only its own slow-CI failures: a Steward with `enact_ci_host`
  goes only to that host's viziers, and the two-hour handover to any
  machine is gone. Stewards without a host still route by `enact_ci_os`,
  or anywhere when they have neither.
- `stranded()` counts a route on this host only while it is alive. Routes
  on other hosts still count, since their leases cannot be read here;
  treating them as dead would make every ticket pinned to another machine
  look stranded.
- `enact push` prints `watch_ci` unless it has fresh evidence that a vizier
  will repair the push's slow-CI failures: the vizier that spawned the ticket
  still renews its lease on that run, or one of the ticket's runs is live. An
  ad hoc push, with no ticket, always prints it. Assuming nobody is watching
  is the cheap mistake: a needless watch costs one session some waiting,
  while a missing one stalled Beryl's master for hours. A worker whose run
  is paused or blocked still gets no notice, since a held run starts no
  repairs; claiming the Steward would fail anyway.
- `common.md` and `executor.md` now tell workers to follow `watch_ci` when
  push prints it, because it then means their vizier has stopped.
- When slow CI files or updates a Steward and no live run here covers its
  `enact_runs`, the ticket gets `enact_unowned=true` and a note in its
  description (or the new failure comment), so a session or scheduled
  routine looking for unrepaired failures finds it without reasoning about
  runs. `claim()` unsets the flag. A failure with no recorded runs is
  always unowned, even while some vizier here loops: that vizier stops when
  its own run is delivered, so it is no promise.
- `enact status` answers the same question as a snapshot, so a scheduled
  routine need not guess which sessions are alive. Each route in
  `active_runs` carries `lease_age` (whole seconds, or null with no lease
  on that epic here), and a run is `stale` when no route is alive.
  `unowned_stewards` lists open Stewards targeted at this machine
  (`steward_targeted_here`) that no live run here would schedule (one
  without a parent, or one in that run's delivery, as `schedule` does) and
  whose assignee, if any, neither holds a fresh lease (on any epic) nor
  updated the ticket within `VIZIER_LEASE_SECONDS`. Unlike `enact_unowned`,
  which is set once when CI files the failure, this counts any live vizier
  as cover for a parentless Steward: a routine runs status again every
  half hour, so the promise only has to hold until then. Worker sessions
  hold no lease, so for them the ticket's `updated_at` is the only evidence.


## Runs finish explicitly (2026-11-13)

- A run counted as active until its epic closed and every commit reached
  master. Beryl's by-gdn3 was fully delivered but stayed open because two
  children were deferred, so its run looked live indefinitely.
- `enact run finish <epic> --reason ...` sets the run's `enact_control` to
  `finished`. `run_live` (so `vizier_active`, `runs_watched` and
  `bead_watched`) and `enact status`'s `active_runs` ignore a finished run,
  and `enact next` on one returns `action: finished` and drops the lease.
  Finishing a paused or blocked run is refused; `run resume --explicit`
  reactivates a finished run. `stopped_runs` is unchanged: finished runs
  are not halted, so late Steward repairs of their commits stay allowed.
- A child set aside indefinitely counts as done (`work_done`) when Enact
  decides whether an epic is complete. Dated deferrals keep their epics open.
  The service announces deadlines on the feed, and `schedule` waits for
  readiness when a deadline is the only barrier.

- The vizier's skill text runs `enact run finish` as its last command.


## Pruning stale runs (2026-11-13)

- Routes outlive the viziers that wrote them, so runs whose vizier died
  stayed in `enact status`. `enact run prune` retires them: a run is stale
  when it is not finished, paused or blocked, has routes and all of them
  are on this host, and none has a fresh lease. A run with any route on
  another host is left alone, because that host's leases cannot be read
  here. A closed run is pruned like an open one.
- A stale run becomes `finished` only when its epic is closed and its
  delivery is complete. A green delivery can belong to an open epic with
  more tickets to run; marking that run finished would strand those tickets.
  An open epic, or one with pending commits or Steward tickets, becomes
  `paused` with a reason naming the expired lease (and a closed epic is
  reopened, as `run pause` does), so a returning vizier resumes it with
  `enact run resume --explicit`.
- `enact ci` prunes after it files or updates a Steward ticket, and
  `enact status` lists `stale_runs` with a hint to run the prune.

## Green delivery with unfinished work (2026-11-13)

- In Beryl run `by-bmdk`, the Codex vizier ended a turn while `enact next`
  still returned `action: wait`. Its lease expired. The epic remained open,
  but the commits delivered so far had all reached master. That state must
  remain recoverable; a green delivery is not evidence that all tickets ran.
- `run prune` now requires both a closed epic and complete delivery to mark
  a stale run `finished`. It pauses an open epic so a returning vizier can
  explicitly resume it. This fixes the false completion in recovery; the
  Codex vizier still needs an active turn or a new message to continue its
  scheduling loop.

## Stalled workers (2026-11-14)

- In Beryl run by-bajq two workers ended their turns waiting on background
  `just specs <card>` commands, servers that never exit. They sat idle for
  4.5 to 6 hours with tickets `in_progress`, filling all three slots. A bound
  open ticket counts as busy forever, so the vizier kept reporting `wait`
  until the user noticed.
- `enact next` now lists `stalled` entries: the vizier's own open tickets
  bound by `enact_session` whose `updated_at` is 90 minutes old or more.
  Each carries the session, idle minutes and a one-line `nudge`. The result
  adds `stall_notice` so the instructions survive compaction.
- ticket age is only a hint: a healthy worker may run long without touching
  its ticket. Only the host knows whether a session is idle, so the vizier
  checks it (`get_session`, `read_thread`), nudges only an idle one with
  `send_message` (which works from automated turns), and releases an
  archived or missing one as any lost worker. A false positive costs one
  session lookup.
- Each 90-minute period of a stall is reported once, keyed by ticket and
  period in the existing per-vizier `archive-reported` file, and a new one
  ends a background wait early. So a vizier is not re-woken by the same
  stall, while a nudge that did not help surfaces again one period later.
  A ticket update clears the key.
- Rejected: watching worktree file times or commits (costly on big trees,
  host-local, and a hung server can still write logs), and a separate
  record of nudges (the period key gives the same spacing statelessly).
  Older viziers ignore the new fields.

## Idle workers are checked after 15 minutes (2026-11-15)

- In Beryl run by-q44s about a dozen workers ended their turn while a long
  job ran outside the harness (a Callgrind count, a GPU gate in a VM, slow
  CI). No notice woke them when it finished, so each sat idle until the
  90-minute stall check, costing 90 to 180 minutes of a worker slot.
- `common.md` now tells workers to wait on long jobs in the foreground or
  with a host-tracked background command, never by ending the turn.
- `enact next` now lists a bound ticket after 15 minutes without an update.
  When the vizier finds that worker's turn still running, it passes
  `--running <ticket>` on its next call; the ticket is then not listed again
  until 90 minutes, as before. The mark is the ticket's ID and `updated_at`,
  kept with the reported keys in the per-vizier `archive-reported` file,
  so any ticket update drops it and the short check starts over.
- The stall key now includes the period length, so a 15-minute report does
  not hide the first 90-minute one.
- Cost: a healthy worker that runs quietly costs the vizier one session
  lookup per ticket update. Rejected: a turn-end hook in each host (Claude
  and Codex differ, and it adds installed state on every machine).

## A run answers only to its own holds (2026-11-14)

- Beryl paused two live runs on 2026-11-14. A Steward that repairs slow
  CI for several runs lists all of them in `enact_runs`. When one of them
  (by-bajq.2) was paused, `schedule` checked the Steward with
  `stopped_runs`, found that pause, and reported the other run (by-jj37)
  as paused too. Its vizier stopped looping and dropped its lease, so the
  next slow-CI prune found no fresh lease and paused by-jj37 for real.
- `run_holds(bead, graph, epic)` now decides what holds a ticket within a
  run: a pause or block on the run, on an epic above it, or on a milestone
  the ticket sits in. `schedule` uses it for the tickets in its scope, and
  `require_running` uses it for work claimed for a run (`enact_run`).
  Unparented Stewards outside the run's scope keep the wider
  `stopped_runs` check.
- A vizier's lease names the epic it loops on, but prune also checks
  milestones, whose routes come from that vizier's workers. A lease on an
  epic now covers every epic below it, so milestones by-bajq.1 to .3 are
  no longer pruned while their vizier loops on by-bajq.
- Rejected: one lease per epic a vizier touches (more files and the same
  question of which epics count), and skipping milestones in prune (a
  milestone can have a vizier of its own).

## Prune touches only registered runs (2026-11-14)

- After the fix above, Beryl run by-bajq stayed paused. Slow CI lists every
  epic a failing commit touches in a Steward's `enact_runs`, milestones
  included. `vizier_routes` then infers a route on milestone by-bajq.1 from
  each Steward that names it, including Stewards claimed by another run's
  vizier (by-jj37's). That vizier's lease named by-jj37, which does not
  cover by-bajq.1, so prune paused by-bajq.1 to .3 as stale runs while
  by-bajq's own vizier was looping. `run resume by-bajq --explicit` cleared
  only by-bajq, and `run_holds` still saw the milestone pauses.
- A run, for prune, is now an epic a vizier registered (`enact_vizier_route_*`
  metadata). Inferred routes still show in `enact status` but never make an
  epic prunable.
- A milestone below a registered, unfinished run is never pruned on its
  own: pausing the run already holds it. This answers the earlier objection
  that a milestone can have a vizier of its own: a fresh lease from a vizier
  registered on any milestone keeps the whole run alive, so prune cannot
  pause a run above a live milestone vizier either.
- `run resume <epic> --explicit` also lifts pauses that prune wrote
  (`actor: enact run prune`) on the epic's milestones, giving them the
  run's new epoch, and reports them in `resumed_milestones`. A pause a
  person wrote on a milestone stays until they resume that milestone. This
  recovers milestones older Enact pruned.
- Steward `enact_runs` still lists milestones: `run_holds` already ignores
  holds outside a run, and the links keep delivery tracking complete.

## Blocked verification is essential (2026-11-14)

- An executor left an oracle check for its own card unrun because the
  checker mismatched a card name, and noted the fix only in its handoff.
- The essential-only follow-up rule read as permission to drop it. A
  tooling defect that blocks verifying the ticket's own work now counts as
  essential: fix it in the ticket when within reach, otherwise file a
  follow-up whose acceptance runs the blocked checks, and record the gap on
  the ticket. Handoffs and summaries are never where such a gap goes, since
  nothing acts on them. Marginal bugs met in passing stay unfiled.

## Dead delivery receipts (2026-11-16)

- `enact next` held the ownership lock for about 10 s per poll on Beryl's
  Mac, 56% of all lock hold time between 02:00 and 04:15 UTC (347 polls).
  7.5 s of that was `recover_deliveries` checking 237 receipts with two
  `git merge-base` calls each. None could ever be recovered.
- 228 of them came from pushes that lost the race to staging. The direct
  path writes a receipt for its head before pushing, and after a rejected
  push it rebased and wrote a new receipt without removing the old one.
  The merge queue path already removed its receipts after a failed push.
- Now a rejected push removes its receipt once the fetch and recovery that
  follow it show that it did not land. `schedule` and the start of
  `cmd_push`, which run right after a fetch that must succeed, also drop
  receipts over an hour old whose head is on neither staging nor master.
  `cmd_status` fetches with `check=False`, so it never drops one.

## Longest chains first (2026-11-15)

- Among ready tickets of equal role class and priority, `enact next` (and lane
  continuation) now starts the ticket with the longest chain of open tickets
  waiting on it through blocking edges (`blocks`, `conditional-blocks`,
  `waits-for`), counting itself. ticket number remains the last tiebreak.
- Why: picking the lowest number could leave a long dependency chain for
  last, so a parallel run would finish with one session working through it
  ticket by ticket while the rest of the pool sat idle. Starting the critical
  path early keeps the tail of a run wide.
- Stewards and explicit priority still come first: priority is the
  planner's stated intent, chain length only a default.
- Closed and deferred tickets do not lengthen a chain. Lanes are not counted
  as chains: lane tickets already run one after another in one session, and
  only edges express a real ordering.

## Pool limit per message (2026-11-15)

- The pool cap is ten starts per user message, not ten over a session's
  life: a Beryl vizier started thirteen sessions across two user messages.
  `pool add` refused once ten had ever been added, so a vizier that lost
  members could not refill its pool. Enact cannot tell user turns apart,
  so `pool add` now refuses only more than ten members in one call (one
  message's starts) and `started` is kept for reporting. `pool-empty` no
  longer sends the user to a new session at the cap.

## Release rather than wait on another ticket (2026-11-16)

- A worker whose ticket needs another ticket's work that has not reached
  staging now commits to its branch, adds the dependency edge, and
  releases its ticket. It does not watch `origin/staging` in a loop.
- Why: in Beryl's week to 2026-11-16, workers in active runs held run
  slots for 16 to 55 minutes at a time in loops that waited for another
  ticket, or a Steward's repair, to land. A run that is limited by its slot
  count loses that time outright. A released ticket is not ready again
  until its new blocker closes, so the vizier gives the slot to ready work
  and restarts the ticket afterwards, and the recovery record keeps the
  branch and head for the next session.
- A closed dependency is not waited on either: its commits are on
  staging already, so the worker rebases.

## One ticket's unmet need never holds a run (2026-11-16)

- A Beryl run stopped overnight for one ticket. The Beryl 3D sweep on Cygnus
  (Windows) filed a cargo-test failure into the epic with no host pin. A
  Mac worker could not reach Cygnus or its log, so it asked for a
  Justiciar. The Justiciar queued a request for Cygnus, waited ten minutes,
  then ran `run block --exhausted`. That held the whole epic, though
  nothing depended on the ticket. The failure was MSVC's missing
  `mspdbcore.dll` on Cygnus, not Beryl's code.
- A ticket only another machine can do or check (its failure came from
  there, or it needs that OS, a device or that host's logs) is now pinned
  to that host by the worker and released. It is not impossible
  acceptance. A failure in a machine's own environment is that machine's
  work.
- The Justiciar drops a ticket with an unmet need from the run instead of
  holding the run: it moves the ticket out of the epic, labels it
  `enact:dropped`, removes edges so nothing waits on it, and comments
  `Dropped requirement:` on the run epic for the vizier's final report.
  The ticket stays open with its acceptance, so this does not close an
  impossible requirement. Waiting on another machine or a person is not
  recovery.
- `run block --exhausted` stays for a need that no ticket left in the run
  can progress without. The helper is unchanged: whether a need is one
  ticket's or the run's is a judgment, and the rule lives in `justiciar.md`.
- Pre-flight's stoppage audit now covers machines, OSes and devices no
  vizier on the run serves, and tooling that files tickets into the epic
  from such machines. It also runs over the open tickets of an existing
  epic or input tickets, which it skipped before.
- When a run is blocked, the vizier gives the user two answers: meet the
  need, or drop the requirement and resume.

## Cloudflare ticket system contract

The ticket service owns the graph, scheduling, leases, and feed. Every
project supplies an active `[tickets]` configuration. Both production projects
completed the irreversible transition to `after` on November 17, 2026.
See [local ticket verification](../README.md#local-ticket-verification)
for the simulator and isolated Git tests.

### 1. Storage

One Durable Object per project, named by the project key (`by`, `en`). Its
SQLite database is `server/contract/schema.sql`:

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
-- keys: seq (global change counter), cutover (before|paused|after),
-- cutover_at, cutover_seq, daily_usage ({day, requests, rows_written}),
-- token_hashes (hash to machine), token_fetched_at

CREATE TABLE tickets (
  id          TEXT PRIMARY KEY,          -- 'q44s.7.24', no project prefix
  parent      TEXT,                      -- id with the last dotted part removed
  next_child  INTEGER NOT NULL DEFAULT 1,-- next number handed to a child
  title       TEXT NOT NULL,
  body        TEXT NOT NULL DEFAULT '',
  acceptance  TEXT NOT NULL DEFAULT '',
  design      TEXT NOT NULL DEFAULT '',
  notes       TEXT NOT NULL DEFAULT '',
  type        TEXT NOT NULL DEFAULT 'task',   -- task|epic|bug|feature|chore|decision|spike|story|milestone
  status      TEXT NOT NULL DEFAULT 'open',   -- open|in_progress|blocked|deferred|closed
  priority    INTEGER NOT NULL DEFAULT 2,     -- 0..4
  assignee    TEXT,
  last        INTEGER NOT NULL DEFAULT 0,     -- 1: runs after its siblings (section 4)
  labels      TEXT NOT NULL DEFAULT '[]',     -- JSON array of strings
  metadata    TEXT NOT NULL DEFAULT '{}',     -- JSON object (Enact's enact_* keys)
  defer_until TEXT,
  close_reason TEXT,
  created_at  TEXT NOT NULL, created_by TEXT NOT NULL,
  updated_at  TEXT NOT NULL, closed_at TEXT,
  version     INTEGER NOT NULL                -- value of seq at the last change
) WITHOUT ROWID;
CREATE INDEX tickets_live ON tickets(status) WHERE status != 'closed';
CREATE INDEX tickets_parent ON tickets(parent) WHERE parent IS NOT NULL;

CREATE TABLE edges (                       -- src depends on dst
  src TEXT NOT NULL, dst TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'blocks',     -- blocks|conditional-blocks|waits-for|related|discovered-from
  PRIMARY KEY (src, dst)
) WITHOUT ROWID;
CREATE INDEX edges_dst ON edges(dst);

CREATE TABLE comments (
  ticket TEXT NOT NULL, n INTEGER NOT NULL,
  author TEXT NOT NULL, at TEXT NOT NULL, text TEXT NOT NULL,
  legacy_id TEXT,                          -- ID of an imported Beads comment
  PRIMARY KEY (ticket, n)
) WITHOUT ROWID;

CREATE TABLE history (                     -- the changelog
  ticket TEXT NOT NULL, seq INTEGER NOT NULL,
  actor TEXT NOT NULL, at TEXT NOT NULL,
  change TEXT NOT NULL,                    -- JSON: {field: [old, new], ...}
  PRIMARY KEY (ticket, seq)
) WITHOUT ROWID;

CREATE TABLE leases (
  name TEXT PRIMARY KEY, holder TEXT NOT NULL, machine TEXT NOT NULL,
  expires_at TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
  waiters TEXT NOT NULL DEFAULT '[]'       -- JSON array of {holder, machine, since}, arrival order
) WITHOUT ROWID;

CREATE TABLE events (
  seq INTEGER PRIMARY KEY, at TEXT NOT NULL,
  kind TEXT NOT NULL,                      -- session|ci|machine|cutover|usage
  machine TEXT NOT NULL, actor TEXT NOT NULL, data TEXT NOT NULL  -- JSON
);
CREATE INDEX events_kind ON events(kind, seq);

CREATE TABLE aliases (alias TEXT PRIMARY KEY, id TEXT NOT NULL) WITHOUT ROWID;
CREATE INDEX aliases_id ON aliases(id);
```

- Labels and metadata are JSON in the ticket's row, not tables. Filtering by
  label scans the live tickets, of which there are a few hundred.
- When the object wakes it loads every ticket whose status is not `closed`,
  and the edges among them, into memory. `graph`, `ready` and readiness are
  answered from memory; writes go to SQLite and memory in one transaction.
- An imported ticket's whole changelog is one `history` row (`seq` 0, `change`
  a JSON array of the old events). This keeps a full import near 40,000 rows
  written, inside one day's limit; the importer ticket measures the real
  figure.
- Events older than 30 days are deleted by a daily alarm.

### 2. IDs

- Alphabet: `0123456789abcdefghjkmnpqrstvwxyz` (base32 without `i l o u`).
- A new root is 4 characters, the first one a letter, chosen at random and
  retried on collision with a ticket or an alias.
- A child's ID is `<parent>.<next_child>`, taking and incrementing the
  parent's `next_child` in the same transaction. Depth is not limited.
  A ticket keeps its ID when an update changes its parent. Readiness uses
  the stored parent.
- Canonical IDs use lowercase letters and digits, with hyphens allowed in
  imported roots and dots before child numbers. Dashboard links and labels
  and all native `tk` output display an uppercase `T` before the ID. Ticket
  lookups with that prefix use the suffix as the canonical ID. Other lookups
  strip a leading `<project>-` and consult `aliases`. The Worker API sends
  canonical IDs. The importer sets each parent's `next_child` to one past
  its highest child, and `tk restore` accepts exports with or without the
  display prefix.

### 3. HTTP API

All routes are under `/<project>/api`. Reads need no token. Writes need
`Authorization: Bearer <token>` and take the session's name from
`X-Actor` and the machine's from the token's registration.

| Route | Does |
| --- | --- |
| `GET /state` | `{cutover, cutover_at, cutover_id, seq, day, requests, rows_written, limits}` |
| `GET /tickets/<id>?comments=1&history=1` | One ticket with its edges in both directions, children, `ready` and `blocked_by` |
| `GET /tickets?status=&type=&parent=&ancestor=&label=&assignee=&ready=1&all=1&limit=` | A list; closed tickets only with `all=1` or `status=closed` |
| `GET /graph?all=1` | Live tickets by default, or all tickets with `all=1`. Includes `ready`, edges within the selected graph, and `seq`. Omits `body`, `design` and `notes`. Matching the projection's `ETag` returns 304. |
| `POST /batch` | The only way to write tickets (below) |
| `POST /leases/<name>` | `{op: acquire\|renew\|release, holder, ttl_s, payload}` |
| `GET /leases` | Every lease with its holder, expiry and waiters |
| `POST /events` | `{kind, data}` |
| `GET /events?kind=&since=&limit=` | Events after `since` |
| `GET /feed` | WebSocket (section 6) |
| `POST /import` | Up to 200 tickets with edges, comments and history; idempotent |
| `GET /export?after=<id>` | JSON lines of whole tickets sorted by ID, paged |
| `POST /state` | `{cutover, pause_id?}`; only moves forward, except `paused` to `before` |

On `GET /tickets`, `search` matches a case-insensitive title phrase or an
ID prefix, including legacy IDs. It includes closed tickets by default and
orders results by priority, newest creation time, then ID. The repeatable
`label_any` filter matches tickets with any of the given labels.

The import body is `{tickets: [...]}`. Each element has the ticket fields
plus its outgoing `edges`, `comments`, `history` and `aliases` arrays. Export
uses the same flat objects, up to 200 per page and an empty page at the end.
The server assigns the version of changed ticket fields and preserves the
higher child allocation counter. It skips snapshots older than the stored
ticket by `updated_at`. Import returns `{seq, imported, rows_written}`, where
`imported` counts changed tickets and `rows_written` counts their data and
the shared sequence write.

An import can supply `daily_row_ceiling` to lower its own quota. The Worker
checks it against current usage in the import transaction. Imported comments
keep their original IDs in `legacy_id`. `metadata._enact_beads` retains
source-only fields and the original dependency records as archival metadata.

`POST /batch` takes `{if: [...], ops: [...]}` and applies all of it in one
transaction or none of it.

- Preconditions in `if`: `{id, version}`, `{id, assignee}` (null for
  unassigned), `{id, status}`, `{seq}` (nothing at all has changed since).
- Operations in `ops`: `create` (fields; `parent`; `deps`; returns the new
  ID, which later operations in the batch may name as `$0`, `$1`), `update`
  (`set` fields, `add_labels`, `remove_labels`, `set_meta`, `unset_meta`),
  `claim` (sets `assignee` to the actor and `status` to `in_progress`; fails
  if assigned to anyone else), `close` (`reason`), `reopen`, `defer`
  (`until`), `comment` (`text`), `dep_add`, `dep_remove` (`on`, `kind`), and
  `event` (append an event associated with a ticket or a batch reference).
- Result: `{seq, results: [...]}`, or status 409 with
  `{failed: <index of the precondition or operation>, current: <ticket>}`.
- A create dependency with `reverse: true` is directed from `on` to the
  new ticket, as in `tk create --deps blocks:ID`.
- Closing a ticket also closes each ancestor whose descendants are now all
  closed, in the same transaction, and reports them in the result.
- A close with `include_ready: true` also returns `ready`, the IDs ready
  after that close. The scheduler uses them to choose a lane continuation
  from the graph it already has, conditional on the close's result sequence.

Enact uses the `all=1` projection to keep closed worker assignments visible
for archiving and to track their commits through slow CI. Cache each graph
projection separately by URL, project and query. `tk graph --if-none-match`
takes the opaque `etag` from a fresh graph response and returns
`{"unchanged": true}` on HTTP 304. It belongs to that project and graph
projection and changes when deferred readiness changes. Use the numeric
`seq` for batch preconditions and `wait change --since SEQ`. Starting the
feed at that sequence catches changes between the read and subscription.

Status codes: 200; 304; 400 malformed; 401 missing or unknown token; 404 no
such ticket; 409 precondition failed, claim or lease lost, or late import
conflict; 429 with
`{limit: requests|rows_written}` when the object's own count says the write
would pass 90 percent of a daily limit.

There are no endpoints written specially for Enact. Its scheduler stays in
Python. For tickets, `enact next` is one `GET /graph` (usually a 304 against the copy the
helper cached on disk) and one `POST /batch` whose preconditions make the
reservation atomic. `enact start` and `enact close` are one batch each, the
close with `--continue` reading the reply's graph delta to pick the lane's
next ticket and claiming it in a second batch. The ticket operations take
at most two requests (three for a lane continuation). The vizier also
updates its heartbeat and looks up its machine's Justiciar lease.

### 4. Readiness

A ticket is **ready** when all of these hold:

1. Its status is `open`, and `defer_until` is absent or in the past.
2. Every `edges` row from it of kind `blocks`, `conditional-blocks` or
   `waits-for` points at a ticket that is closed and, if that ticket has
   children, has no live descendant.
3. The same holds for every ancestor's blocking edges.
4. If `last` is 1: every other descendant of its parent whose `last` is 0 is
   closed.
5. It has no live child. A ticket with children is a container and closes
   by itself.

On the server, a plan puts its audits and their findings under the milestone
they cover. Audits are marked `last` and the next milestone depends on that
milestone. The Archivist is a `last` child of the run and depends on any
audits that are also `last` children of the run. Enact retains the Archivist
hold for legacy plans whose Archivist has no `last` flag.

`GET /tickets/<id>` reports `blocked_by` as the list of ticket IDs that make
it not ready, with which rule each came from, so a listing can always say
why. Nothing is hidden from listings for being blocked.

Each live graph ticket includes `blocked_by` for rules 2–5 so a scheduler
can see blockers involving closed tickets omitted from the live graph. The
graph includes the full readiness result in `ready`. When an eligible
ticket's defer time passes, the graph's ETag changes without a new write.
The graph's numeric `seq` remains the batch precondition.

`tk defer` leaves the ticket open with `defer_until` set. Imported dated
deferrals wake when their deadline passes. The alarm or a graph or ready
read reopens the ticket, clears its date, advances the sequence, and
journals the change for the feed. An undated deferral needs an explicit
status change.

### 5. Leases

A lease is a named, expiring claim. Names in use: `staging` (the merge
queue's lock, `ttl_s` 180, renewed every 50 s while a batch is checked),
`justiciar/<machine>`, and `vizier/<epic>/<vizier>` for vizier liveness.

- `acquire`: succeeds if there is no row, the row has expired, or the caller
  already holds it. Otherwise the caller is appended to `waiters` if absent
  and gets 409 with the holder, expiry, its position and the feed sequence.
  An expired lease goes to the first waiter that asks. A waiter that has not
  asked for 60 s is dropped. With `no_queue` set, a conflict leaves the queue
  alone and omits the position.
- `renew` and `release` succeed only for the holder. When the caller supplies
  `if_revision`, it must also match the revision in the current payload.
  A conflict returns 409 (`LeaseLost` for the staging lease in Enact).
- A waiter does not poll: it holds the feed and asks again when told the
  lease changed or reached its expiry. Subscribing from the 409 response's
  sequence also catches a change before the socket connected. If an expired
  lease is still reserved by an abandoned head waiter, the alarm wakes the
  remaining queue again when that reservation lapses.

### 6. The feed

`GET /feed` upgrades to a WebSocket, accepted with the hibernation API so an
idle connection costs nothing.

- Server to client, after each committed change:
  `{seq, tickets: [ids], leases: [names], events: [kinds], cutover}`.
  Changes within 100 ms are sent as one message.
- Client to server, once after connecting: `{since: <seq>}`. If the server's
  `seq` differs, it replies with one message listing everything changed
  since, or `{resync: true}` if that is more than 500 tickets or the
  cursor is outside the journal's replay window.
- One bounded SQLite journal row records each committed change alongside
  the operation. The journal holds the latest 4,096 sequence slots and
  replays at most 1,000 commits per subscription. A stale or missing
  cursor can therefore never make a replay scan the whole database.
- One alarm announces lease expiries and the end of a time-based readiness
  block. It wakes at the earliest pending deadline and advances the
  sequence so waiters recheck the state.
- `tk wait` and the dashboard are the only clients. The dashboard fetches
  the HTML fragments for the tickets named and swaps them in.

### 7. Authority and recovery

The Worker is authoritative for production tickets. Enact uses the native
client for every tracker operation. A service outage preserves the queued
operation or returns an error. Daily native exports can be restored with
`tk restore` in a fresh project. The archived migration used authenticated
state transitions to verify ticket parity before activating each project.

### 8. Tokens

- `tk register` writes 32 random bytes, hex encoded, to
  `~/.config/enact/token` (`%APPDATA%\enact\token` on Windows), and pushes
  the ref `refs/enact/tokens/<machine>/<sha256 of the token>` to the
  project's GitHub remote, pointing at the repository's first commit. It
  deletes the machine's older refs.
- On a write whose token hash is not in `meta.token_hashes`, the server
  reads the project's public Git upload-pack ref advertisement at most once
  a minute. It replaces the stored set with the advertised token refs and
  checks the request again. GitHub's anonymous REST quota was exhausted on
  the production Worker's egress IP. The repository for each project is a
  Worker variable.

### 9. The `tk` command line

Global options: `--project`, `--actor`, `--json`, `--retry-for <seconds>`
(default 120). The server's address and the project come from `[tickets]`
in `.enact.toml`.

```
tk show <id>... [--comments] [--history]
tk list [--status S] [--type T] [--parent ID] [--ancestor ID] [--label L]...
        [--label-any a,b] [--assignee A] [--all] [-n|--limit N]
tk children <id>
tk search <words> [--status S] [-n|--limit N]
tk ready [--ancestor ID] [--label L]...
tk graph [--all] [--if-none-match ETAG]
tk create [TITLE | --title T] [-t|--type T] [-p N] [--parent ID] [-l|--labels a,b]
          [--body-file F] [--description D] [--acceptance A] [--design-file F]
          [--deps a,b] [--last] [--silent]
tk update <id> [--title T] [--status S] [-p N] [--assignee A] [--parent ID]
          [--body-file F] [--acceptance A] [--notes N] [--add-label L]...
          [--remove-label L]... [--set-metadata k=v]... [--unset-metadata k]...
          [--claim] [--last|--no-last] [--if-version N] [--if-assignee A]
tk claim <id>
tk close <id> --reason R
tk reopen <id>
tk defer <id> --until DATE
tk comments <id>
tk comments add <id> [TEXT | -f|--body-file F]
tk dep add|remove <id> <on> [-t|--type K]
tk dep cycles
tk batch                      # the /batch JSON on stdin
tk lease acquire|renew|release <name> [--ttl S] [--holder H] [--payload JSON]
         [--no-queue] [--if-revision REV]
tk leases
tk event <kind> <json>
tk events [--kind K] [--since SEQ]
tk wait ready [--ancestor ID] [--label L]... | ticket <id> [--status S]
        | lease <name> | change [--since SEQ]   --timeout S
tk state [--set-cutover before|paused|after [--pause-id ID]]
tk export
tk restore <tickets.jsonl> [--row-budget N]
tk register | tk bench | tk version
```

Without `--json` a command prints a short human-readable form; with it, the
server's JSON. Exit codes: 0 done; 1 bad usage, not found or a malformed
request; 3 a precondition failed or a claim or lease belongs to someone else
(the code Enact already uses for a lost race); 4 the server could not be
reached within `--retry-for`; 5 a daily limit was hit and did not clear
within `--retry-for`; 6 `tk wait` timed out. Retries back off from 0.2 s to
10 s. Only reads and operations with preconditions are retried without
asking the server whether the first attempt landed; a `create` carries a
client-chosen request key that the server remembers for a day, so a retry
never creates twice.

A JSON
`show` with one ID returns a ticket object. With multiple IDs it returns an
array in the requested order. `comments <id>` returns the ticket's comments
array. `dep cycles` returns arrays of ticket IDs. `create --silent` prints
only the canonical ID when called without `--json`.

Search accepts a quoted phrase or separate words. It returns up to 50 tickets
by default. Pass `--limit 0` to return every match.

#### Daily backups and restore

At startup, the slow-CI runner checks whether the active ticket backend is
the server and whether the project's configured Git remote already has a
backup for the UTC day. Run `enact ticket-backup` from the project to make the
same check on days with no slow CI or to retry a failure. Backup failures are
logged separately from the slow check.

The `ticket-backups` branch contains only `tickets.jsonl`, with one ticket per
line in ID order. The new commit names its UTC day and extends the previous
backup. Git accepts the push only if the branch tip still matches the one
the exporter read. If the server sequence changes during pagination, retry
the export so all pages come from the same project state.

To restore, configure a checkout to reach an isolated Worker with a fresh
project key in its `TICKET_REPOSITORIES` allowlist. Give the client a write
token registered for that project and keep other writers out until
verification finishes. Use the allowlisted key in place of `recovery` below.
With `origin` as the backup remote, run:

```sh
git fetch origin refs/heads/ticket-backups
git show FETCH_HEAD:tickets.jsonl > tickets.jsonl
tk --project recovery restore tickets.jsonl
tk --project recovery export > restored.jsonl
```

Use a byte-preserving shell for the Git redirect, such as Git Bash on
Windows. Repeat `restore` with the same file if it exits 5 for the daily
write budget. Compare the exported tickets and their related records by ID.
The destination assigns new ticket versions. The backup covers ticket data,
including dependencies, comments, history and aliases. Leases, events,
tokens and cutover state are maintained separately by the Worker.

### 10. Budget

SQLite reports the rows and index entries written. A changed batch writes
the shared `meta.seq` row once. Excluding that row:

- A keyed root `create` writes 4 rows (ticket, live index, history and request
  key). A child writes 2 more (parent index and the parent's allocation
  counter). A newly added dependency writes 2. An unkeyed or closed creation
  omits its request-key or live-index row, respectively. A keyed retry
  writes zero.
- A ticket update writes 2 rows for the ticket and history. An insert or
  update of a live status adds 1, and a new parent adds 1. In the simulator
  a close writes 2 rows per closed ticket, including any closed ancestors.
- A comment writes 1 row. Removing a dependency writes 1. Comments and
  dependency changes advance graph `seq`; they have their own tables and
  leave the ticket's field `version` unchanged. The allocation counter
  also leaves the parent's field `version` unchanged.
- An event writes 2 rows and a lease operation writes 1, before shared
  bookkeeping.

| A normal day | Count | Requests | Rows written |
| --- | --- | --- | --- |
| Ticket writes (create, update, claim, close) | 6,000 | 6,000 | 20,000 |
| Comments | 1,000 | 1,000 | 1,000 |
| Reads by sessions (`show`, `list`, `graph`, `state`) | 15,000 | 15,000 | 0 |
| Lease operations, staging renewals included | 4,000 | 4,000 | 4,000 |
| Session, CI and machine events | 2,000 | 2,000 | 4,000 |
| Dashboard pages and fragments | 3,000 | 3,000 | 0 |
| Feed connections and messages | | 500 | 0 |
| **Total** | | **31,500 of 100,000** | **29,000 of 100,000** |

The table estimates operation rows. Each changed request can also write
one sequence row and one feed journal row. Each project counts requests
against a durable reservation of 32 requests at a time. A read spends a
reserved request in memory. A mutation still persists its written-row
count with the data change. After eviction the object assumes any unspent
requests in the prior reservation were used. The counters reset at 00:00
UTC. The usage row itself counts toward the limit. When the per-project row
counter reaches its 90 percent threshold, accounting stops writing and
public reads remain available.

The combined Beryl and Enact replay model puts a busy day at 4,849,700
SQLite rows read and 94,429 billed rows written. These are estimates for the
account. The Worker's write guard and dashboard counters are per project.
The estimate assumes viewers use the live dashboard and Beryl's legacy
export pollers have stopped. See the budget evidence in Beryl's
`by-r61mr.36`.


### 11. Deployment target

`server/wrangler.toml` names the existing `tickets` Worker on the
`thurn.workers.dev` account subdomain and allowlists Beryl (`by`) and Enact
(`en`). The Worker rejects unconfigured project routes before it opens
Durable Object storage. Production activation is tracked in Beryl epic
`by-vaexw`. The [operator steps](../README.md#ticket-enrollment-and-cutover)
describe the deployment and enrollment gates.

## Ticket server coordination (2026-11-16)

The ticket backend holds the `staging` lease for the whole merge-queue
batch and renews it before the Git push. Waiters subscribe from the
sequence returned with the acquisition conflict. The holder renews before
the waiters' positions expire so they can refresh on the feed notification.

Each machine's Justiciar uses a six-hour lease. Request, spawn and acquire
advance a revision in the lease payload. Renewing or releasing the lease
checks the revision, so a caller that read an older state cannot overwrite
it. Justiciar contenders receive a conflict without entering the staging
style wait queue. Viziers renew their own leases while waiting and release
them when their loop ends.

Session events and slow-CI failure events accompany their ticket changes
in the same batch. A sequence precondition makes concurrent CI failures on
the same OS converge on one open Steward ticket. Passing CI advertises
promotion after the Git push, so a feed waiter can fetch and check master.
The local runner still tracks its PID, log and pending promotion receipt.

## Runs never wait for time to pass (2026-11-16)

- Audits and plans kept filing tickets whose acceptance needed elapsed time:
  Beryl's by-q44s.4.6 ("first measure a day of pushes after" a fix) under
  the M2 review gate, and by-vaexw.1 ("over a normal running day"). Such a
  ticket holds its epic, so the whole run, open for hours with nothing to do.
- The stoppage audit in enact-plan lists waiting for time or data as a
  stoppage. `common.md` forbids filing such tickets: acceptance must be
  measurable when the ticket is worked (existing logs, replays, benchmarks).
- Observation still worth having becomes an **observation ticket**: no
  parent, label `enact:observe`, deferred to the date the data will exist.
  A dated deferral under a run epic would keep that epic open (`work_done`),
  so it lives outside every run. When the date passes it is ordinary open,
  unowned work, which the sweep triages or a later run takes.
- A worker handed such a ticket does the change, measures what it can now,
  files the rest as an observation ticket and closes, so tickets already filed
  stop blocking too. This is prompt-only; the helper is unchanged.


## Push check trees (2026-11-17)

On Beryl, the Beryl 3D shared test build in a push's fast check used a
median of 134 CPU-seconds (p90 303) on 2026-11-16, against about 20 for the
same change in a tree that had built the commit before it. A push checks
in its own tree, and that tree last built whatever staging was when its
agent started, often 20-30 commits earlier. rustc keys incremental caches to
the checkout's absolute path, so a fresher build directory cannot be copied
in: `--remap-path-prefix` puts the path in the flag itself, and
`trim-paths` and `-Zremap-cwd-prefix` are unstable.

`[push] check_trees = N` runs each push's fast check in one of N reused
worktrees, `check-1` to `check-N`, instead. Every push is staging's tip
plus one change, so a tree that only checks pushes stays a commit or two
behind staging and its incremental caches stay valid.

- `claim_check_tree` takes the first free tree by its OS lock
  (`<state>/check-N.lock`), most recently used first (`check-N.used`), and
  checks the pushed head out detached with `checkout_slow_tree`: ignored
  build directories stay, tracked changes and untracked files go.
- Busy trees are skipped. When all are busy, or git refuses a checkout,
  the check runs in the pushing tree, as before, so a push never waits on
  another push's check.
- The check gets `ENACT_BRANCH`, the branch it checks, since the tree is
  detached. Processes the check leaves behind are reaped before the lock is
  released. The worktree sweep keeps check trees.
- The merge-queue path (`queue = true`) still checks in the pushing tree.

