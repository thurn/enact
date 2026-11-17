use clap::{Args, Parser, Subcommand, ValueEnum};
use std::path::PathBuf;

#[derive(Parser)]
#[command(about = "Read and update Enact tickets")]
pub struct Options {
    #[arg(long, global = true)]
    pub project: Option<String>,
    #[arg(long, global = true, default_value = "tk")]
    pub actor: String,
    #[arg(long, global = true)]
    pub json: bool,
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
    Show {
        #[arg(required = true)]
        ids: Vec<String>,
        #[arg(long)]
        comments: bool,
        #[arg(long)]
        history: bool,
    },
    List(List),
    Children {
        id: String,
    },
    /// Search ticket titles and ID prefixes, including closed tickets.
    Search(Search),
    Ready(Ready),
    Graph {
        #[arg(long)]
        if_none_match: Option<String>,
        #[arg(long)]
        all: bool,
    },
    Create(Create),
    Update(Update),
    Claim {
        id: String,
    },
    Close {
        id: String,
        #[arg(long)]
        reason: String,
    },
    Reopen {
        id: String,
    },
    Defer {
        id: String,
        #[arg(long)]
        until: String,
    },
    #[command(subcommand_negates_reqs = true, args_conflicts_with_subcommands = true)]
    Comments {
        #[arg(required = true)]
        id: Option<String>,
        #[command(subcommand)]
        operation: Option<CommentOperation>,
    },
    Dep {
        #[command(subcommand)]
        operation: EdgeOperation,
    },
    /// Send a batch from standard input.
    Batch,
    Lease {
        operation: LeaseOperation,
        name: String,
        #[arg(long, default_value_t = 180)]
        ttl: u64,
        #[arg(long)]
        holder: Option<String>,
        #[arg(long)]
        payload: Option<String>,
        #[arg(long)]
        if_revision: Option<String>,
        #[arg(long)]
        no_queue: bool,
    },
    Leases,
    Event {
        kind: String,
        data: String,
    },
    Events {
        #[arg(long)]
        kind: Option<String>,
        #[arg(long)]
        since: Option<u64>,
        #[arg(long)]
        limit: Option<u64>,
    },
    Wait(Wait),
    State {
        #[arg(long)]
        set_cutover: Option<Cutover>,
        /// Identify a managed pause. Subsequent transitions must name the same pause.
        #[arg(long, requires = "set_cutover")]
        pause_id: Option<String>,
    },
    /// Publish this machine's in-flight Beads writes during a cutover pause.
    Drain,
    /// Stream a full JSON Lines export.
    Export,
    /// Copy a legacy Beads JSON Lines export and its audit history.
    Import {
        #[arg(required_unless_present = "frozen_beads")]
        file: Option<std::path::PathBuf>,
        /// Read tickets and audit events from an isolated copy of the Beads checkout.
        #[arg(long, conflicts_with_all = ["file", "history_dir"])]
        frozen_beads: Option<std::path::PathBuf>,
        #[arg(long)]
        history_dir: Option<std::path::PathBuf>,
        /// Override the daily import ceiling for a local rehearsal.
        #[arg(long)]
        row_budget: Option<u64>,
        /// Import only legacy changes since this archive, after cutover.
        #[arg(long, requires = "frozen_beads")]
        since_frozen_beads: Option<std::path::PathBuf>,
        /// Fence the final import to the active managed pause.
        #[arg(long, requires = "frozen_beads", conflicts_with = "since_frozen_beads")]
        pause_id: Option<String>,
        /// Fence a staged import to the period when Beads is authoritative.
        #[arg(long, conflicts_with_all = ["pause_id", "since_frozen_beads"])]
        before_cutover: bool,
    },
    /// Restore a ticket backup into a fresh project, or resume its restore.
    Restore {
        file: std::path::PathBuf,
        /// Lower the daily write ceiling.
        #[arg(long)]
        row_budget: Option<u64>,
    },
    /// Check the legacy Beads store against the ticket server.
    Compare {
        /// Verify tickets, readiness and audit events against an isolated Beads copy.
        #[arg(long, conflicts_with = "history_dir")]
        frozen_beads: Option<std::path::PathBuf>,
        #[arg(long)]
        history_dir: Option<std::path::PathBuf>,
    },
    /// Measure fresh-process read and write latency.
    Bench {
        #[arg(long, default_value_t = 25, value_parser = clap::value_parser!(u32).range(1..))]
        samples: u32,
    },
    /// Register or rotate this machine’s write token through the Git remote.
    Register {
        /// Keep the current token and ensure it is registered in this repository.
        #[arg(long)]
        ensure: bool,
    },
    Version,
}

