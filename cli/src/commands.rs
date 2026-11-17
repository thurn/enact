use crate::args::{Command, Create, Cutover, EdgeOperation, LeaseOperation, List, Ready, Update};
use crate::client::{Client, Result, usage};
use serde_json::{Map, Value, json};
use std::io::{self, Write};

type Fields = Map<String, Value>;
type Query = Vec<(&'static str, String)>;

pub fn run(client: &Client, command: Command) -> Result<Value> {
    match command {
        Command::Show {
            id,
            comments,
            history,
        } => {
            let mut query = Query::new();
            flag(&mut query, "comments", comments);
            flag(&mut query, "history", history);
            read(client, &["tickets", &id], &query)
        }
        Command::List(List {
            ready_filters,
            status,
            ticket_type,
            parent,
            assignee,
            all,
            ready,
            limit,
        }) => {
            let mut query = ready_query(ready_filters);
            for (name, value) in [
                ("status", status),
                ("type", ticket_type),
                ("parent", parent),
                ("assignee", assignee),
                ("limit", limit.map(|n| n.to_string())),
            ] {
                query.extend(value.map(|value| (name, value)));
            }
            flag(&mut query, "all", all);
            flag(&mut query, "ready", ready);
            read(client, &["tickets"], &query)
        }
        Command::Ready(filters) => {
            let mut query = ready_query(filters);
            flag(&mut query, "ready", true);
            read(client, &["tickets"], &query)
        }
        Command::Graph { if_none_match, all } => {
            let mut query = Query::new();
            flag(&mut query, "all", all);
            let reply = client.get(
                &client.url(&["graph"], &query),
                if_none_match.as_deref(),
                client.retry_for,
            )?;
            let etag = reply.etag.clone();
            let mut graph = reply.json()?;
            if let Some(etag) = etag.filter(|_| graph["unchanged"] != true) {
                graph["etag"] = json!(etag);
            }
            Ok(graph)
        }
        Command::Create(args) => create(client, args),
        Command::Update(args) => update(client, args),
        Command::Claim { id } => single(client, json!({"op": "claim", "id": id}), vec![]),
        Command::Close { id, reason } => single(
            client,
            json!({"op": "close", "id": id, "reason": reason}),
            vec![],
        ),
        Command::Reopen { id } => single(client, json!({"op": "reopen", "id": id}), vec![]),
        Command::Defer { id, until } => single(
            client,
            json!({"op": "defer", "id": id, "until": until}),
            vec![],
        ),
        Command::Comment {
            id,
            text,
            body_file,
        } => {
            let text = file_contents(body_file)?
                .or(text)
                .expect("comment text validated by parser");
            single(
                client,
                json!({"op": "comment", "id": id, "text": text}),
                vec![],
            )
        }
        Command::Dep {
            operation,
            id,
            on,
            kind,
        } => {
            let operation = match operation {
                EdgeOperation::Add => "dep_add",
                EdgeOperation::Remove => "dep_remove",
            };
            let mut op = json!({"op": operation, "id": id, "on": on});
            if let Some(kind) = kind {
                op["kind"] = json!(kind);
            }
            single(client, op, vec![])
        }
        Command::Batch => batch(
            client,
            serde_json::from_reader(io::stdin().lock()).map_err(usage)?,
        ),
        Command::Lease {
            operation,
            name,
            ttl,
            holder,
            payload,
            if_revision,
            no_queue,
        } => {
            let holder = holder.as_deref().unwrap_or(&client.actor);
            let (operation, duration) = match operation {
                LeaseOperation::Acquire => ("acquire", Some(ttl)),
                LeaseOperation::Renew => ("renew", Some(ttl)),
                LeaseOperation::Release => ("release", None),
            };
            let mut data = json!({"op": operation, "holder": holder});
            if let Some(ttl) = duration {
                data["ttl_s"] = json!(ttl);
            }
            if let Some(payload) = payload {
                data["payload"] = serde_json::from_str(&payload).map_err(usage)?;
            }
            if let Some(revision) = if_revision {
                data["if_revision"] = json!(revision);
            }
            if no_queue {
                data["no_queue"] = json!(true);
            }
            write(client, &["leases", &name], &data)
        }
        Command::Leases => read(client, &["leases"], &[]),
        Command::Event { kind, data } => {
            let data: Value = serde_json::from_str(&data).map_err(usage)?;
            write(client, &["events"], &json!({"kind": kind, "data": data}))
        }
        Command::Events { kind, since, limit } => {
            let mut query = Query::new();
            query.extend(kind.map(|value| ("kind", value)));
            query.extend(since.map(|value| ("since", value.to_string())));
            query.extend(limit.map(|value| ("limit", value.to_string())));
            read(client, &["events"], &query)
        }
        Command::Wait(args) => crate::feed::wait(client, args),
        Command::State {
            set_cutover: Some(cutover),
        } => {
            let cutover = match cutover {
                Cutover::Before => "before",
                Cutover::Paused => "paused",
                Cutover::After => "after",
            };
            write(client, &["state"], &json!({"cutover": cutover}))
        }
        Command::State { set_cutover: None } => read(client, &["state"], &[]),
        Command::Import {
            file,
            frozen_beads,
            history_dir,
            row_budget,
        } => crate::migration::import(client, file, frozen_beads, history_dir, row_budget),
        Command::Restore { file, row_budget } => {
            crate::migration::restore(client, &file, row_budget)
        }
        Command::Compare {
            frozen_beads,
            history_dir,
        } => crate::migration::compare(client, frozen_beads, history_dir),
        Command::Bench { .. } | Command::Export | Command::Register { .. } | Command::Version => {
            unreachable!("handled by main")
        }
    }
}

fn flag(query: &mut Query, name: &'static str, enabled: bool) {
    if enabled {
        query.push((name, "1".into()));
    }
}

pub fn ready_query(filters: Ready) -> Query {
    let mut query = Query::new();
    query.extend(filters.ancestor.map(|ancestor| ("ancestor", ancestor)));
    query.extend(filters.label.into_iter().map(|label| ("label", label)));
    query
}

pub(crate) fn read(client: &Client, path: &[&str], query: &[(&str, String)]) -> Result<Value> {
    client
        .get(&client.url(path, query), None, client.retry_for)?
        .json()
}

fn write(client: &Client, path: &[&str], body: &Value) -> Result<Value> {
    client.post(&client.url(path, &[]), body, false)?.json()
}

pub fn export(client: &Client) -> Result<()> {
    let mut output = io::stdout().lock();
    export_pages(client, |page| {
        output.write_all(page.as_bytes()).map_err(usage)?;
        if !page.ends_with('\n') {
            output.write_all(b"\n").map_err(usage)?;
        }
        Ok(())
    })
}

pub fn export_pages(client: &Client, mut visit: impl FnMut(&str) -> Result<()>) -> Result<()> {
    let mut cursor = String::new();
    loop {
        let query = if cursor.is_empty() {
            vec![]
        } else {
            vec![("after", cursor.clone())]
        };
        let page = client
            .get(&client.url(&["export"], &query), None, client.retry_for)?
            .body;
        let Some(last) = page.lines().rev().find(|line| !line.trim().is_empty()) else {
            return Ok(());
        };
        let ticket: Value = serde_json::from_str(last).map_err(usage)?;
        let id = ticket["id"]
            .as_str()
            .or_else(|| ticket["ticket"]["id"].as_str())
            .ok_or_else(|| usage("an export row has no ticket ID"))?;
        if id <= cursor.as_str() {
            return Err(usage("export cursor did not advance"));
        }
        cursor = id.to_owned();
        visit(&page)?;
    }
}

fn create(client: &Client, args: Create) -> Result<Value> {
    let mut fields = Fields::from_iter([("title".into(), json!(args.title))]);
    put(&mut fields, "type", args.ticket_type);
    put(&mut fields, "priority", args.priority);
    put(&mut fields, "body", file_contents(args.body_file)?);
    put(&mut fields, "acceptance", args.acceptance);
    put(&mut fields, "design", file_contents(args.design_file)?);
    if !args.labels.is_empty() {
        fields.insert("labels".into(), json!(args.labels));
    }
    if args.last {
        fields.insert("last".into(), json!(1));
    }
    let mut op = json!({"op": "create", "fields": fields, "deps": args.dep});
    if let Some(parent) = args.parent {
        op["parent"] = json!(parent);
    }
    single(client, op, vec![])
}

fn update(client: &Client, args: Update) -> Result<Value> {
    let mut fields = Fields::new();
    put(&mut fields, "title", args.title);
    put(&mut fields, "status", args.status);
    put(&mut fields, "priority", args.priority);
    put(&mut fields, "assignee", args.assignee.map(nullable));
    put(&mut fields, "parent", args.parent.map(nullable));
    put(&mut fields, "body", file_contents(args.body_file)?);
    put(&mut fields, "acceptance", args.acceptance);
    put(&mut fields, "notes", args.notes);
    if args.last || args.no_last {
        fields.insert("last".into(), json!(u8::from(args.last)));
    }
    let mut metadata = Fields::new();
    for setting in args.set {
        let (key, value) = setting
            .split_once('=')
            .filter(|(key, _)| !key.is_empty())
            .ok_or_else(|| usage("--set expects key=value"))?;
        metadata.insert(key.into(), json!(value));
    }
    let mut conditions = Vec::new();
    if let Some(version) = args.if_version {
        conditions.push(json!({"id": args.id, "version": version}));
    }
    if let Some(assignee) = args.if_assignee {
        conditions.push(json!({"id": args.id, "assignee": nullable(assignee)}));
    }
    let mut operation = json!({"op": "update", "id": args.id, "set": fields});
    for (field, values) in [
        ("add_labels", args.add_label),
        ("remove_labels", args.remove_label),
        ("unset_meta", args.unset),
    ] {
        if !values.is_empty() {
            operation[field] = json!(values);
        }
    }
    if !metadata.is_empty() {
        operation["set_meta"] = json!(metadata);
    }
    single(client, operation, conditions)
}

fn nullable(value: String) -> Value {
    if value.is_empty() {
        Value::Null
    } else {
        json!(value)
    }
}

fn put(fields: &mut Fields, name: &str, value: Option<impl Into<Value>>) {
    if let Some(value) = value {
        fields.insert(name.into(), value.into());
    }
}

fn file_contents(path: Option<std::path::PathBuf>) -> Result<Option<String>> {
    path.map(|path| {
        std::fs::read_to_string(&path)
            .map_err(|error| usage(format!("{}: {error}", path.display())))
    })
    .transpose()
}

pub(crate) fn single(client: &Client, op: Value, conditions: Vec<Value>) -> Result<Value> {
    batch(client, json!({"if": conditions, "ops": [op]}))
}

pub(crate) fn batch(client: &Client, mut body: Value) -> Result<Value> {
    let operations = body["ops"]
        .as_array()
        .ok_or_else(|| usage("batch requires an ops array"))?;
    // A condition must change with the write to stop a replay. An assignee
    // check alone can still pass after a comment or another unrelated update.
    let guarded = body["if"].as_array().is_some_and(|conditions| {
        conditions.iter().any(|condition| {
            !operations.is_empty() && condition["seq"].is_u64()
                || condition["version"].is_u64()
                    && condition["id"].is_string()
                    && operations.len() == 1
                    && operations.first().is_some_and(|op| {
                        op["id"] == condition["id"]
                            && matches!(
                                op["op"].as_str(),
                                Some("update" | "claim" | "close" | "reopen" | "defer")
                            )
                    })
        })
    });
    let operations = body["ops"]
        .as_array_mut()
        .ok_or_else(|| usage("batch requires an ops array"))?;
    let mut all_creates = !operations.is_empty();
    for operation in operations {
        if operation["op"] == "create" {
            if operation.get("request_key").is_none() {
                let mut key = [0_u8; 16];
                getrandom::fill(&mut key).map_err(usage)?;
                operation["request_key"] = json!(
                    key.iter()
                        .map(|byte| format!("{byte:02x}"))
                        .collect::<String>()
                );
            }
        } else {
            all_creates = false;
        }
    }
    client
        .post(&client.url(&["batch"], &[]), &body, guarded || all_creates)?
        .json()
}
