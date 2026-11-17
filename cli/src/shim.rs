mod create;
mod diagnostics;
mod output;
pub(crate) mod state;
mod update;

use crate::client::{Client, Failure, Result, usage};
use crate::commands;
use clap::{Parser, Subcommand};
use serde_json::{Value, json};
use std::env;
use std::ffi::OsString;
use std::io::{self, Read};
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
        #[arg(long, value_delimiter = ',')]
        label_any: Vec<String>,
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
    Search(crate::args::Search),
    Children {
        id: String,
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
    Update(update::Update),
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
        #[arg(long, short = 'f')]
        file: Option<PathBuf>,
    },
    Epic {
        #[command(subcommand)]
        operation: Epic,
    },
    Defer {
        id: String,
        #[arg(long)]
        until: Option<String>,
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
    Cycles,
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
            Self::Update(_) => "update",
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
            | Self::Search(_)
            | Self::Children { .. }
            | Self::Ready { .. }
            | Self::Show { .. }
            | Self::Export { .. }
            | Self::Dep {
                operation: Dependency::List { .. } | Dependency::Tree { .. } | Dependency::Cycles,
            }
            | Self::Label {
                operation: Label::List { .. },
            }
            | Self::Epic {
                operation: Epic::Status,
            }
            | Self::Dolt { .. } => true,
            Self::Comments { arguments, file } => {
                file.is_none() && arguments.len() == 1 && arguments[0] != "add"
            }
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
            ["comments", "create", "list", "ready", "undefer", "update"]
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
        "children" => "list --parent",
        "label" | "undefer" => "update",
        "comments" => "comment",
        "epic" => "list --type epic",
        "init" | "dolt" => "state",
        "list" | "ready" | "show" | "create" | "update" | "close" | "defer" | "reopen"
        | "search" => &command,
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

fn set_deferral(
    client: &Client,
    id: &str,
    mut fields: Value,
    reason: Option<&str>,
) -> Result<Option<Value>> {
    for attempt in 0..=2 {
        let before = ticket(client, id, &[])?;
        if fields["status"] == "open" && before["status"] != "deferred" {
            return Ok(None);
        }
        if let Some(reason) = reason.filter(|text| !text.is_empty()) {
            let notes = string(&before["notes"]);
            fields["notes"] = if notes.is_empty() {
                json!(reason)
            } else {
                json!(format!("{notes}\n{reason}"))
            };
        }
        // Keep a concurrent note or status edit when applying the change.
        let condition = json!({"id": id, "version": before["version"]});
        let operation = json!({"op": "update", "id": id, "set": fields});
        match commands::single(client, operation, vec![condition]) {
            Ok(_) => return Ok(Some(output::row(client, &ticket(client, id, &[])?))),
            Err(error) if error.status == Some(409) && attempt < 2 => continue,
            Err(error) => return Err(error),
        }
    }
    unreachable!("the final update attempt returns")
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
            label_any,
            assignee,
            parent,
            limit,
            all,
            brief,
            flat,
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
            query.extend(label_any.into_iter().map(|label| ("label_any", label)));
            query.extend(id.into_iter().map(|id| ("id", id)));
            if all {
                query.push(("all", "1".into()));
            }
            let rows = output::listing(
                client,
                query,
                limit.unwrap_or(0),
                false,
                options.quiet || options.json,
                brief,
            )?;
            if !flat && !options.json {
                diagnostics::issue_tree(array(&rows));
                return Ok(0);
            }
            rows
        }
        Action::Search(search) => return output::search(client, search, options.json),
        Action::Children { id } => return diagnostics::children(client, id, options.json),
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
        Action::Update(update) => return update.run(client, options.json, options.quiet),
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
        Action::Dep {
            operation: Dependency::Cycles,
        } => {
            return diagnostics::cycles(client, options.json);
        }
        Action::Dep { operation } => dependency(client, operation)?,
        Action::Label { operation } => label(client, operation)?,
        Action::Comments { arguments, file } => {
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
                [id] if id != "add" && file.is_none() => {
                    let ticket = ticket(client, id, &query)?;
                    json!(
                        array(&ticket["comments"])
                            .iter()
                            .map(|comment| output::comment(client, comment))
                            .collect::<Vec<_>>()
                    )
                }
                [sub, id, rest @ ..] if sub == "add" && rest.len() <= 1 => {
                    let text = match comment_text(rest.first(), file.as_deref()) {
                        Ok(text) => text,
                        Err(error) if options.json => {
                            output::json(&json!({"error": error.message, "schema_version": 1}))?;
                            return Ok(1);
                        }
                        Err(error) => return Err(error),
                    };
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
            let mut fields = json!({"status": "deferred"});
            if let Some(until) = until.filter(|date| !date.is_empty()) {
                fields["defer_until"] = json!(defer_until(until)?);
            }
            let row = set_deferral(client, &id, fields, reason.as_deref())?
                .expect("defer changes the ticket");
            rendered = Some(format!("* Deferred {}\n", string(&row["id"])));
            json!([row])
        }
        Action::Undefer { id } => {
            let fields = json!({"status": "open", "defer_until": null});
            let row = set_deferral(client, &id, fields, None)?;
            rendered = Some(row.as_ref().map_or_else(String::new, |row| {
                format!("* Undeferred {} (now open)\n", string(&row["id"]))
            }));
            json!(row.into_iter().collect::<Vec<_>>())
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

fn comment_text(text: Option<&String>, file: Option<&Path>) -> Result<String> {
    let Some(file) = file else {
        return text
            .cloned()
            .ok_or_else(|| usage("comment text is required"));
    };
    if let Some(text) = text {
        return Err(usage(format!(
            "cannot combine positional text {text:?} with --file"
        )));
    }
    let mut text = String::new();
    if file == Path::new("-") {
        io::stdin().read_to_string(&mut text).map_err(usage)?;
    } else {
        text = std::fs::read_to_string(file).map_err(|error| {
            let reason = match error.kind() {
                io::ErrorKind::NotFound => "no such file or directory".into(),
                io::ErrorKind::PermissionDenied => "permission denied".into(),
                _ => error.to_string(),
            };
            usage(format!(
                "failed to open file: open {}: {reason}",
                file.display()
            ))
        })?;
    }
    Ok(text)
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
        Dependency::Cycles => unreachable!("cycles render separately"),
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
