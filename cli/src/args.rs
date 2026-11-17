use clap::{Args, Parser, Subcommand, ValueEnum};
use std::path::PathBuf;

#[derive(Parser)]
#[command(
    about = "Read and update Enact tickets",
    after_help = "Run `tk prime` for agent workflow, JSON shapes, exit codes and examples.\nWrites publish immediately. No editor, pager or sync command is needed.\nConfiguration: [tickets].project and [tickets].url in .enact.toml."
)]
pub struct Options {
    /// Override the enrolled project key; keep the configured server URL.
    #[arg(long, global = true)]
    pub project: Option<String>,
    /// Attribute writes and claims to this actor; use a unique session name.
    #[arg(long, global = true, default_value = "tk")]
    pub actor: String,
    /// Print machine-readable JSON; export always prints JSON Lines.
    #[arg(long, global = true)]
    pub json: bool,
    /// Omit long text, comments, history and lease payloads; export stays complete.
    #[arg(long, global = true)]
    pub brief: bool,
    /// Retry safe operations for this many seconds; 0 disables retries.
    #[arg(long, global = true, default_value_t = 120.0, value_parser = seconds)]
    pub retry_for: f64,
    #[command(subcommand)]
    pub command: Command,
}

fn seconds(value: &str) -> Result<f64, String> {
    let value: f64 = value.parse().map_err(|_| "expected seconds")?;
    if std::time::Duration::try_from_secs_f64(value).is_err() {
        return Err("expected nonnegative, finite seconds".into());
    }
    Ok(value)
}

#[derive(Subcommand)]
pub enum Command {
    /// Show full ticket details, optionally with comments and history.
    Show {
        /// Ticket IDs: T-prefixed, bare native IDs or imported aliases.
        #[arg(required = true)]
        ids: Vec<String>,
        /// Include the complete comment thread.
        #[arg(long)]
        comments: bool,
        /// Include the complete change history.
        #[arg(long)]
        history: bool,
    },
    /// List active tickets with filters; use --all to include closed tickets.
    List(List),
    /// List direct children of a ticket, including closed children.
    Children {
        /// Ticket ID: T-prefixed, bare native ID or imported alias.
        id: String,
    },
    /// Search ticket titles and ID prefixes, including closed tickets.
    Search(Search),
    /// List tickets eligible to start under Enact readiness rules.
    Ready(ReadyList),
    /// Read tickets, dependency edges and a sequence cursor as one snapshot.
    Graph {
        /// Return {"unchanged":true} if this graph ETag still matches.
        #[arg(long)]
        if_none_match: Option<String>,
        /// Include closed tickets, which are excluded by default.
        #[arg(long)]
        all: bool,
    },
    /// Create a ticket; JSON returns its ID in results[0].id.
    Create(Create),
    /// Update ticket fields, labels or metadata, optionally with preconditions.
    Update(Update),
    /// Atomically claim an unassigned ticket for --actor.
    Claim {
        /// Ticket ID: T-prefixed, bare native ID or imported alias.
        id: String,
    },
    /// Close a ticket and record a completion reason.
    Close {
        /// Ticket ID: T-prefixed, bare native ID or imported alias.
        id: String,
        /// Record why the ticket was completed.
        #[arg(long)]
        reason: String,
    },
    /// Reopen a closed ticket.
    Reopen {
        /// Ticket ID: T-prefixed, bare native ID or imported alias.
        id: String,
    },
    /// Postpone a ticket until the specified date.
    Defer {
        /// Ticket ID: T-prefixed, bare native ID or imported alias.
        id: String,
        /// Defer until a timestamp or YYYY-MM-DD date (UTC midnight).
        #[arg(long)]
        until: String,
    },
    /// Read comments: tk comments ID; append: tk comments add ID TEXT.
    #[command(subcommand_negates_reqs = true, args_conflicts_with_subcommands = true)]
    Comments {
        /// Ticket ID: T-prefixed, bare native ID or imported alias.
        #[arg(required = true)]
        id: Option<String>,
        /// Operation to perform.
        #[command(subcommand)]
        operation: Option<CommentOperation>,
    },
    /// Add, remove or inspect dependencies; ID depends on ON.
    Dep {
        /// Operation to perform.
        #[command(subcommand)]
        operation: EdgeOperation,
    },
    /// Send an atomic JSON batch from standard input.
    #[command(after_help = "Example:
  printf '%s' '{\"ops\":[{\"op\":\"create\",\"fields\":{\"title\":\"Investigate failure\"}}]}' | tk batch --json

Operations: create, update, claim, close, reopen, defer, comment, dep_add, dep_remove, event.
Use {\"if\":[{\"id\":\"Tabcd\",\"version\":7}],\"ops\":[...]} for guarded writes.
Create references $0, $1, ... refer to earlier create operations in the batch.
Success: {\"seq\":N,\"results\":[...]}; conflicts exit 3 and roll back the whole batch.")]
    Batch,
    /// Acquire, renew or release a named coordination lease.
    Lease {
        /// Operation to perform.
        operation: LeaseOperation,
        /// Lease name.
        name: String,
        /// Lease lifetime in seconds.
        #[arg(long, default_value_t = 180)]
        ttl: u64,
        /// Lease owner; defaults to --actor.
        #[arg(long)]
        holder: Option<String>,
        /// Lease payload as a JSON object.
        #[arg(long)]
        payload: Option<String>,
        /// Require the current payload revision before renewing or releasing.
        #[arg(long)]
        if_revision: Option<String>,
        /// Fail immediately without joining the lease queue.
        #[arg(long)]
        no_queue: bool,
    },
    /// List active coordination leases.
    Leases,
    /// Append a structured orchestration event.
    Event {
        /// Event kind: session, ci, machine, cutover or usage.
        kind: String,
        /// Event data as JSON.
        data: String,
    },
    /// Read orchestration events, optionally after a sequence cursor.
    Events {
        /// Event kind: session, ci, machine, cutover or usage.
        #[arg(long)]
        kind: Option<String>,
        /// Return events after this sequence number.
        #[arg(long)]
        since: Option<u64>,
        /// Maximum events (default: 200); 0 returns no events.
        #[arg(long)]
        limit: Option<u64>,
    },
    /// Wait for ready work, a ticket, a lease or a change through the live feed.
    Wait(Wait),
    /// Read service state, usage and cutover status.
    State {
        /// Change administrative cutover state; ordinary ticket work does not need this.
        #[arg(long)]
        set_cutover: Option<Cutover>,
        /// Identify a managed pause. Subsequent transitions must name the same pause.
        #[arg(long, requires = "set_cutover")]
        pause_id: Option<String>,
    },
    /// Stream a full JSON Lines export.
    Export,
    /// Restore a ticket backup into a fresh project, or resume its restore.
    Restore {
        /// Path to a JSON Lines ticket backup.
        file: std::path::PathBuf,
        /// Lower the daily write ceiling.
        #[arg(long)]
        row_budget: Option<u64>,
    },

