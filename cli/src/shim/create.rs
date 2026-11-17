use super::{defer_until, git_config, output, string, ticket};
use crate::client::{Client, Result, usage};
use crate::commands;
use clap::Args;
use serde_json::{Map, Value, json};
use std::fs;
use std::io::{self, Read};

#[derive(Args)]
pub(super) struct Create {
    #[arg(long)]
    id: Option<String>,
    #[arg(long)]
    parent: Option<String>,
    #[arg(long, required_unless_present = "positional_title")]
    title: Option<String>,
    positional_title: Option<String>,
    #[arg(long = "type", short = 't', default_value = "task")]
    kind: String,
    #[arg(short = 'p', long, default_value_t = 2)]
    priority: u8,
    #[arg(long, short = 'd')]
    description: Option<String>,
    #[arg(long)]
    body_file: Option<String>,
    #[arg(long, short = 'l', value_delimiter = ',')]
    labels: Vec<String>,
    #[arg(long)]
    acceptance: Option<String>,
    #[arg(long, value_delimiter = ',')]
    deps: Vec<String>,
    #[arg(long)]
    defer: Option<String>,
    #[arg(long)]
    metadata: Option<String>,
    #[arg(long, short = 's')]
    status: Option<String>,
    #[arg(long)]
    silent: bool,
}

impl Create {
    pub(super) fn run(self, client: &Client, json_output: bool) -> Result<u8> {
        if self.id.is_some() && self.parent.is_some() {
            return Err(usage("Error: cannot specify both --id and --parent flags"));
        }
        let title = self
            .title
            .or(self.positional_title)
            .expect("title validated by parser");
        let body = match self.body_file.as_deref() {
            Some("-") => {
                let mut text = String::new();
                io::stdin().read_to_string(&mut text).map_err(usage)?;
                text
            }
            Some(path) => fs::read_to_string(path).map_err(usage)?,
            None => self.description.unwrap_or_default(),
        };
        let mut metadata = match self.metadata {
            Some(value) => {
                let json = match value.strip_prefix('@') {
                    Some(path) => fs::read_to_string(path).map_err(usage)?,
                    None => value,
                };
                serde_json::from_str::<Map<String, Value>>(&json).map_err(usage)?
            }
            None => Map::new(),
        };
        if metadata.contains_key("_enact_beads") {
            return Err(usage("invalid metadata key _enact_beads"));
        }
        if let Some(owner) = git_config("user.email") {
            metadata.insert("_enact_beads".into(), json!({"owner": owner}));
        }
        let status = self.status.as_deref().unwrap_or(if self.defer.is_some() {
            "deferred"
        } else {
            "open"
        });
        let deps: Vec<_> = self.deps.iter().map(|spec| dependency(spec)).collect();
        let mut operation = json!({
            "op": "create", "parent": self.parent, "deps": deps,
            "fields": {
                "title": title, "type": self.kind, "priority": self.priority, "body": body,
                "labels": self.labels, "acceptance": self.acceptance.unwrap_or_default(),
                "metadata": metadata, "status": status, "defer_until": self.defer.map(defer_until).transpose()?,
            },
        });
        if let Some(id) = self.id {
            operation["legacy_id"] = json!(id);
        }
        let response = commands::single(client, operation, vec![])?;
        let id = string(&response["results"][0]["id"]);
        if json_output {
            let mut row = output::row(client, &ticket(client, id, &[])?);
            row["schema_version"] = json!(1);
            output::json(&row)?;
        } else {
            let id = output::legacy_id(client, id);
            if self.silent {
                println!("{id}");
            } else {
                println!("✓ Created {id}: {title}");
            }
        }
        Ok(0)
    }
}

fn dependency(spec: &str) -> Value {
    let (kind, id) = spec.split_once(':').unwrap_or(("blocked-by", spec));
    // In `bd create --deps`, `blocks` describes what the new ticket blocks.
    // In `bd dep add` and the server API, the source is the blocked ticket.
    match kind {
        "blocks" => json!({"on": id, "kind": "blocks", "reverse": true}),
        "blocked-by" | "depends-on" => json!({"on": id, "kind": "blocks"}),
        "relates-to" => json!({"on": id, "kind": "related"}),
        _ => json!({"on": id, "kind": kind}),
    }
}
