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
- Commit to `master` and push to origin `master`. Projects and live sessions
  on every machine use this repository directly, so keep changes backward
  compatible.
- Record design decisions in `docs/design.md`.
