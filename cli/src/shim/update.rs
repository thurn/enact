use super::{defer_until, output, string, ticket};
use crate::client::{Client, Failure, Result, usage};
use chrono::{SecondsFormat, TimeDelta, Utc};
use clap::Args;
use serde_json::{Map, Value, json};
use std::fs;
use std::io::{self, Read};

#[derive(Args)]
pub(super) struct Update {
    id: String,
    #[arg(long, short = 's')]
    status: Option<String>,
    #[arg(long, short = 'a')]
    assignee: Option<String>,
    #[arg(long)]
    claim: bool,
    #[arg(long)]
    force: bool,
    #[arg(long)]
    if_assignee: Option<String>,
    #[arg(long)]
    if_status: Option<String>,
    #[arg(long)]
    title: Option<String>,
    #[arg(long)]
    body_file: Option<String>,
    #[arg(long)]
    allow_empty_description: bool,
    #[arg(long)]
    design_file: Option<String>,
    #[arg(long)]
    acceptance: Option<String>,
    #[arg(long)]
    append_notes: Option<String>,
    #[arg(long)]
    parent: Option<String>,
    #[arg(long, value_delimiter = ',')]
    add_label: Vec<String>,
    #[arg(long)]
    defer: Option<String>,
    #[arg(long)]
    set_metadata: Vec<String>,
    #[arg(long)]
    unset_metadata: Vec<String>,
}

impl Update {
    pub(super) fn run(self, client: &Client, json_output: bool, quiet: bool) -> Result<u8> {
        if self.force && self.if_assignee.is_some() {
            return Err(usage(
                "Error: if any flags in the group [force if-assignee] are set none of the others can be; [force if-assignee] were all set",
            ));
        }
        if self.claim && (self.if_status.is_some() || self.if_assignee.is_some()) {
            return input_error(
                "cannot combine --if-assignee/--if-status with --claim (--claim is already an atomic compare-and-set)",
                json_output,
            );
        }
        let body = read_text(self.body_file.as_deref())?;
        if body.as_deref() == Some("") && !self.allow_empty_description {
            return input_error(
                r#"empty description from stdin/file requires --allow-empty-description (or use an explicit inline empty value like --description "")"#,
                json_output,
            );
        }
        let mut fields = Map::new();
        for (key, field) in [
            ("title", self.title.clone().map(Value::String)),
            ("body", body.map(Value::String)),
            (
                "design",
                read_text(self.design_file.as_deref())?.map(Value::String),
            ),
            ("acceptance", self.acceptance.clone().map(Value::String)),
            ("parent", self.parent.as_deref().map(nullable)),
        ] {
            if let Some(field) = field {
                fields.insert(key.into(), field);
            }
        }
        if let Some(until) = &self.defer {
            let value = if until.is_empty() {
                Value::Null
            } else {
                json!(defer_until(until.clone())?)
            };
            fields.insert("defer_until".into(), value);
        }
        let metadata = self.metadata()?;
        let updates_fields = fields.keys().any(|field| field != "parent")
            || !metadata.is_empty()
            || !self.unset_metadata.is_empty()
            || self.append_notes.is_some()
            || self.status.is_some()
            || self.assignee.is_some()
            || self.claim;
        if !updates_fields
            && (fields.contains_key("parent") || !self.add_label.is_empty())
            && (self.if_assignee.is_some() || self.if_status.is_some())
        {
            return input_error(
                "--if-assignee/--if-status require at least one field update (e.g. -a, -s); label and parent edits are not covered by the guard",
                json_output,
            );
        }
        if !updates_fields && fields.is_empty() && self.add_label.is_empty() {
            if !quiet {
                println!("No updates specified");
            }
            return Ok(0);
        }
        let mut before = ticket(client, &self.id, &[])?;
        // A version condition protects both the ownership checks and values
        // derived from the current ticket, including appended notes.
        let mut retries = 2;
        loop {
            self.check(client, &before, json_output)?;
            let operation = self.operation(client, &before, fields.clone(), metadata.clone());
            let batch = json!({
                "if": [{"id": self.id, "version": before["version"]}],
                "ops": [operation],
            });
            // Retry a confirmed version conflict. If a reply was lost after
            // the write committed, a second append would duplicate the note.
            match client.post(&client.url(&["batch"], &[]), &batch, false) {
                Ok(_) => break,
                Err(error) if retries > 0 && error.status == Some(409) => {
                    before = error
                        .body
                        .and_then(|body| body.get("current").cloned())
                        .filter(Value::is_object)
                        .ok_or_else(|| usage("update conflict has no current ticket"))?;
                    retries -= 1;
                }
                Err(error) => return Err(error),
            }
        }
        if json_output {
            let row = output::row(client, &ticket(client, &self.id, &[])?);
            output::json(&json!([row]))?;
        } else if !quiet {
            let title = self
                .title
                .as_deref()
                .unwrap_or_else(|| string(&before["title"]));
            let id = output::legacy_id(client, string(&before["id"]));
            println!("✓ Updated issue: {id} — {title}");
        }
        Ok(0)
    }

