use crate::client::{Result, usage};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::{BufRead, Cursor};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

pub type Tickets = BTreeMap<String, Value>;

pub fn legacy(arguments: &[&str], low_priority: bool) -> Result<Vec<u8>> {
    legacy_in(arguments, low_priority, None)
}

fn legacy_in(arguments: &[&str], low_priority: bool, root: Option<&Path>) -> Result<Vec<u8>> {
    let binary = crate::shim::state::real_binary()?;
    let mut command = if low_priority && cfg!(unix) {
        let mut nice = Command::new("nice");
        nice.args(["-n", "10"]).arg(&binary);
        nice
    } else {
        Command::new(&binary)
    };
    #[cfg(windows)]
    if low_priority {
        use std::os::windows::process::CommandExt;
        const BELOW_NORMAL_PRIORITY_CLASS: u32 = 0x4000;
        command.creation_flags(BELOW_NORMAL_PRIORITY_CLASS);
    }
    if let Some(root) = root {
        // Read the copy even if this process normally targets another store.
        for (key, _) in std::env::vars_os() {
            if key
                .to_string_lossy()
                .to_ascii_uppercase()
                .starts_with("BEADS_")
            {
                command.env_remove(key);
            }
        }
        command
            .current_dir(root)
            .env("BEADS_DIR", root.join(".beads"));
    }
    let output = command
        .args(["--readonly", "--sandbox"])
        .args(arguments)
        .stdin(Stdio::null())
        .output()
        .map_err(|error| usage(format!("bd: {error}")))?;
    if !output.status.success() {
        return Err(usage(format!(
            "bd {} failed: {}",
            arguments.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        )));
    }
    Ok(output.stdout)
}

pub fn lines(input: impl BufRead) -> Result<Tickets> {
    let mut tickets = BTreeMap::new();
    for (index, line) in input.lines().enumerate() {
        let line = line.map_err(usage)?;
        if line.trim().is_empty() {
            continue;
        }
        let ticket: Value = serde_json::from_str(&line)
            .map_err(|error| usage(format!("line {}: {error}", index + 1)))?;
        let id = field(&ticket, "id")?.to_owned();
        if tickets.insert(id.clone(), ticket).is_some() {
            return Err(usage(format!("duplicate ticket {id}")));
        }
    }
    Ok(tickets)
}

pub fn field<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key]
        .as_str()
        .ok_or_else(|| usage(format!("missing or invalid {key}")))
}

pub fn array<'a>(value: &'a Value, key: &str) -> Result<&'a [Value]> {
    match &value[key] {
        Value::Null => Ok(&[]),
        Value::Array(items) => Ok(items),
        _ => Err(usage(format!("invalid {key}, expected a JSON array"))),
    }
}

pub fn canonical_id(id: &str, prefix: &str) -> Result<String> {
    let bare = id
        .strip_prefix(prefix)
        .ok_or_else(|| usage(format!("unexpected Beads ID {id}")))?;
    if !bare.is_empty()
        && bare.as_bytes()[0].is_ascii_alphanumeric()
        && bare
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"-.".contains(&byte))
    {
        Ok(bare.to_owned())
    } else {
        Err(usage(format!("invalid Beads ID {id}")))
    }
}

pub fn prefix(source: &Tickets) -> Result<String> {
    match source.first_key_value() {
        None => Ok(String::new()),
        Some((id, _)) => id
            .split_once('-')
            .filter(|(prefix, _)| !prefix.is_empty())
            .map(|(prefix, _)| format!("{prefix}-"))
            .ok_or_else(|| usage(format!("Beads ID has no project prefix: {id}"))),
    }
}

pub fn convert(source: &Tickets, prefix: &str) -> Result<Tickets> {
    let mut next_children: BTreeMap<&str, u64> = BTreeMap::new();
    for id in source.keys() {
        if let Some((parent, suffix)) = id.rsplit_once('.') {
            let next = suffix
                .parse::<u64>()
                .ok()
                .and_then(|value| value.checked_add(1))
                .ok_or_else(|| usage(format!("invalid child ID {id}")))?;
            let counter = next_children.entry(parent).or_default();
            *counter = (*counter).max(next);
        }
    }
    source
        .iter()
        .map(|(old_id, row)| {
            let id = canonical_id(old_id, prefix)?;
            let ticket = convert_ticket(
                row,
                &id,
                old_id,
                prefix,
                *next_children.get(old_id.as_str()).unwrap_or(&1),
            )
            .map_err(|error| usage(format!("{old_id}: {}", error.message)))?;
            Ok((id, ticket))
        })
        .collect()
}

