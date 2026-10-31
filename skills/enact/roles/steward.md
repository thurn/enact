# Enact role: 🚒 Steward

The slow CI suite failed on staging, and you make it green again. This is
the highest-priority work in the project. Read `common.md` first.

A session outside any run may take this role after `enact push` printed
`watch_ci`: it claims the bead with `enact claim <bead>` instead of
`enact start`, keeps its own title, and after pushing its repair keeps
watching with `enact ci --wait <sha>` until slow CI passes.

## The failure

The slow-CI runner filed your bead. It records:

- `enact_ci_os`: the OS that failed. Prefer fixing it on that OS. If you are
  on a different OS and cannot reproduce, comment and keep going with
  analysis: the cause is often visible in the log.
- `enact_ci_host`: the machine that failed. A pass on another machine, or
  master advancing from another machine's runner, does not prove the
  failure was transient: look for that host's own state (caches, vendor
  files, tools) in the log before closing it as already green.
- `enact_ci_range`: `<last passing commit>..<failing tip>`. The baseline can
  be a passing commit awaiting promotion after a push failure. Several
  pushes may be folded into one run.
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
4. **Is it a real bug?** A failing or flaky test whose cause is in the
   product (a race, nondeterminism, a broken edge case) is a real failure:
   fix the product. If the product is fine and only the test is wrong (a
   timing threshold tripping under machine load, a sleep, a brittle
   assertion on incidental output), very seriously consider deleting the
   test instead of fixing it. A false positive is strong evidence the test
   is worthless: it pulled a Steward off real work and caught nothing.
   Repair it only if it guards behavior nothing else covers, and then
   replace the timing dependence with a deterministic check (`warden.md`,
   "Tests that survive load") rather than loosening the threshold or
   rerunning. Comment on the bead which tests you deleted and why. If you
   can neither fix nor justify deleting it, quarantine the test (move it
   out of the slow suite, with a reason) and file a bead to fix and
   restore it.
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
