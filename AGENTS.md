# Working on Enact

- Enact's skills live in `skills/` and the stdlib-only orchestration helper
  lives in `scripts/enact.py`. The ticket service is a TypeScript Cloudflare
  Worker in `server/`. The Rust client in `cli/` builds `tk`.
- Skill and role files are markdown wrapped at 80 columns. Keep
  `skills/enact/SKILL.md` under ~500 lines and each role file around 100.
- The helper must stay stdlib-only, Python 3.11+, and work on macOS, Linux
  and Windows (no shell-specific syntax, `pathlib` everywhere).
- Fast and slow check commands live in `.enact.toml`. Both run the Worker
  and Rust tests. Fast CI compiles the Python helper and runs its unit tests.
  Slow CI also exercises real git, bd and Dolt in temporary directories.
  Budget: 2-3 minutes for slow CI; cut or redesign scenarios to stay within it.
- For focused Worker checks, run `npm ci` and `npm test` in `server/` with
  Node 22.12 or later. Run Wrangler there with `npx wrangler`. Worker tests
  use the local simulator and require no Cloudflare login.
- For focused Rust checks, run `npm ci` and `npm run pretest` in `server/`, then
  run `cargo test --locked` in `cli/`. The CLI tests launch Wrangler locally.
  Build the release executable with `cargo build --locked --release`.
- Set `[tickets].project` and `[tickets].url` in a project's `.enact.toml`
  to point `tk` at its ticket server. The URL is the server root, for
  example `https://tickets.thurn.workers.dev`. Use a local URL with
  `tk bench` to measure fresh-process reads and writes against Wrangler.
- Deliver through Enact's own flow, never by committing or pushing to
  `master`: `python3 scripts/enact.py worktree create <name>` from the main
  checkout, commit in the printed worktree, then
  `python3 scripts/enact.py push` there. Slow CI advances `master`, which is
  what every machine pulls, so only green changes reach live sessions. If
  push prints `watch_ci`, follow it before finishing.
- Title every session working here `⚒️ <short summary>` from the start
  (🔮 while waiting on slow CI, 🚒 while fixing a Steward bead) and end it on
  `✅ <same summary>`, or `⛔` if you stop without finishing. A turn that
  ends waiting on the user's answer or decision ends on `🔍 <same
  summary>`. Enact users read session state from these emojis; the host's
  activity indicator is not accessible enough to rely on.
- Slow-CI failures file Steward beads in this repository's own Beads graph
  (prefix `en`).
- Projects and live sessions on every machine use this repository directly,
  so keep changes backward compatible.
- Record design decisions in `docs/design.md`.