fn convert_ticket(
    row: &Value,
    id: &str,
    old_id: &str,
    prefix: &str,
    next_child: u64,
) -> Result<Value> {
    let mut parent = None;
    let mut edges = BTreeMap::new();
    for edge in array(row, "dependencies")? {
        let target = canonical_id(field(edge, "depends_on_id")?, prefix)?;
        let kind = match field(edge, "type")? {
            "parent-child" => {
                if parent.replace(target).is_some() {
                    return Err(usage("multiple parents"));
                }
                continue;
            }
            "relates-to" | "related" => "related",
            kind @ ("blocks" | "conditional-blocks" | "waits-for" | "discovered-from") => kind,
            kind => return Err(usage(format!("unsupported dependency type {kind}"))),
        };
        if edges
            .insert(
                target.clone(),
                json!({"src": id, "dst": target, "kind": kind}),
            )
            .is_some()
        {
            return Err(usage(format!("multiple edges to {target}")));
        }
    }
    let mut comments = BTreeMap::new();
    let mut comment_ids = BTreeSet::new();
    for comment in array(row, "comments")? {
        let legacy_id = match &comment["id"] {
            Value::String(id) if !id.is_empty() => id.clone(),
            Value::Number(id) => id.to_string(),
            _ => return Err(usage("invalid comment ID")),
        };
        if !comment_ids.insert(legacy_id.clone()) {
            return Err(usage(format!("duplicate comment ID {legacy_id}")));
        }
        let at = field(comment, "created_at")?;
        comments.insert(
            (at, legacy_id.clone()),
            json!({
                "ticket": id, "legacy_id": legacy_id, "author": field(comment, "author")?,
                "at": at, "text": field(comment, "text")?,
            }),
        );
    }
    let comments = comments
        .into_values()
        .enumerate()
        .map(|(index, mut comment)| {
            comment["n"] = json!(index + 1);
            comment
        })
        .collect::<Vec<_>>();
    let labels = array(row, "labels")?
        .iter()
        .map(|label| label.as_str().ok_or_else(|| usage("invalid label")))
        .collect::<Result<BTreeSet<_>>>()?;
    let text = |key: &str| match &row[key] {
        Value::Null => Ok(""),
        Value::String(value) => Ok(value.as_str()),
        _ => Err(usage(format!("invalid {key}, expected text"))),
    };
    let mut metadata = match &row["metadata"] {
        Value::Null => serde_json::Map::new(),
        Value::Object(value) => value.clone(),
        _ => return Err(usage("invalid metadata")),
    };
    // Keep source-only fields for the shim and historical reports, including
    // started_at, which Beryl's efficiency dashboard reads.
    let ticket_fields = [
        "_type",
        "id",
        "title",
        "description",
        "design",
        "acceptance_criteria",
        "notes",
        "issue_type",
        "status",
        "priority",
        "assignee",
        "labels",
        "metadata",
        "created_by",
        "created_at",
        "updated_at",
        "defer_until",
        "closed_at",
        "close_reason",
        "dependencies",
        "comments",
        "dependency_count",
        "dependent_count",
        "comment_count",
    ];
    let mut legacy: serde_json::Map<String, Value> = row
        .as_object()
        .expect("legacy ticket object")
        .iter()
        .filter(|(key, _)| !ticket_fields.contains(&key.as_str()))
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect();
    if let Some(dependencies) = row.get("dependencies") {
        legacy.insert("dependencies".into(), dependencies.clone());
    }
    if !legacy.is_empty()
        && metadata
            .insert("_enact_beads".into(), Value::Object(legacy))
            .is_some()
    {
        return Err(usage(
            "legacy metadata already has the reserved _enact_beads key",
        ));
    }
    Ok(json!({
        "id": id, "aliases": [old_id], "parent": parent, "next_child": next_child,
        "title": field(row, "title")?, "body": text("description")?, "design": text("design")?,
        "acceptance": text("acceptance_criteria")?, "notes": text("notes")?,
        "type": field(row, "issue_type")?, "status": field(row, "status")?,
        "priority": row["priority"], "assignee": row["assignee"], "last": 0,
        "labels": labels, "metadata": metadata, "created_by": text("created_by")?,
        "created_at": field(row, "created_at")?, "updated_at": field(row, "updated_at")?,
        "defer_until": row["defer_until"], "closed_at": row["closed_at"], "close_reason": row["close_reason"],
        "edges": edges.into_values().collect::<Vec<_>>(), "comments": comments,
        "history": [],
    }))
}

pub struct FrozenBeads {
    root: PathBuf,
    database: PathBuf,
    prefix: String,
}

