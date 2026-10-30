# Working on Enact

- Enact is the skill text in `skills/` plus the stdlib-only helper
  `scripts/enact.py`. Keep it minimal: add helper code only when it clearly
  beats calling git or `bd` directly.
- Skill and role files are markdown wrapped at 80 columns. Keep
  `skills/enact/SKILL.md` under ~500 lines and each role file around 100.
- The helper must stay stdlib-only, Python 3.11+, and work on macOS, Linux
  and Windows (no shell-specific syntax, `pathlib` everywhere).
- Fast checks:
  `python3 -m py_compile scripts/enact.py && python3 -m unittest -q tests.test_unit`.
- Slow checks: `python3 -m unittest -q tests.test_unit tests.test_integration`
  (real git, bd and Dolt in temp dirs; several minutes).
- Deliver through Enact's own flow, never by committing or pushing to
  `master`: `python3 scripts/enact.py worktree create <name>` from the main
  checkout, commit in the printed worktree, then
  `python3 scripts/enact.py push` there. Slow CI advances `master`, which is
  what every machine pulls, so only green changes reach live sessions. If
  push prints `watch_ci`, follow it before finishing.
- Slow-CI failures file Steward beads in this repository's own Beads graph
  (prefix `en`).
- Projects and live sessions on every machine use this repository directly,
  so keep changes backward compatible.
- Record design decisions in `docs/design.md`.
