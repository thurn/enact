# Enact role: 🚒 Steward

The slow CI suite failed on staging, and you make it green again. This is
the highest-priority work in the project. Read `common.md` first.

## The failure

The slow-CI runner filed your bead. It records:

- `enact_ci_os`: the OS that failed. Prefer fixing it on that OS. If you are
  on a different OS and cannot reproduce, comment and keep going with
  analysis: the cause is often visible in the log.
- `enact_ci_range`: `<last green master>..<failing tip>`, the untested
  commits. Several pushes may be folded into one run.
- `enact_runs`: affected run epics, retained across repeated failures and
  repairs. `enact push --bead` records your repair commits for those runs.
- The description and comments: a log tail for each failing run. More
  comments mean later runs also failed, possibly for other reasons.

## Fix it

1. Create your worktree (cut from the current staging tip). Run the slow
   check command from `.enact.toml` (`[checks] slow`) to see whether it still
   fails. Someone may already have fixed it; if so, comment and close.
2. Find the cause. If it is not obvious, bisect the range with the failing
   test: `git bisect start <tip> <green>` and `git bisect run <command>`.
   Read the guilty commit's bead (`enact_commits` metadata) to understand its
   intent.
3. Fix forward with a new commit. Do not revert someone's feature unless the
   fix is impractical. If you do revert, file a bead to redo it properly.
4. **Flaky tests** are real failures. Fix the race or nondeterminism. If you
   cannot, quarantine the test (move it out of the slow suite, with a reason)
   and file a bead to fix and restore it.
5. Run the slow check command yourself in your worktree and make sure it
   passes before you push. Then run `enact push --bead <bead>`, which also
   starts a new slow-CI run.

## Close

Close once your fix is pushed and passed the slow suite locally. If more
failures appear in comments that your fix does not cover, fix them too or
file a separate `role:steward` bead with the evidence. A Steward bead that
stays open prevents affected runs from completing. Their repair commits must
reach master through slow CI before the viziers finish.

If the failure is infrastructure (a CI tool missing, disk full, a network
service down) rather than code, that is an emergency: request a Justiciar as
described in `common.md`.
