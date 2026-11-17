mod create;
mod diagnostics;
mod output;
pub(crate) mod state;

use crate::client::{Client, Failure, Result, usage};
use crate::commands;
use clap::{Parser, Subcommand};
use serde_json::{Value, json};
use std::env;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

#[derive(Parser)]
#[command(name = "bd", disable_help_flag = true, disable_version_flag = true)]
struct Options {
    #[arg(long, global = true)]
    json: bool,
    #[arg(long, global = true)]
    quiet: bool,
    #[arg(long, global = true)]
    actor: Option<String>,
    #[arg(long, short = 'C', global = true)]
    directory: Option<PathBuf>,
    #[arg(long, global = true)]
    readonly: bool,
    // Sandbox disables Beads' Dolt auto-push. Ticket reads have no auto-push.
    #[arg(long, global = true)]
    sandbox: bool,
    #[command(subcommand)]
    action: Action,
}

#[derive(Subcommand)]
enum Action {
    List {
        #[arg(long)]
        status: Option<String>,
        #[arg(long = "type", short = 't')]
        kind: Option<String>,
        #[arg(long)]
        label: Vec<String>,
        #[arg(long)]
        assignee: Option<String>,
        #[arg(long)]
        parent: Option<String>,
        #[arg(long, short = 'n')]
        limit: Option<usize>,
        #[arg(long)]
        all: bool,
        #[arg(long)]
        brief: bool,
        #[arg(long)]
        flat: bool,
        #[arg(long)]
        title: Option<String>,
        #[arg(long, value_delimiter = ',')]
        id: Vec<String>,
    },
    Ready {
        #[arg(long)]
        label: Vec<String>,
        #[arg(long)]
        parent: Option<String>,
        #[arg(long = "type", short = 't')]
        kind: Option<String>,
        #[arg(long)]
        brief: bool,
        #[arg(long)]
        explain: bool,
        #[arg(long, value_delimiter = ',')]
        exclude_type: Vec<String>,
        #[arg(long, short = 'u')]
        unassigned: bool,
        #[arg(long, short = 'n', default_value_t = 100)]
        limit: usize,
    },
    Show {
        ids: Vec<String>,
    },
    Export {
        #[arg(long, short = 'o')]
        output: Option<PathBuf>,
        #[arg(long, short = 'h')]
        help: bool,
        #[arg(long)]
        all: bool,
        #[arg(long)]
        exclude_owner: Vec<String>,
        #[arg(long)]
        include_infra: bool,
        #[arg(long)]
        include_memories: bool,
        #[arg(long)]
        scrub: bool,
        #[arg(long)]
        verbose: bool,
    },
    Create(create::Create),
    Update {
        id: String,
        #[arg(long)]
        status: Option<String>,
        #[arg(long)]
        assignee: Option<String>,
        #[arg(long)]
        set_metadata: Vec<String>,
    },
    Close {
        id: String,
        #[arg(long)]
        reason: String,
        #[arg(long)]
        force: bool,
    },
    Dep {
        #[command(subcommand)]
        operation: Dependency,
    },
    Label {
        #[command(subcommand)]
        operation: Label,
    },
    Comments {
        arguments: Vec<String>,
    },
    Epic {
        #[command(subcommand)]
        operation: Epic,
    },
    Defer {
        id: String,
        #[arg(long)]
        until: String,
        #[arg(long)]
        reason: Option<String>,
    },
    Undefer {
        id: String,
    },
    Dolt {
        #[command(subcommand)]
        operation: Dolt,
    },
}

#[derive(Subcommand)]
enum Dependency {
    Add {
        id: String,
        on: String,
        #[arg(long = "type", default_value = "blocks")]
        kind: String,
    },
    Remove {
        id: String,
        on: String,
    },
    List {
        ids: Vec<String>,
        #[arg(long, default_value = "down", value_parser = ["up", "down"])]
        direction: String,
        #[arg(long = "type", short = 't')]
        kind: Option<String>,
    },
    Tree {
        id: String,
    },
}