    /// Measure fresh-process read and write latency.
    Bench {
        /// Number of read/write samples.
        #[arg(long, default_value_t = 25, value_parser = clap::value_parser!(u32).range(1..))]
        samples: u32,
    },
    /// Register or rotate this machine’s write token through the Git remote.
    Register {
        /// Keep the current token and ensure it is registered in this repository.
        #[arg(long)]
        ensure: bool,
    },
    /// Print the client version without reading project configuration.
    Version,
    /// Print concise agent workflow, examples, JSON contracts and exit codes offline.
    Prime,
}

#[derive(Args)]
pub struct Ready {
    /// Only descendants of this ticket, at any depth.
    #[arg(long)]
    pub ancestor: Option<String>,
    /// Require every label; repeat or separate with commas.
    #[arg(long, short = 'l', value_delimiter = ',')]
    pub label: Vec<String>,
}

#[derive(Args)]
pub struct ReadyList {
    #[command(flatten)]
    pub filters: Ready,
    /// Maximum results; 0 or omitted returns every match.
    #[arg(long, short = 'n')]
    pub limit: Option<u64>,
}

#[derive(Args)]
pub struct List {
    #[command(flatten)]
    pub ready_filters: Ready,
    /// Filter by open, in_progress, blocked, deferred or closed.
    #[arg(long, short = 's')]
    pub status: Option<String>,
    /// Type: task, epic, bug, feature, chore, decision, spike, story or milestone.
    #[arg(long = "type", short = 't')]
    pub ticket_type: Option<String>,
    /// Match any of these labels; repeat or separate with commas.
    #[arg(long, value_delimiter = ',')]
    pub label_any: Vec<String>,
    /// Only direct children of this ticket.
    #[arg(long)]
    pub parent: Option<String>,
    /// Only tickets assigned to this actor.
    #[arg(long, short = 'a')]
    pub assignee: Option<String>,
    /// Include closed tickets, which are excluded by default.
    #[arg(long)]
    pub all: bool,
    /// Only tickets eligible to start under Enact readiness rules.
    #[arg(long)]
    pub ready: bool,
    /// Maximum results; 0 or omitted returns every match.
    #[arg(long, short = 'n')]
    pub limit: Option<u64>,
}