impl FrozenBeads {
    pub fn open(root: &Path, project: &str) -> Result<Self> {
        let absolute = |path: &Path| {
            fs::canonicalize(path).map_err(|error| usage(format!("{}: {error}", path.display())))
        };
        let root = absolute(root)?;
        let beads = root.join(".beads");
        let database = beads.join("embeddeddolt").join(project);
        let dolt = database.join(".dolt");
        if absolute(&dolt)? != dolt || !dolt.is_dir() {
            return Err(usage(format!(
                "{} must be a local embedded Dolt copy",
                dolt.display()
            )));
        }
        let metadata = beads.join("metadata.json");
        let bytes = fs::read(&metadata)
            .map_err(|error| usage(format!("{}: {error}", metadata.display())))?;
        let config: Value = serde_json::from_slice(&bytes).map_err(usage)?;
        if config["dolt_mode"] != "embedded" || config["dolt_database"] != project {
            return Err(usage(format!(
                "{} must describe the embedded Beads database for project {project}",
                metadata.display()
            )));
        }
        Ok(Self {
            root,
            database,
            prefix: format!("{project}-"),
        })
    }

    pub fn read(&self, arguments: &[&str]) -> Result<Vec<u8>> {
        legacy_in(arguments, false, Some(&self.root))
    }

    pub fn export(&self) -> Result<Tickets> {
        lines(Cursor::new(self.read(&["export"])?))
    }

    pub fn snapshots(&self, source: &Tickets) -> Result<Tickets> {
        let mut converted = convert(source, &self.prefix)?;
        // The events table is independent of the issue export. In particular,
        // adding and removing a label can leave the export byte-for-byte equal.
        let output = Command::new("dolt")
            .current_dir(&self.database)
            .args([
                "sql",
                "--disable-auto-gc",
                // `json`, not `jsonl`: dolt before 2.3 accepts only tabular, csv and json.
                "-r",
                "json",
                "-q",
                "SELECT id, issue_id, event_type, actor, old_value, new_value, comment, \
                 DATE_FORMAT(created_at, '%Y-%m-%dT%H:%i:%sZ') AS created_at FROM events",
            ])
            .stdin(Stdio::null())
            .output()
            .map_err(|error| usage(format!("dolt sql: {error}")))?;
        if !output.status.success() {
            return Err(usage(format!(
                "dolt sql in {} failed: {}",
                self.database.display(),
                String::from_utf8_lossy(&output.stderr).trim()
            )));
        }
        let mut histories: BTreeMap<String, Vec<Value>> = BTreeMap::new();
        for event in lines(Cursor::new(json_rows(&output.stdout)?))?.into_values() {
            let id = field(&event, "issue_id")?;
            // Beads' default export excludes infrastructure and memories.
            if source.contains_key(id) {
                histories.entry(id.to_owned()).or_default().push(event);
            }
        }
        for (id, events) in histories {
            let id = canonical_id(&id, &self.prefix)?;
            attach_events(events, converted.get_mut(&id).expect("converted source"));
        }
        Ok(converted)
    }
}

/// The rows of `dolt sql -r json` output, `{"rows": [...]}`, one JSON object per line. Dolt
/// prints nothing at all for a query with no rows.
fn json_rows(output: &[u8]) -> Result<Vec<u8>> {
    if output.iter().all(u8::is_ascii_whitespace) {
        return Ok(Vec::new());
    }
    let parsed: Value = serde_json::from_slice(output)
        .map_err(|error| usage(format!("dolt sql output: {error}")))?;
    let mut rows = Vec::new();
    for row in array(&parsed, "rows")? {
        serde_json::to_writer(&mut rows, row).map_err(usage)?;
        rows.push(b'\n');
    }
    Ok(rows)
}

pub struct Histories(PathBuf);

impl Histories {
    pub fn open(directory: Option<PathBuf>, project: &str) -> Result<Self> {
        let directory = match directory {
            Some(directory) => directory,
            None => crate::client::token_path()?
                .with_file_name("import-history")
                .join(project),
        };
        fs::create_dir_all(&directory)
            .map_err(|error| usage(format!("{}: {error}", directory.display())))?;
        Ok(Self(directory))
    }

