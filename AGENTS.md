# Working on Enact

- Enact's skills live in `skills/` and the stdlib-only orchestration helper
  lives in `scripts/enact.py`. The TypeScript ticket service in `server/`
  runs on Node with local SQLite. Its ticket store and
  coordination APIs live in `server/src/`, its schema in `server/contract/`
  and server tests in `server/tests/`. `cli/src/` builds the native client.
- Skill and role files are markdown wrapped at 80 columns. Keep
  `skills/enact/SKILL.md` under ~500 lines and each role file around 100.
- The helper must stay stdlib-only, Python 3.11+, and work on macOS, Linux
  and Windows (no shell-specific syntax, `pathlib` everywhere).
- Fast and slow check commands live in `.enact.toml`. Both run the server
  and Rust tests. Fast CI compiles the Python helper and runs its unit tests.
  Slow CI exercises real Git and the isolated SQLite ticket service.
  Budget: 2-3 minutes for slow CI; cut or redesign scenarios to stay within it.
- For focused ticket service checks, run `npm ci` and `npm test` in `server/`
  with Node 24.22.3 or later. The tests use isolated SQLite databases. The
  operator guide is in `docs/sqlite-tickets.md`.
- For focused Rust checks, run `npm ci` and `npm run pretest` in `server/`, then
  run `cargo test --locked` in `cli/`. The CLI tests use local HTTP and SQLite.
  Build the release executable with `cargo build --locked --release`.
- Set `[tickets].project` and `[tickets].url` in the main checkout's
  `.enact.toml`. Linked worktrees inherit enrollment from the main checkout.
  Use an isolated ticket service in fixtures.
- Deliver through Enact's own flow, never by committing or pushing to
  `master`: `python3 scripts/enact.py worktree create <name>` from the main
  checkout, commit in the printed worktree, then
  `python3 scripts/enact.py push` there. Slow CI advances `master`, which is
  what every machine pulls, so only green changes reach live sessions. If
  push prints `watch_ci`, follow it before finishing.
- Title every session working here `⚒️ <short summary>` from the start
  (🔮 while waiting on slow CI, 🚒 while fixing a Steward ticket) and end it on
  `✅ <same summary>`, or `⛔` if you stop without finishing. A turn that
  ends waiting on the user's answer or decision ends on `🔍 <same
  summary>`. Enact users read session state from these emojis; the host's
  activity indicator is not accessible enough to rely on.
- Slow-CI failures file one Steward task per OS in the active tracker.
  Run `enact backend` to identify it. The live Enact ticket project is `en`.
- Projects and live sessions on every machine use this repository directly,
  so keep changes backward compatible.
- Record design decisions in `docs/design.md`.