#[derive(Args)]
pub struct Search {
    /// Title text or an ID prefix; quoted phrases and separate words are accepted.
    #[arg(required = true)]
    pub words: Vec<String>,
    /// Filter by open, in_progress, blocked, deferred or closed.
    #[arg(long, short = 's')]
    pub status: Option<String>,
    /// Maximum results (default: 50); 0 returns every match.
    #[arg(long, short = 'n', default_value_t = 50)]
    pub limit: u64,
}

#[derive(Args)]
pub struct CreateTitle {
    /// Ticket title (alternative to the positional TITLE).
    #[arg(
        long,
        required_unless_present = "positional_title",
        conflicts_with = "positional_title"
    )]
    title: Option<String>,
    /// Ticket title (alternative to --title).
    #[arg(value_name = "TITLE")]
    positional_title: Option<String>,
}

impl CreateTitle {
    pub fn into_text(self) -> String {
        self.title
            .or(self.positional_title)
            .expect("title validated by parser")
    }
}

#[derive(Args)]
pub struct Create {
    #[command(flatten)]
    pub title: CreateTitle,
    /// Type: task, epic, bug, feature, chore, decision, spike, story or milestone.
    #[arg(long = "type", short = 't')]
    pub ticket_type: Option<String>,
    /// Priority 0–4 or P0–P4; 0 is most urgent (default on create: 2).
    #[arg(short = 'p', long, value_parser = priority)]
    pub priority: Option<u8>,
    /// Create as a direct child of this epic.
    #[arg(long)]
    pub parent: Option<String>,
    /// Labels to add; repeat or separate with commas.
    #[arg(long, short = 'l', value_delimiter = ',')]
    pub labels: Vec<String>,
    /// Read the complete ticket body from a UTF-8 file; - reads stdin.
    #[arg(long)]
    pub body_file: Option<PathBuf>,
    /// Ticket body text (alternative to --body-file).
    #[arg(long, short = 'd', conflicts_with = "body_file")]
    pub description: Option<String>,
    /// Acceptance criteria text.
    #[arg(long)]
    pub acceptance: Option<String>,
    /// Read design text from a UTF-8 file; - reads stdin.
    #[arg(long)]
    pub design_file: Option<std::path::PathBuf>,
    /// Dependencies: ID or KIND:ID; bare ID means this ticket depends on ID.
    #[arg(long, value_delimiter = ',')]
    pub deps: Vec<String>,
    /// Run after ordinary siblings in the same epic.
    #[arg(long)]
    pub last: bool,
    /// Print only the new ID; --json takes precedence.
    #[arg(long)]
    pub silent: bool,
}

pub(crate) fn priority(value: &str) -> Result<u8, String> {
    value
        .strip_prefix(['P', 'p'])
        .unwrap_or(value)
        .parse::<u8>()
        .ok()
        .filter(|value| *value <= 4)
        .ok_or_else(|| "expected priority 0–4 or P0–P4".into())
}

#[derive(Args)]
pub struct Update {
    /// Ticket ID: T-prefixed, bare native ID or imported alias.
    pub id: String,
    /// Replace the title.
    #[arg(long)]
    pub title: Option<String>,
    /// Set open, in_progress, blocked, deferred or closed.
    #[arg(long, short = 's')]
    pub status: Option<String>,
    /// Priority 0–4 or P0–P4; 0 is most urgent.
    #[arg(short = 'p', long, value_parser = priority)]
    pub priority: Option<u8>,
    /// Set the assignee; an empty string clears it.
    #[arg(long, short = 'a')]
    pub assignee: Option<String>,
    /// Move to this parent epic; an empty string detaches it.
    #[arg(long)]
    pub parent: Option<String>,
    /// Read the complete ticket body from a UTF-8 file; - reads stdin.
    #[arg(long)]
    pub body_file: Option<std::path::PathBuf>,
    /// Replace body text (alternative to --body-file).
    #[arg(long, short = 'd', conflicts_with = "body_file")]
    pub description: Option<String>,
    /// Read design text from a UTF-8 file; - reads stdin.
    #[arg(long)]
    pub design_file: Option<PathBuf>,
    /// Acceptance criteria text.
    #[arg(long)]
    pub acceptance: Option<String>,
    /// Replace notes text.
    #[arg(long)]
    pub notes: Option<String>,
    /// Add labels; repeat or separate with commas.
    #[arg(long, value_delimiter = ',')]
    pub add_label: Vec<String>,
    /// Remove labels; repeat or separate with commas.
    #[arg(long, value_delimiter = ',')]
    pub remove_label: Vec<String>,
    /// Set KEY=VALUE; booleans, numbers and null are parsed, other values stay text.
    #[arg(long)]
    pub set_metadata: Vec<String>,
    /// Remove a metadata key; repeat for several keys.
    #[arg(long)]
    pub unset_metadata: Vec<String>,
    /// Atomically claim for --actor and set in_progress; fail if already owned.
    #[arg(long, conflicts_with_all = ["assignee", "status"])]
    pub claim: bool,
    /// Run after ordinary siblings in the same epic.
    #[arg(long, conflicts_with = "no_last")]
    pub last: bool,
    /// Clear the run-after-siblings flag.
    #[arg(long)]
    pub no_last: bool,
    /// Fail with exit 3 unless the ticket version matches.
    #[arg(long)]
    pub if_version: Option<u64>,
    /// Fail with exit 3 unless the assignee matches; empty means unassigned.
    #[arg(long)]
    pub if_assignee: Option<String>,
}