#[derive(Subcommand)]
enum Label {
    Add { id: String, label: String },
    Remove { id: String, label: String },
    List { id: String },
}

#[derive(Subcommand)]
enum Epic {
    Status,
    CloseEligible,
}

#[derive(Subcommand)]
enum Dolt {
    Remote {
        #[command(subcommand)]
        operation: Remote,
    },
}

#[derive(Subcommand)]
enum Remote {
    List,
}

impl Action {
    fn write_name(&self) -> &str {
        match self {
            Self::Create(_) => "create",
            Self::Update { .. } => "update",
            Self::Close { .. } => "close",
            Self::Dep { .. } => "dep",
            Self::Label { .. } => "label",
            Self::Comments { .. } => "comments",
            Self::Epic { .. } => "epic",
            Self::Defer { .. } => "defer",
            Self::Undefer { .. } => "undefer",
            _ => unreachable!("read-only commands are already accepted"),
        }
    }

    fn reads_only(&self) -> bool {
        match self {
            Self::List { .. }
            | Self::Ready { .. }
            | Self::Show { .. }
            | Self::Export { .. }
            | Self::Dep {
                operation: Dependency::List { .. } | Dependency::Tree { .. },
            }
            | Self::Label {
                operation: Label::List { .. },
            }
            | Self::Epic {
                operation: Epic::Status,
            }
            | Self::Dolt { .. } => true,
            Self::Comments { arguments } => arguments.len() == 1 && arguments[0] != "add",
            _ => false,
        }
    }
}

pub fn invoked_as_bd() -> bool {
    env::args_os().next().is_some_and(|arg| {
        Path::new(&arg)
            .file_stem()
            .is_some_and(|name| name.eq_ignore_ascii_case("bd"))
    })
}

pub fn main() -> ExitCode {
    let args: Vec<_> = env::args_os().collect();
    let options = Options::try_parse_from(&args);
    let reads = options
        .as_ref()
        .is_ok_and(|parsed| parsed.readonly || parsed.action.reads_only());
    let directory = options
        .as_ref()
        .ok()
        .and_then(|parsed| parsed.directory.clone());
    let result = if legacy_help(&args[1..]) {
        // The installed Beads binary owns its static usage and flag descriptions.
        state::real(&args[1..])
    } else {
        state::dispatch(&args[1..], reads, directory.as_deref(), || {
            options.map_err(|_| unsupported(&args[1..]))
        })
    };
    match result {
        Ok(code) => ExitCode::from(code),
        Err(error) => {
            eprintln!("{}", error.message);
            ExitCode::from(error.code)
        }
    }
}

fn legacy_help(args: &[OsString]) -> bool {
    match args {
        [command, help] if help == "--help" || help == "-h" => {
            ["comments", "create", "list", "ready", "undefer"]
                .iter()
                .any(|name| command == *name)
        }
        [command, subcommand, help] if help == "--help" || help == "-h" => {
            (command == "dep" && (subcommand == "list" || subcommand == "remove"))
                || (command == "comments" && subcommand == "add")
        }
        _ => false,
    }
}

fn unsupported(args: &[OsString]) -> Failure {
    let command = args
        .iter()
        .scan(false, |skip, arg| {
            let is_value = std::mem::take(skip);
            *skip = arg == "--actor";
            Some((!is_value && !arg.to_string_lossy().starts_with('-')).then_some(arg))
        })
        .flatten()
        .next()
        .map_or_else(String::new, |arg| arg.to_string_lossy().into_owned());
    let native = match command.as_str() {
        "dep" => "dep",
        "label" | "undefer" => "update",
        "comments" => "comment",
        "epic" => "list --type epic",
        "init" | "dolt" => "state",
        "list" | "ready" | "show" | "create" | "update" | "close" | "defer" | "reopen" => &command,
        "export" => return retired_export(),
        _ => "--help",
    };
    usage(format!(
        "bd: unsupported arguments for {command:?}; use tk {native}"
    ))
}

fn retired_export() -> Failure {
    usage(
        "bd: full exports are disabled after ticket cutover. Use the ticket dashboard or targeted tk list/show reads.",
    )
}

