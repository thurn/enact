use clap::{Args, Parser, Subcommand, ValueEnum};

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
        id: String,
        #[arg(long)]
        comments: bool,
        #[arg(long)]
        history: bool,
    },
    List(List),
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
    Comment {
        id: String,
        #[arg(required_unless_present = "body_file")]
        text: Option<String>,
        #[arg(long, conflicts_with = "text")]
        body_file: Option<std::path::PathBuf>,
    },
    Dep {
        operation: EdgeOperation,
        id: String,
        on: String,
        #[arg(long)]
        kind: Option<String>,
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
    },
    /// Stream a full JSON Lines export.
    Export,
    /// Copy a legacy Beads JSON Lines export and its audit history.
    Import {
        file: std::path::PathBuf,
        #[arg(long)]
        history_dir: Option<std::path::PathBuf>,
        /// Override the daily import ceiling for a local rehearsal.
        #[arg(long)]
        row_budget: Option<u64>,
    },
    /// Check the legacy Beads store against the ticket server.
    Compare {
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
    #[arg(long)]
    pub label: Vec<String>,
}

#[derive(Args)]
pub struct List {
    #[command(flatten)]
    pub ready_filters: Ready,
    #[arg(long)]
    pub status: Option<String>,
    #[arg(long = "type")]
    pub ticket_type: Option<String>,
    #[arg(long)]
    pub parent: Option<String>,
    #[arg(long)]
    pub assignee: Option<String>,
    #[arg(long)]
    pub all: bool,
    #[arg(long)]
    pub ready: bool,
    #[arg(long)]
    pub limit: Option<u64>,
}

#[derive(Args)]
pub struct Create {
    #[arg(long)]
    pub title: String,
    #[arg(long = "type")]
    pub ticket_type: Option<String>,
    #[arg(short = 'p', long, value_parser = clap::value_parser!(u8).range(0..=4))]
    pub priority: Option<u8>,
    #[arg(long)]
    pub parent: Option<String>,
    #[arg(long, value_delimiter = ',')]
    pub labels: Vec<String>,
    #[arg(long)]
    pub body_file: Option<std::path::PathBuf>,
    #[arg(long)]
    pub acceptance: Option<String>,
    #[arg(long)]
    pub design_file: Option<std::path::PathBuf>,
    #[arg(long)]
    pub dep: Vec<String>,
    #[arg(long)]
    pub last: bool,
}

#[derive(Args)]
pub struct Update {
    pub id: String,
    #[arg(long)]
    pub title: Option<String>,
    #[arg(long)]
    pub status: Option<String>,
    #[arg(short = 'p', long, value_parser = clap::value_parser!(u8).range(0..=4))]
    pub priority: Option<u8>,
    #[arg(long)]
    pub assignee: Option<String>,
    #[arg(long)]
    pub parent: Option<String>,
    #[arg(long)]
    pub body_file: Option<std::path::PathBuf>,
    #[arg(long)]
    pub acceptance: Option<String>,
    #[arg(long)]
    pub notes: Option<String>,
    #[arg(long)]
    pub add_label: Vec<String>,
    #[arg(long)]
    pub remove_label: Vec<String>,
    #[arg(long)]
    pub set: Vec<String>,
    #[arg(long)]
    pub unset: Vec<String>,
    #[arg(long, conflicts_with = "no_last")]
    pub last: bool,
    #[arg(long)]
    pub no_last: bool,
    #[arg(long)]
    pub if_version: Option<u64>,
    #[arg(long)]
    pub if_assignee: Option<String>,
}

#[derive(Clone, Copy, ValueEnum)]
pub enum EdgeOperation {
    Add,
    Remove,
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