    fn metadata(&self) -> Result<Map<String, Value>> {
        let mut metadata = Map::new();
        for assignment in &self.set_metadata {
            let (key, value) = assignment
                .split_once('=')
                .ok_or_else(|| usage("--set-metadata expects key=value"))?;
            if key.is_empty() || key == "_enact_beads" {
                return Err(usage("invalid metadata key"));
            }
            let value = match serde_json::from_str::<Value>(value) {
                Ok(value) if value.is_boolean() || value.is_number() || value.is_null() => value,
                _ => json!(value),
            };
            metadata.insert(key.to_owned(), value);
        }
        if self
            .unset_metadata
            .iter()
            .any(|key| key.is_empty() || key == "_enact_beads")
        {
            return Err(usage("invalid metadata key"));
        }
        Ok(metadata)
    }

    fn check(&self, client: &Client, before: &Value, json_output: bool) -> Result<()> {
        let id = &self.id;
        let assignee = string(&before["assignee"]);
        let status = string(&before["status"]);
        if let Some(expected) = &self.if_assignee
            && expected != assignee
        {
            return Err(rejected(
                id,
                format!(
                    "assignee mismatch: {id} is held by {}, expected {}",
                    json!(assignee),
                    json!(expected),
                ),
                true,
                json_output,
            ));
        }
        if let Some(expected) = &self.if_status
            && expected != status
        {
            return Err(rejected(
                id,
                format!(
                    "status mismatch: {id} has status {}, expected {}",
                    json!(status),
                    json!(expected),
                ),
                true,
                json_output,
            ));
        }
        if self.claim {
            let problem = if !matches!(status, "open" | "in_progress") {
                format!("issue not claimable: status {status}")
            } else if !assignee.is_empty() && assignee != client.actor {
                format!("issue already claimed by {assignee}")
            } else {
                return Ok(());
            };
            return Err(rejected(id, problem, false, json_output));
        }
        if let Some(next) = &self.assignee
            && !self.force
            && status == "in_progress"
            && !assignee.is_empty()
            && assignee != client.actor
            && assignee != next
        {
            let message = format!(
                "cannot reassign {id}: held by {} (in_progress); coordinate with the holder (bd mail {assignee}) — pass --force only if their claim is abandoned (crashed agent, expired lease), or use bd reclaim",
                json!(assignee),
            );
            return Err(update_failure(id, &message, &message, false, json_output));
        }
        Ok(())
    }

