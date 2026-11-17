# SQLite ticket operations

## Prepare JARVIS

Install Node 24.22.3 or later and build from the Enact release that contains
the SQLite service. Use a dedicated service account and keep the release, its
configuration, and its database directory on JARVIS's local SSD. The service
account needs read access to the built release and exclusive write access to
the database and snapshot directories.

```sh
npm --prefix server ci
npm --prefix server run build:local
```

Save a service configuration outside the checkout. Replace the example paths
and the tailnet hostname with JARVIS's actual values. Keep `read_only` true
during cutover verification.

```json
{
  "data_dir": "/var/lib/enact-tickets",
  "projects": {"en": "thurn/enact", "by": "thurn/beryl"},
  "public_url": "https://jarvis.example.ts.net",
  "port": 7345,
  "read_only": true
}
```

Create a system LaunchDaemon with `RunAtLoad` and `KeepAlive` true,
`UserName` set to the service account, and `ProgramArguments` containing the
absolute Node binary, the absolute `server/.local/tickets.mjs` path, `serve`,
and the absolute configuration path. Route stderr to a service log. The
process requires an existing database for each configured project.

Install the CLI-only `tailscaled` as a system service so JARVIS can join the
tailnet before an interactive login. Enable MagicDNS and tailnet HTTPS. Grant
the Enact machines access to TCP 443 on JARVIS and configure the HTTPS proxy.
Verify that the proxy is tailnet-only and that the host's sleep and reboot
behavior fits the required availability.

```sh
tailscale serve --bg http://127.0.0.1:7345
tailscale serve status
```

## Freeze, migrate, and activate

1. Quiesce CI and ticket writers on both projects before freezing. Deploy the
   compatible Worker from this release, retaining its Durable Object namespace.
   Install a unique high-entropy Worker secret named `TICKET_MIGRATION_TOKEN`.
   Set `ENACT_MIGRATION_TOKEN` to the same value in the migration shell.
2. Freeze Enact and Beryl independently. Assign each project a stable,
   distinct UUID as its migration ID. Keep these IDs with the migration
   receipts. Run the operations as the JARVIS service account once for each
   project, replacing
   `PROJECT`, `MIGRATION_ID`, and `DATA_DIRECTORY`.

   ```sh
   npm --prefix server run tickets -- freeze https://tickets.thurn.workers.dev PROJECT MIGRATION_ID
   npm --prefix server run tickets -- migrate https://tickets.thurn.workers.dev PROJECT MIGRATION_ID DATA_DIRECTORY
   ```

   An interrupted import can resume with the same project, ID and directory.
   It verifies row counts and content against the frozen source for each
   table and publishes the database once validation passes. If Cloudflare
   rejects access due to quota, keep writers stopped and retry after access
   returns. The Git JSONL export is a ticket-only recovery copy and can be
   older than the live database.
3. Start JARVIS with `read_only` true. From JARVIS and a tailnet peer, compare
   `tk state` with the migration receipt and check ticket history, readiness,
   the dashboard, and feed connection. Check service startup after reboot.
   Benchmark a scratch copy of production data using release `tk`. Run
   `python3 server/scripts/bench-local.py SCRATCH_CHECKOUT RELEASE_TK
   --check-loopback-targets` on JARVIS, and repeat from a peer without the
   loopback flag. Also run with `--concurrent-writes 4` and no loopback flag
   while taking a snapshot of the scratch copy. Compare the share of graph
   revalidations served from cache.
4. Change `[tickets].url` in the main checkouts to JARVIS's HTTPS origin.
   Set `ENACT_TICKETS_URL=http://127.0.0.1:7345` for JARVIS's Enact shell
   and CI environment. Confirm each effective endpoint with `enact backend`.
   Leave this override unset for fixture work outside Enact's test harness.
   Restart the service with `read_only` false, then restart waiting clients
   and resume Enact. Check an authenticated write and its feed notification
   before resuming CI on each project.

Before JARVIS starts writing, stop it and use `tickets thaw URL PROJECT
MIGRATION_ID` with the same secret to roll back. After JARVIS becomes
writable, keep Cloudflare frozen and repair forward on JARVIS. Keep the
Cloudflare data until a verified NAS snapshot has been restored successfully
to an isolated service. Remove the migration secret when the bridge is
retired.

## Back up and recover

Run the backup command from an hourly LaunchDaemon as the service account,
with `StartInterval` set to 3600 and the Node binary and built entry point
from the service job. Point the existing hourly NAS job at the completed
snapshot directories, skipping dot-prefixed staging directories. Run the
NAS verification command after the copy and collect its exit status and
stderr in the backup log.

```sh
npm --prefix server run tickets -- backup SERVICE_CONFIG SNAPSHOT_DIRECTORY
npm --prefix server run tickets -- verify-nas SERVICE_CONFIG NAS_DIRECTORY
npm --prefix server run tickets -- verify-backup NAS_DIRECTORY/SNAPSHOT
```

The backup keeps the latest 48 snapshots and a daily snapshot from the last
30 days. A peer can optionally pull one completed snapshot and its manifest
from the NAS each day using read-only credentials and its own retention
policy. Healthy hourly backups limit disk-loss recovery to roughly an hour
of work. Continue the existing Git JSONL backup for portable ticket history.

To restore, first verify a chosen snapshot and copy its project database files
into an isolated service's fresh data directory. Test `tk state`, a few
tickets with history, and the dashboard. For production recovery, stop JARVIS
and pause all writers. Save the damaged data directory, including any WAL
sidecars, and switch the service config to the restored directory. Keep it
read-only until operators reconcile pending CI receipts and lease holders
against Git and confirm no old lease holder can resume. Restart clients and
discard their cached ticket graphs (`ticket-graph-*.json` under the Git
common directory's `enact/` folder) before resuming writes and CI.
