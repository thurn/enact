use super::{array, string, ticket};
use crate::client::{Client, Result, usage};
use crate::commands;
use serde_json::{Value, json};
use std::io::{self, Write};

pub fn legacy_id(client: &Client, id: &str) -> String {
    let prefix = format!("{}-", client.project);
    if id.starts_with(&prefix) {
        id.to_owned()
    } else {
        format!("{prefix}{id}")
    }
}

pub fn row(client: &Client, ticket: &Value) -> Value {
    let mut result = ticket["metadata"]["_enact_beads"]
        .as_object()
        .cloned()
        .unwrap_or_default();
    result.remove("dependencies");
    for (source, target) in [
        ("title", "title"),
        ("body", "description"),
        ("acceptance", "acceptance_criteria"),
        ("design", "design"),
        ("notes", "notes"),
        ("status", "status"),
        ("priority", "priority"),
        ("type", "issue_type"),
        ("assignee", "assignee"),
        ("created_at", "created_at"),
        ("created_by", "created_by"),
        ("updated_at", "updated_at"),
        ("closed_at", "closed_at"),
        ("close_reason", "close_reason"),
        ("defer_until", "defer_until"),
    ] {
        let field = &ticket[source];
        if !field.is_null() && field.as_str() != Some("") {
            result.insert(target.into(), field.clone());
        }
    }
    result.insert("id".into(), json!(legacy_id(client, string(&ticket["id"]))));
    let mut labels: Vec<_> = array(&ticket["labels"])
        .iter()
        .filter_map(Value::as_str)
        .collect();
    labels.sort_unstable();
    if !labels.is_empty() {
        result.insert("labels".into(), json!(labels));
    }
    if let Some(mut metadata) = ticket["metadata"].as_object().cloned() {
        metadata.remove("_enact_beads");
        if !metadata.is_empty() {
            result.insert("metadata".into(), Value::Object(metadata));
        }
    }
    Value::Object(result)
}

pub fn listing(
    client: &Client,
    query: Vec<(&str, String)>,
    limit: usize,
    ready: bool,
    quiet: bool,
) -> Result<Value> {
    let reply = commands::read(client, &["tickets"], &query)?;
    let mut rows: Vec<_> = array(&reply)
        .iter()
        .map(|ticket| {
            let mut row = row(client, ticket);
            let dependencies = listed_dependencies(client, ticket);
            if !dependencies.is_empty() {
                row["dependencies"] = json!(dependencies);
            }
            if let Some(parent) = ticket["parent"].as_str() {
                row["parent"] = json!(legacy_id(client, parent));
            }
            for field in ["dependency_count", "dependent_count", "comment_count"] {
                row[field] = ticket[field].clone();
            }
            row
        })
        .collect();
    rows.sort_by(|a, b| {
        a["priority"]
            .as_u64()
            .cmp(&b["priority"].as_u64())
            .then_with(|| {
                let (a, b) = (string(&a["created_at"]), string(&b["created_at"]));
                if ready { a.cmp(b) } else { b.cmp(a) }
            })
            .then_with(|| string(&a["id"]).cmp(string(&b["id"])))
    });
    if limit != 0 && rows.len() > limit {
        if !quiet {
            let kind = if ready { "ready issues" } else { "issues" };
            eprintln!(
                "Showing {limit} of {} {kind}. Use --limit 0 for all, or --limit N to raise the cap.",
                rows.len()
            );
        }
        rows.truncate(limit);
    }
    Ok(json!(rows))
}

fn listed_dependencies(client: &Client, ticket: &Value) -> Vec<Value> {
    let source = legacy_id(client, string(&ticket["id"]));
    let mut edges: Vec<_> = array(&ticket["edges"])
        .iter()
        .map(|edge| (string(&edge["dst"]), string(&edge["kind"])))
        .collect();
    if let Some(parent) = ticket["parent"].as_str() {
        edges.push((parent, "parent-child"));
    }
    let imported = array(&ticket["metadata"]["_enact_beads"]["dependencies"]);
    edges.into_iter().map(|(target, kind)| {
        let target = legacy_id(client, target);
        let saved = imported.iter().find(|edge| edge["depends_on_id"] == target
            && (edge["type"] == kind || kind == "related" && edge["type"] == "relates-to"));
        saved.cloned().unwrap_or_else(|| json!({
            "issue_id": source, "depends_on_id": target,
            "type": if kind == "related" { "relates-to" } else { kind },
            "created_at": ticket["created_at"], "created_by": ticket["created_by"], "metadata": "{}",
        }))
    }).collect()
}