#[derive(Subcommand)]
pub enum CommentOperation {
    /// Append a comment without opening an editor.
    Add {
        /// Ticket ID: T-prefixed, bare native ID or imported alias.
        id: String,
        /// Comment text (alternative to --body-file).
        #[arg(required_unless_present = "body_file")]
        text: Option<String>,
        /// Read comment text from a UTF-8 file; - reads stdin.
        #[arg(long, short = 'f', visible_alias = "file", conflicts_with = "text")]
        body_file: Option<PathBuf>,
    },
}

#[derive(Args)]
pub struct Edge {
    /// Ticket ID: T-prefixed, bare native ID or imported alias.
    pub id: String,
    /// Prerequisite ticket: ID depends on ON for a blocking edge.
    pub on: String,
    /// Edge kind: blocks (default), conditional-blocks, waits-for, related or discovered-from.
    #[arg(long = "type", short = 't')]
    pub kind: Option<String>,
}

#[derive(Subcommand)]
pub enum EdgeOperation {
    /// Add an edge; for blocks, ID cannot start until ON finishes.
    Add(Edge),
    /// Remove an edge (blocks by default).
    Remove(Edge),
    /// Detect cycles in blocking dependencies.
    Cycles,
}

#[derive(Clone, Copy, ValueEnum)]
pub enum LeaseOperation {
    Acquire,
    Renew,
    Release,
}

#[derive(Clone, Copy, ValueEnum)]
pub enum Cutover {
    Before,
    Paused,
    After,
}

#[derive(Args)]
pub struct Wait {
    /// Maximum wait in seconds (default: 120); exit 6 if it expires.
    #[arg(long, global = true, value_parser = seconds)]
    pub timeout: Option<f64>,
    /// Resume after a sequence returned by an earlier read.
    #[arg(long, global = true)]
    pub since: Option<u64>,
    #[command(subcommand)]
    pub mode: WaitMode,
}

#[derive(Subcommand)]
pub enum WaitMode {
    /// Wait until at least one ticket is ready.
    Ready(Ready),
    /// Wait for a ticket change, or until it has --status.
    Ticket {
        /// Ticket ID: T-prefixed, bare native ID or imported alias.
        id: String,
        /// Filter by open, in_progress, blocked, deferred or closed.
        #[arg(long)]
        status: Option<String>,
    },
    /// Wait until the named lease is available.
    Lease {
        /// Lease name.
        name: String,
    },
    /// Wait for any project change after --since (or the current sequence).
    Change,
}

#[cfg(test)]
mod tests {
    use super::Options;
    use clap::CommandFactory;

    #[test]
    fn every_command_and_argument_explains_itself() {
        fn inspect(command: &mut clap::Command) {
            assert!(
                command.get_about().is_some(),
                "{} has no description",
                command.get_name()
            );
            command.build();
            for argument in command.get_arguments() {
                assert!(
                    argument.get_help().is_some(),
                    "{} {} has no help",
                    command.get_name(),
                    argument.get_id()
                );
            }
            assert!(command.render_long_help().to_string().contains("Usage:"));
            for child in command
                .get_subcommands_mut()
                .filter(|child| child.get_name() != "help")
            {
                inspect(child);
            }
        }
        let mut command = Options::command();
        command.debug_assert();
        command = Options::command();
        inspect(&mut command);
    }
}