#[derive(Args)]
pub struct Ready {
    #[arg(long)]
    pub ancestor: Option<String>,
    #[arg(long, short = 'l', value_delimiter = ',')]
    pub label: Vec<String>,
}

#[derive(Args)]
pub struct List {
    #[command(flatten)]
    pub ready_filters: Ready,
    #[arg(long, short = 's')]
    pub status: Option<String>,
    #[arg(long = "type", short = 't')]
    pub ticket_type: Option<String>,
    #[arg(long, value_delimiter = ',')]
    pub label_any: Vec<String>,
    #[arg(long)]
    pub parent: Option<String>,
    #[arg(long, short = 'a')]
    pub assignee: Option<String>,
    #[arg(long)]
    pub all: bool,
    #[arg(long)]
    pub ready: bool,
    #[arg(long, short = 'n')]
    pub limit: Option<u64>,
}

#[derive(Args)]
pub struct Search {
    #[arg(required = true)]
    pub words: Vec<String>,
    #[arg(long, short = 's')]
    pub status: Option<String>,
    #[arg(long, short = 'n', default_value_t = 50)]
    pub limit: u64,
}

#[derive(Args)]
pub struct CreateTitle {
    #[arg(long, required_unless_present = "positional_title")]
    title: Option<String>,
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
    #[arg(long = "type", short = 't')]
    pub ticket_type: Option<String>,
    #[arg(short = 'p', long, value_parser = priority)]
    pub priority: Option<u8>,
    #[arg(long)]
    pub parent: Option<String>,
    #[arg(long, short = 'l', value_delimiter = ',')]
    pub labels: Vec<String>,
    #[arg(long)]
    pub body_file: Option<PathBuf>,
    #[arg(long, short = 'd')]
    pub description: Option<String>,
    #[arg(long)]
    pub acceptance: Option<String>,
    #[arg(long)]
    pub design_file: Option<std::path::PathBuf>,
    #[arg(long, value_delimiter = ',')]
    pub deps: Vec<String>,
    #[arg(long)]
    pub last: bool,
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
    pub id: String,
    #[arg(long)]
    pub title: Option<String>,
    #[arg(long, short = 's')]
    pub status: Option<String>,
    #[arg(short = 'p', long, value_parser = priority)]
    pub priority: Option<u8>,
    #[arg(long, short = 'a')]
    pub assignee: Option<String>,
    #[arg(long)]
    pub parent: Option<String>,
    #[arg(long)]
    pub body_file: Option<std::path::PathBuf>,
    #[arg(long)]
    pub acceptance: Option<String>,
    #[arg(long)]
    pub notes: Option<String>,
    #[arg(long, value_delimiter = ',')]
    pub add_label: Vec<String>,
    #[arg(long, value_delimiter = ',')]
    pub remove_label: Vec<String>,
    #[arg(long)]
    pub set_metadata: Vec<String>,
    #[arg(long)]
    pub unset_metadata: Vec<String>,
    #[arg(long, conflicts_with_all = ["assignee", "status"])]
    pub claim: bool,
    #[arg(long, conflicts_with = "no_last")]
    pub last: bool,
    #[arg(long)]
    pub no_last: bool,
    #[arg(long)]
    pub if_version: Option<u64>,
    #[arg(long)]
    pub if_assignee: Option<String>,
}

#[derive(Subcommand)]
pub enum CommentOperation {
    Add {
        id: String,
        #[arg(required_unless_present = "body_file")]
        text: Option<String>,
        #[arg(long, short = 'f', visible_alias = "file", conflicts_with = "text")]
        body_file: Option<PathBuf>,
    },
}

#[derive(Args)]
pub struct Edge {
    pub id: String,
    pub on: String,
    #[arg(long = "type", short = 't')]
    pub kind: Option<String>,
}

#[derive(Subcommand)]
pub enum EdgeOperation {
    Add(Edge),
    Remove(Edge),
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
    Ready(Ready),
    Ticket {
        id: String,
        #[arg(long)]
        status: Option<String>,
    },
    Lease {
        name: String,
    },
    Change,
}