fn git_config(key: &str) -> Option<String> {
    let result = Command::new("git")
        .args(["config", "--get", key])
        .output()
        .ok()?;
    result.status.success().then_some(())?;
    let text = String::from_utf8(result.stdout).ok()?;
    Some(text.trim().to_owned()).filter(|text| !text.is_empty())
}

fn ticket(client: &Client, id: &str, options: &[(&str, String)]) -> Result<Value> {
    commands::read(client, &["tickets", id], options)
}

fn array(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}

fn string(value: &Value) -> &str {
    value.as_str().unwrap_or_default()
}

fn update(client: &Client, id: &str, fields: Value) -> Result<Value> {
    commands::single(
        client,
        json!({"op": "update", "id": id, "set": fields}),
        vec![],
    )?;
    Ok(json!([output::row(client, &ticket(client, id, &[])?)]))
}

fn defer_until(until: String) -> Result<String> {
    use chrono::{Local, NaiveDate, TimeZone};
    let Ok(date) = NaiveDate::parse_from_str(&until, "%Y-%m-%d") else {
        return Ok(until);
    };
    let midnight = date.and_hms_opt(0, 0, 0).expect("midnight");
    Ok(Local
        .from_local_datetime(&midnight)
        .earliest()
        .ok_or_else(|| usage("invalid local date"))?
        .to_utc()
        .to_rfc3339_opts(chrono::SecondsFormat::Secs, true))
}