    pub fn attach(&self, row: &Value, snapshot: &mut Value) -> Result<()> {
        let old_id = field(row, "id")?;
        let path = self.0.join(format!("{old_id}.json"));
        let cached = match fs::read(&path) {
            Ok(bytes) => Some(
                serde_json::from_slice::<Value>(&bytes)
                    .map_err(|error| usage(format!("{}: {error}", path.display())))?,
            ),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => return Err(usage(format!("{}: {error}", path.display()))),
        };
        let events = match cached {
            Some(cached) if cached["source"] == *row && cached["events"].is_array() => {
                cached["events"].clone()
            }
            _ => {
                let bytes = legacy(&["history", old_id, "--events", "--json"], true)?;
                let events: Value = serde_json::from_slice(&bytes).map_err(usage)?;
                let events = if events.is_null() { json!([]) } else { events };
                let temp = path.with_extension(format!("{}.tmp", std::process::id()));
                let contents =
                    serde_json::to_vec(&json!({"source": row, "events": events})).map_err(usage)?;
                fs::write(&temp, contents).map_err(usage)?;
                fs::rename(&temp, &path).map_err(usage)?;
                events
            }
        };
        let events = events
            .as_array()
            .ok_or_else(|| usage("bd history did not return an array"))?
            .clone();
        attach_events(events, snapshot);
        Ok(())
    }
}

fn attach_events(mut events: Vec<Value>, snapshot: &mut Value) {
    // Both sources use the same newest-first audit listing and break ties
    // by event ID so an unchanged history compares equal across runs.
    events.sort_by(|a, b| {
        b["created_at"]
            .as_str()
            .cmp(&a["created_at"].as_str())
            .then_with(|| a["id"].as_str().cmp(&b["id"].as_str()))
    });
    if !events.is_empty() {
        snapshot["history"] = json!([{
            "ticket": snapshot["id"], "seq": 0, "actor": snapshot["created_by"],
            "at": snapshot["created_at"], "change": events,
        }]);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_dolt_json_rows_and_empty_output() {
        let output = br#"{"rows": [{"id": "e1", "issue_id": "by-a"}, {"id": "e2", "issue_id": "by-b"}]}"#;
        let events = lines(Cursor::new(json_rows(output).ok().unwrap())).ok().unwrap();
        assert_eq!(events.keys().collect::<Vec<_>>(), ["e1", "e2"]);
        assert_eq!(events["e2"]["issue_id"], "by-b");
        assert!(json_rows(b"\n").ok().unwrap().is_empty());
        assert!(json_rows(br#"{"rows": []}"#).ok().unwrap().is_empty());
    }

    #[test]
    fn converts_legacy_aliases_uuids_and_moved_children() {
        let row = |id| {
            json!({
                "id": id, "title": "Ticket", "issue_type": "task", "status": "open",
                "priority": 2, "created_at": "2026-11-16T08:00:00Z", "updated_at": "2026-11-16T08:00:00Z",
            })
        };
        let mut child = row("by-parent.7");
        child["dependencies"] = json!([
            {"type": "parent-child", "depends_on_id": "by-ci-abcd"},
            {"type": "relates-to", "depends_on_id": "by-parent"},
        ]);
        child["started_at"] = json!("2026-11-16T08:05:00Z");
        child["comments"] = json!([
            {"id": "27e9a640-1e3d-5519-abfd-16080ed75930", "created_at": "2026-11-16T08:20:00Z", "author": "Ada", "text": "Echo"},
            {"id": "facd143d-f940-5839-ae2d-716282e5c28d", "created_at": "2026-11-16T08:10:00Z", "author": "Bea", "text": "Unicode ‘雪’\nnext line"},
        ]);
        let source = Tickets::from([
            ("by-parent".into(), row("by-parent")),
            ("by-parent.7".into(), child),
            ("by-ci-abcd".into(), row("by-ci-abcd")),
        ]);
        let converted = convert(
            &source,
            &prefix(&source).unwrap_or_else(|e| panic!("{}", e.message)),
        )
        .unwrap_or_else(|e| panic!("{}", e.message));
        assert_eq!(converted["parent"]["next_child"], 8);
        assert_eq!(converted["ci-abcd"]["next_child"], 1);
        let child = &converted["parent.7"];
        assert_eq!(child["parent"], "ci-abcd");
        assert_eq!(child["aliases"], json!(["by-parent.7"]));
        assert_eq!(
            child["edges"],
            json!([{"src":"parent.7","dst":"parent","kind":"related"}])
        );
        assert_eq!(
            child["comments"][0]["legacy_id"],
            "facd143d-f940-5839-ae2d-716282e5c28d"
        );
        assert_eq!(
            child["metadata"]["_enact_beads"]["started_at"],
            "2026-11-16T08:05:00Z"
        );
        assert_eq!(
            child["metadata"]["_enact_beads"]["dependencies"][0]["type"],
            "parent-child"
        );
        assert_eq!(child["comments"][0]["n"], 1);
        assert_eq!(child["comments"][1]["n"], 2);
    }
}
