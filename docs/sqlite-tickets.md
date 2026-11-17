# SQLite ticket operations

JARVIS is the authoritative host for Enact (`en`) and Beryl (`by`).
Each project owns one SQLite database on the host’s local SSD. Clients
connect over the tailnet at `https://jarvis.tail4b0774.ts.net:8443`. On
JARVIS, the same service accepts loopback at `http://127.0.0.1:7345`.

## Build and serve

Use Node 24.22.3 or later. Build an immutable release outside the checkout
so a Git update or dependency reinstall cannot affect the running service.

```sh
npm --prefix server ci
npm --prefix server run build:local
npm --prefix server prune --omit=dev
```

Keep the configuration and data outside the release. Restrict both to the
service account. For example:

```json
{
  "data_dir": "/var/lib/enact-tickets",
  "projects": {"en": "thurn/enact", "by": "thurn/beryl"},
  "public_url": "https://jarvis.tail4b0774.ts.net:8443",
  "port": 7345,
  "read_only": false
}
```

Start the service with the absolute Node binary and built entry point.

```sh
node server/.local/tickets.mjs serve SERVICE_CONFIG
```

The service requires an existing database for each configured project.
Set `read_only` to true when inspecting a restored copy. Configure
`launchd` with `RunAtLoad` and `KeepAlive`, and send both output streams to
local log files. A system daemon with a dedicated account can start before
an interactive login. JARVIS’s user jobs are
`ca.thurn.enact-tickets.service` and `ca.thurn.enact-tickets.backup`.

The tailnet proxy has its own HTTPS port so it can coexist with the
host’s existing listener on 443.

```sh
tailscale serve --bg --https=8443 http://127.0.0.1:7345
tailscale serve status
```

Check that the listener on 8443 is tailnet-only. Connect clients to the
tailnet, then use `enact backend` and `tk state` from each checkout. Set
`ENACT_TICKETS_URL=http://127.0.0.1:7345` for commands on JARVIS if they
need loopback. Linked worktrees inherit the main checkout’s ticket
configuration. Use an explicit override for isolated test fixtures.

## Back up and recover

Run the backup command hourly from a separate `launchd` job. Point an
off-host backup at completed snapshots and skip dot-prefixed staging
directories. Verify a copied snapshot before using it to restore.

```sh
node server/.local/tickets.mjs backup SERVICE_CONFIG SNAPSHOT_DIRECTORY
node server/.local/tickets.mjs verify-nas SERVICE_CONFIG NAS_DIRECTORY
node server/.local/tickets.mjs verify-backup SNAPSHOT_DIRECTORY/SNAPSHOT
```

The backup keeps the latest 48 snapshots and one per day for the last 30
days. Continue the daily Git JSONL export for portable ticket history.

To restore, verify a chosen snapshot and copy its project databases into
a fresh data directory. Start an isolated read-only service and check
`tk state`, ticket history and dashboard access. For production recovery,
pause ticket writers and CI, stop the service and preserve the existing
database directory with its WAL files. Switch the service config to the
restored directory. Reconcile pending CI receipts and lease holders
against Git before reopening writes. Restart clients and discard their
cached graphs (`ticket-graph-*.json` in the Git common directory’s
`enact/` folder).