fn run(client: &Client, options: Options) -> Result<u8> {
    if options.readonly && !options.action.reads_only() {
        let command = options.action.write_name();
        return Err(usage(format!(
            "Error: operation '{command}' is not allowed in read-only mode"
        )));
    }
    let mut rendered = None;
    let answer = match options.action {
        Action::List {
            status,
            kind,
            label,
            assignee,
            parent,
            limit,
            all,
            brief,
            flat: _,
            title,
            id,
        } => {
            let mut query = vec![("beads", "1".into())];
            for (key, value) in [
                ("status", status),
                ("type", kind),
                ("assignee", assignee),
                ("parent", parent),
                ("title", title),
            ] {
                if let Some(value) = value {
                    query.push((key, value));
                }
            }
            for value in label {
                query.push(("label", value));
            }
            query.extend(id.into_iter().map(|id| ("id", id)));
            if all {
                query.push(("all", "1".into()));
            }
            output::listing(
                client,
                query,
                limit.unwrap_or(0),
                false,
                options.quiet || options.json,
                brief,
            )?
        }
        Action::Ready {
            label,
            exclude_type,
            unassigned,
            limit,
            parent,
            kind,
            brief,
            explain,
        } => {
            if explain {
                return diagnostics::explain(client, options.json);
            }
            let mut query = vec![("beads", "1".into()), ("ready", "1".into())];
            query.extend(label.into_iter().map(|value| ("label", value)));
            query.extend(
                exclude_type
                    .into_iter()
                    .map(|value| ("exclude_type", value)),
            );
            if unassigned {
                query.push(("unassigned", "1".into()));
            }
            query.extend(parent.map(|parent| ("ancestor", parent)));
            query.extend(kind.map(|kind| ("type", kind)));
            output::listing(client, query, limit, true, options.quiet, brief)?
        }
        Action::Show { ids } => return output::show(client, &ids, options.json),
        Action::Export { .. } => return Err(retired_export()),
        Action::Create(create) => return create.run(client, options.json),
        Action::Update {
            id,
            status,
            assignee,
            set_metadata,
        } => {
            let before = ticket(client, &id, &[])?;
            let mut fields = json!({});
            let mut operation = json!({"op": "update", "id": id});
            let (mut metadata, mut unset) = (serde_json::Map::new(), Vec::new());
            if let Some(status) = status {
                if status == "in_progress"
                    && before["status"] != "in_progress"
                    && before["metadata"]["_enact_beads"]["started_at"].is_null()
                {
                    let mut legacy = before["metadata"]["_enact_beads"]
                        .as_object()
                        .cloned()
                        .unwrap_or_default();
                    legacy.insert(
                        "started_at".into(),
                        json!(
                            chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
                        ),
                    );
                    metadata.insert("_enact_beads".into(), json!(legacy));
                }
                fields["status"] = json!(status);
            }
            if let Some(assignee) = assignee {
                fields["assignee"] = if assignee.is_empty() {
                    Value::Null
                } else {
                    json!(assignee)
                };
            }
            for assignment in set_metadata {
                let (key, value) = assignment
                    .split_once('=')
                    .ok_or_else(|| usage("--set-metadata expects key=value"))?;
                if key.is_empty() || key == "_enact_beads" {
                    return Err(usage("invalid metadata key"));
                }
                if value.is_empty() {
                    unset.push(key.to_owned());
                } else {
                    metadata.insert(
                        key.to_owned(),
                        serde_json::from_str(value).unwrap_or_else(|_| json!(value)),
                    );
                }
            }
            operation["set"] = fields;
            operation["set_meta"] = json!(metadata);
            operation["unset_meta"] = json!(unset);
            commands::single(
                client,
                operation,
                vec![json!({"id": id, "version": before["version"]})],
            )?;
            json!([output::row(client, &ticket(client, &id, &[])?)])
        }
        Action::Close { id, reason, force } => {
            let before = ticket(client, &id, &[])?;
            if !force
                && let Some(assignee) = before["assignee"].as_str()
                && assignee != client.actor
            {
                return Err(usage(format!(
                    "cannot close {id}: assignee is {assignee:?}, actor is {:?}; reclaim or use --force to override",
                    client.actor
                )));
            }
            commands::single(
                client,
                json!({"op": "close", "id": id, "reason": reason, "legacy": true}),
                vec![json!({"id": id, "version": before["version"]})],
            )?;
            if !options.json {
                println!(
                    "✓ Closed {} — {}: {reason}",
                    output::legacy_id(client, string(&before["id"])),
                    string(&before["title"]),
                );
                return Ok(0);
            }
            json!([output::row(client, &ticket(client, &id, &[])?)])
        }
        Action::Dep { operation } => dependency(client, operation)?,
        Action::Label { operation } => label(client, operation)?,
        Action::Comments { arguments } => {
            let query = [("comments", "1".into())];
            match arguments.as_slice() {
                [command, ..] if command == "list" => {
                    let message = concat!(
                        "\"bd comments list\" is not valid.\n\n",
                        "To list comments on an issue, run:\n  bd comments <issue-id>\n\n",
                        "Example:\n  bd comments bd-123\n\nSee: bd comments --help"
                    );
                    if options.json {
                        output::json(&json!({"error": message, "schema_version": 1}))?;
                        return Ok(1);
                    }
                    return Err(usage(message));
                }
                [id] if id != "add" => {
                    let ticket = ticket(client, id, &query)?;
                    json!(
                        array(&ticket["comments"])
                            .iter()
                            .map(|comment| output::comment(client, comment))
                            .collect::<Vec<_>>()
                    )
                }
                [sub, id, text] if sub == "add" => {
                    let reply = commands::single(
                        client,
                        json!({"op": "comment", "id": id, "text": text, "legacy": true}),
                        vec![],
                    )?;
                    let comment = &reply["results"][0]["comment"];
                    if !comment.is_object() {
                        return Err(usage("batch reply did not include the created comment"));
                    }
                    let mut row = output::comment(client, comment);
                    row["schema_version"] = json!(1);
                    rendered = Some(format!("Comment added to {}\n", string(&row["issue_id"])));
                    row
                }
                _ => {
                    return Err(usage(
                        "bd: unsupported comments arguments; use tk comment or tk show --comments",
                    ));
                }
            }
        }
        Action::Epic { operation } => epic(client, operation)?,
        Action::Defer { id, until, reason } => {
            let until = defer_until(until)?;
            let mut fields = json!({"status": "deferred", "defer_until": until});
            if let Some(reason) = reason {
                fields["notes"] = json!(reason);
            }
            update(client, &id, fields)?
        }
        Action::Undefer { id } => {
            update(client, &id, json!({"status": "open", "defer_until": null}))?
        }
        Action::Dolt { .. } => {
            rendered = Some("No remotes configured.\n".into());
            json!([])
        }
    };
    if options.json {
        output::json(&answer)?;
    } else if let Some(text) = rendered {
        print!("{text}");
    } else {
        output::human(&answer)?;
    }
    Ok(0)
}

