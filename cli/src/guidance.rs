pub const PRIME: &str = r#"Enact tickets (tk)

All commands are non-interactive. Writes publish immediately. Use --json for
programmatic output; diagnostics go to stderr. No sync command is needed.
Run `tk COMMAND --help` or `tk help COMMAND` for flags and defaults.

Start and finish work:
  tk ready --brief --json
  tk show Tabcd --comments --json
  tk claim Tabcd --actor session-name --json
  tk update Tabcd --notes 'Investigation result' --json
  tk close Tabcd --reason 'Implemented and verified' --json
In an Enact run, use enact start/close to also record session and lane state.
A conflict exits 3: re-read the ticket; do not overwrite another actor's claim.

Create and connect work:
  tk create 'Fix the failure' -t bug -p P1 --labels role:executor --json
  tk create 'Child task' --parent Tabcd --body-file body.txt --json
  tk update Tabcd --body-file - --json < body.txt
  tk comments add Tabcd --body-file handoff.txt --json
  tk dep add Tabcd Tefgh --json
For a blocking edge, the first ID depends on the second. Bare --deps ID means
the new ticket depends on ID; --deps blocks:ID means ID depends on the new one.
Use --parent for hierarchy. Dotted IDs do not determine a reparented ticket's
current parent. T-prefixed IDs, bare native IDs and imported aliases are accepted.

Browse and wait:
  tk list --all --label role:executor --limit 20 --json
  tk children Tabcd --json
  tk search 'failure' --json
  tk graph --all --json
  tk wait ticket Tabcd --status closed --timeout 60 --json
List excludes closed tickets by default; children and search include them.
Search matches titles and ID prefixes, not body text. List has no default
limit; search defaults to 50. Both accept --limit 0 for every match.
Ready uses Enact rules, including ancestor blockers, last siblings and deferral.
Use --brief to omit long text, comments, history and lease payloads from results;
export always stays complete. Use tk wait instead of polling. Clear an assignee
or parent with an empty string.

JSON contracts:
  show ID: ticket object; show ID ID: array in requested order
  list, ready, children, search, comments ID: arrays
  create, update, claim, close, reopen, defer, dep, comments add:
    {"seq":N,"results":[...]}; a create ID is results[0].id
  graph: {"seq":N,"tickets":[...],"edges":[...],"etag":"..."}
  export: JSON Lines, even with --json; includes comments, history and aliases
  prime --json: {"guide":"..."}; version --json: {"version":"..."}
  failure with --json: JSON object on stdout; diagnostic on stderr
  --help always prints text, even with --json
Use tk batch --help for atomic multi-ticket changes and preconditions. Batch
reads one JSON object from stdin; --body-file - reads UTF-8 text from stdin.
For concurrent updates, use --if-version from show; a mismatch exits 3.

Exit codes:
  0 success; 1 usage, configuration, authentication, not found or bad request
  3 precondition failed, claim lost or lease held
  4 server unavailable or transport failure; an unsafe write may have landed
  5 daily write limit did not clear within the retry budget
  6 wait timeout
--retry-for defaults to 120 seconds; 0 disables retries. Reads and replay-safe
writes retry automatically. Create request keys prevent duplicate tickets during
one invocation's retries. Re-read after an uncertain write before resending it.

Setup:
[tickets].project and [tickets].url in .enact.toml enroll the repository.
Linked worktrees inherit enrollment from the main checkout. --project changes
the project on the same server. Run enact setup for managed installation and
registration; tk register --ensure verifies this machine's write registration.
Help, prime and version work offline without enrollment or a write token.
"#;