pub fn comment(client: &Client, comment: &Value) -> Value {
    let ticket = legacy_id(client, string(&comment["ticket"]));
    let id = comment["legacy_id"]
        .as_str()
        .map_or_else(|| format!("{ticket}:{}", comment["n"]), str::to_owned);
    json!({"id": id, "issue_id": ticket, "author": comment["author"], "text": comment["text"], "created_at": comment["at"]})
}

pub fn related(client: &Client, source: &Value, up: bool) -> Result<Vec<Value>> {
    let id = &source["id"];
    let mut relations: Vec<_> = array(&source["edges"])
        .iter()
        .filter_map(|edge| {
            let (from, to) = if up { ("dst", "src") } else { ("src", "dst") };
            (edge[from] == *id).then(|| (string(&edge[to]), string(&edge["kind"])))
        })
        .collect();
    if up {
        relations.extend(
            array(&source["children"])
                .iter()
                .map(|child| (string(child), "parent-child")),
        );
    } else if let Some(parent) = source["parent"].as_str() {
        relations.push((parent, "parent-child"));
    }
    relations.sort_unstable();
    relations
        .into_iter()
        .map(|(id, kind)| {
            let mut output = row(client, &ticket(client, id, &[])?);
            output["dependency_type"] = json!(if kind == "related" {
                "relates-to"
            } else {
                kind
            });
            Ok(output)
        })
        .collect()
}

pub fn show(client: &Client, ids: &[String], as_json: bool) -> Result<u8> {
    if ids.is_empty() {
        return Err(usage("bd: show requires an ID; use tk show"));
    }
    let mut rows = Vec::new();
    for id in ids {
        let raw = match ticket(
            client,
            id,
            &[("beads", "1".into()), ("comments", "1".into())],
        ) {
            Err(error) if error.status == Some(404) => {
                eprintln!(
                    "Issue {id} not found\nHint: this ID may have never existed, or may reference a deleted/purged record with no trace left in the live database — try 'bd history {id}'"
                );
                continue;
            }
            response => response?,
        };
        let mut row = row(client, &raw);
        let dependencies = related(client, &raw, false)?;
        let dependents = related(client, &raw, true)?;
        for (field, values) in [("dependencies", &dependencies), ("dependents", &dependents)] {
            if !values.is_empty() {
                row[field] = json!(values);
            }
        }
        if let Some(parent) = raw["parent"].as_str() {
            row["parent"] = json!(legacy_id(client, parent));
        }
        let comments: Vec<_> = array(&raw["comments"])
            .iter()
            .map(|row| comment(client, row))
            .collect();
        if !comments.is_empty() {
            row["comments"] = json!(comments);
        }
        row["dependency_count"] = json!(dependencies.len());
        row["dependent_count"] = json!(dependents.len());
        row["comment_count"] = raw["comment_count"].clone();
        // Beads exposes an opaque, string-valued revision for its own CAS API.
        row["revision"] = json!(raw["version"].to_string());
        rows.push(row);
    }
    if rows.is_empty() {
        if as_json {
            json(
                &json!({"error": "no issues found matching the provided IDs", "schema_version": 1,
                "hint": "some IDs may reference deleted/purged records with no trace left in the live database — try 'bd history <id>' to check"}),
            )?;
        }
        return Ok(1);
    }
    if as_json {
        json(&json!(rows))?;
    } else {
        human(&json!(rows))?;
    }
    Ok(0)
}

pub fn json(value: &Value) -> Result<()> {
    let mut output = io::stdout().lock();
    serde_json::to_writer_pretty(&mut output, value).map_err(usage)?;
    writeln!(output).map_err(usage)
}

pub fn human(value: &Value) -> Result<()> {
    let mut output = io::stdout().lock();
    for row in value
        .as_array()
        .map_or(std::slice::from_ref(value), Vec::as_slice)
    {
        if let Some(text) = row.as_str() {
            writeln!(output, "{text}").map_err(usage)?;
        } else if let (Some(id), Some(title)) = (row["id"].as_str(), row["title"].as_str()) {
            writeln!(
                output,
                "{id} [P{}] [{}] - {title}",
                row["priority"],
                string(&row["issue_type"])
            )
            .map_err(usage)?;
        } else {
            writeln!(output, "{row}").map_err(usage)?;
        }
    }
    Ok(())
}