fn dependency(client: &Client, operation: Dependency) -> Result<Value> {
    let (id, on, kind) = match operation {
        Dependency::List {
            ids,
            direction,
            kind,
        } => {
            let mut rows = Vec::new();
            for id in ids {
                let source = ticket(client, &id, &[])?;
                rows.extend(output::related(
                    client,
                    &source,
                    direction == "up",
                    kind.as_deref(),
                )?);
            }
            return Ok(json!(rows));
        }
        Dependency::Tree { id } => return output::tree(client, &id),
        Dependency::Add { id, on, kind } => (id, on, Some(kind)),
        Dependency::Remove { id, on } => (id, on, None),
    };
    let mut op =
        json!({"op": if kind.is_some() { "dep_add" } else { "dep_remove" }, "id": id, "on": on});
    if let Some(kind) = &kind {
        op["kind"] = json!(if kind == "relates-to" {
            "related"
        } else {
            kind
        });
    }
    commands::single(client, op, vec![])?;
    let mut row = json!({"depends_on_id": output::legacy_id(client, &on), "issue_id": output::legacy_id(client, &id),
        "schema_version": 1, "status": if kind.is_some() { "added" } else { "removed" }});
    if let Some(kind) = kind {
        row["type"] = json!(kind);
    }
    Ok(row)
}

fn label(client: &Client, operation: Label) -> Result<Value> {
    let (id, label, add) = match operation {
        Label::List { id } => {
            return Ok(json!(array(
                &output::row(client, &ticket(client, &id, &[])?)["labels"]
            )));
        }
        Label::Add { id, label } => (id, label, true),
        Label::Remove { id, label } => (id, label, false),
    };
    let mut operation = json!({"op": "update", "id": id});
    operation[if add { "add_labels" } else { "remove_labels" }] = json!([label]);
    commands::single(client, operation, vec![])?;
    Ok(
        json!([{"issue_id": output::legacy_id(client, &id), "label": label, "status": if add { "added" } else { "removed" }}]),
    )
}

fn epic(client: &Client, operation: Epic) -> Result<Value> {
    let is_status = matches!(operation, Epic::Status);
    let sequence = if is_status {
        Value::Null
    } else {
        commands::read(client, &["state"], &[])?["seq"].clone()
    };
    let rows = commands::read(
        client,
        &["tickets"],
        &[("type", "epic".into()), ("epic_counts", "1".into())],
    )?;
    let eligible = |row: &Value| {
        row["total_children"].as_u64().unwrap_or(0) > 0
            && row["total_children"] == row["closed_children"]
    };
    if is_status {
        return Ok(json!(
            array(&rows)
                .iter()
                .filter(|row| row["total_children"].as_u64().unwrap_or(0) > 0)
                .map(|row| json!({
                    "epic": output::row(client, row), "total_children": row["total_children"],
                    "closed_children": row["closed_children"], "eligible_for_close": eligible(row),
                }))
                .collect::<Vec<_>>()
        ));
    }
    let reason = "All children completed";
    let candidates: Vec<_> = array(&rows).iter().filter(|row| eligible(row)).collect();
    let operations: Vec<_> = candidates
        .iter()
        .map(|row| json!({"op": "close", "id": row["id"], "reason": reason, "legacy": true}))
        .collect();
    if !operations.is_empty() {
        commands::batch(
            client,
            json!({"if": [{"seq": sequence}], "ops": operations}),
        )?;
    }
    let closed: Vec<_> = candidates
        .iter()
        .map(|row| output::legacy_id(client, string(&row["id"])))
        .collect();
    Ok(json!({"closed": closed, "count": closed.len(), "reason": reason, "schema_version": 1}))
}