    fn operation(
        &self,
        client: &Client,
        before: &Value,
        mut fields: Map<String, Value>,
        mut metadata: Map<String, Value>,
    ) -> Value {
        let status = if self.claim {
            Some("in_progress")
        } else {
            self.status.as_deref().or(self
                .defer
                .as_deref()
                .filter(|until| !until.is_empty())
                .map(|_| "deferred"))
        };
        let assignee = if self.claim {
            Some(client.actor.as_str())
        } else {
            self.assignee.as_deref()
        };
        if let Some(status) = status {
            fields.insert("status".into(), json!(status));
        }
        if let Some(assignee) = assignee {
            fields.insert("assignee".into(), nullable(assignee));
        }
        if let Some(note) = &self.append_notes {
            let previous = string(&before["notes"]);
            let combined = if previous.is_empty() {
                note.clone()
            } else {
                format!("{previous}\n{note}")
            };
            fields.insert("notes".into(), json!(combined));
        }

        let now = Utc::now();
        let mut legacy = before["metadata"]["_enact_beads"]
            .as_object()
            .cloned()
            .unwrap_or_default();
        let prior = legacy.clone();
        if status == Some("in_progress")
            && before["status"] != "in_progress"
            && !legacy.contains_key("started_at")
        {
            legacy.insert(
                "started_at".into(),
                json!(now.to_rfc3339_opts(SecondsFormat::Secs, true)),
            );
        }
        if self.claim {
            if before["status"] != "in_progress" || before["assignee"] != client.actor {
                legacy.insert(
                    "heartbeat_at".into(),
                    json!(now.to_rfc3339_opts(SecondsFormat::Secs, true)),
                );
                legacy.insert(
                    "lease_expires_at".into(),
                    json!((now + TimeDelta::minutes(5)).to_rfc3339_opts(SecondsFormat::Secs, true)),
                );
            }
        } else if status.is_some_and(|status| status != "in_progress")
            || assignee.is_some_and(|assignee| assignee != string(&before["assignee"]))
        {
            legacy.remove("heartbeat_at");
            legacy.remove("lease_expires_at");
        }
        if legacy != prior {
            metadata.insert("_enact_beads".into(), json!(legacy));
        }
        json!({
            "op": "update", "id": self.id, "set": fields, "add_labels": self.add_label,
            "set_meta": metadata, "unset_meta": self.unset_metadata,
        })
    }
}

fn read_text(path: Option<&str>) -> Result<Option<String>> {
    match path {
        None => Ok(None),
        Some("-") => {
            let mut text = String::new();
            io::stdin().read_to_string(&mut text).map_err(usage)?;
            Ok(Some(text))
        }
        Some(path) => fs::read_to_string(path).map(Some).map_err(usage),
    }
}

fn nullable(value: &str) -> Value {
    if value.is_empty() {
        Value::Null
    } else {
        json!(value)
    }
}

fn rejected(id: &str, problem: String, guard: bool, json_output: bool) -> Failure {
    update_failure(
        id,
        &format!("Error updating {id}: {problem}"),
        &format!("updating issue: {problem}"),
        guard,
        json_output,
    )
}

fn update_failure(
    id: &str,
    message: &str,
    problem: &str,
    guard: bool,
    json_output: bool,
) -> Failure {
    let summary = "1 of 1 issues failed to update";
    let detail = if json_output {
        let guard_field = if guard {
            ",\"guard_mismatch\":true"
        } else {
            ""
        };
        format!(
            "{{\"error\":{},\"failed\":[{{\"id\":{},\"error\":{}{guard_field}}}],\"schema_version\":1}}",
            json!(summary),
            json!(id),
            json!(problem),
        )
    } else {
        format!("Error: {summary}\n  {id}: {problem}")
    };
    Failure::new(if guard { 13 } else { 1 }, format!("{message}\n{detail}"))
}

fn input_error(problem: &str, json_output: bool) -> Result<u8> {
    if json_output {
        output::json(&json!({"error": problem, "schema_version": 1}))?;
        Ok(1)
    } else {
        Err(usage(format!("Error: {problem}")))
    }
}
