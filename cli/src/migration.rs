mod source;

use crate::client::{Client, Failure, Result, usage};
use crate::commands::export_pages;
use serde_json::{Value, json};
use source::{Histories, Tickets, array, canonical_id, convert, field, legacy, lines, prefix};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::{BufReader, Cursor};
use std::path::{Path, PathBuf};

const BATCH_SIZE: usize = 20;
// The server caps writes at 90%. Leave room inside that cap for normal
// operations, including the shared sequence writes in the budget table.
const NORMAL_ROWS: u64 = 36_000;

pub fn import(
    client: &Client,
    file: &Path,
    history_dir: Option<PathBuf>,
    row_budget: Option<u64>,
) -> Result<Value> {
    let input = File::open(file).map_err(|error| usage(format!("{}: {error}", file.display())))?;
    let source = lines(BufReader::new(input))?;
    let expected = snapshots(&source, history_dir, &client.project)?;
    let actual = server_snapshots(client)?;
    let mut pending = Vec::new();
    for (id, mut ticket) in expected {
        let previous = actual.get(&id).unwrap_or(&Value::Null);
        if difference(&ticket, previous).is_empty() {
            continue;
        }
        // The allocation counter and aliases survive a replay of an older export.
        if let Some(counter) = previous["next_child"].as_u64()
            && counter > count(&ticket, "next_child")?
        {
            ticket["next_child"] = json!(counter);
        }
        let rows = estimate(&ticket, previous)?;
        pending.push((ticket, rows));
    }
    pending.sort_by_key(|(ticket, _)| ticket["status"] != "closed");
    if pending.is_empty() {
        return Ok(report(0, 0, &[], 1));
    }

    let state = state_snapshot(client)?;
    let limit = count(&state["limits"], "rows_written")?;
    let normal_ceiling = (limit - limit.div_ceil(10)).saturating_sub(NORMAL_ROWS);
    let ceiling = row_budget.map_or(normal_ceiling, |budget| budget.min(normal_ceiling));
    let mut available = ceiling.saturating_sub(count(&state, "rows_written")?);
    let mut imported = 0;
    let mut written = 0;
    let mut start = 0;
    while start < pending.len() {
        // One sequence row and one usage row per changed request.
        let mut required = 2;
        let size = pending[start..]
            .iter()
            .take(BATCH_SIZE)
            .take_while(|(_, estimate)| {
                required += estimate;
                required <= available
            })
            .count();
        if size == 0 {
            break;
        }
        let tickets: Vec<_> = pending[start..start + size]
            .iter()
            .map(|(ticket, _)| ticket)
            .collect();
        let body = json!({"tickets": tickets, "daily_row_ceiling": ceiling});
        let reply = match client.post(&client.url(&["import"], &[]), &body, true) {
            Ok(reply) => reply.json()?,
            Err(error) if error.code == 5 => break,
            Err(error) => return Err(error),
        };
        let rows = count(&reply, "rows_written")?;
        let changed = count(&reply, "imported")?;
        if changed != size as u64 {
            // A transport retry may have replayed a batch that already committed.
            // Read it back to distinguish that from a skipped stale snapshot.
            let current = server_snapshots(client)?;
            for ticket in tickets {
                let id = field(ticket, "id")?;
                if !difference(ticket, current.get(id).unwrap_or(&Value::Null)).is_empty() {
                    return Err(usage(format!(
                        "{id}: server ticket changed during import or is newer than the Beads export"
                    )));
                }
            }
        }
        imported += changed;
        written += rows;
        if changed == size as u64 {
            available = available.saturating_sub(rows + 1);
        } else {
            available = ceiling.saturating_sub(count(&state_snapshot(client)?, "rows_written")?);
        }
        start += size;
    }

    let result = report(imported, written, &pending[start..], ceiling);
    if start == pending.len() {
        Ok(result)
    } else {
        let tickets = pending.len() - start;
        let days = result["estimated_days"].as_u64();
        let remaining = days.map_or_else(
            || "the configured budget cannot fit the next ticket".to_owned(),
            |days| format!("about {days} more UTC day(s) at the configured budget"),
        );
        let mut failure = Failure::new(
            5,
            format!(
                "import paused after {imported} tickets and {written} rows. {tickets} tickets remain, {remaining}. Rerun import to resume."
            ),
        );
        failure.body = Some(result);
        Err(failure)
    }
}

fn report(imported: u64, rows: u64, remaining: &[(Value, u64)], ceiling: u64) -> Value {
    let mut days = 0;
    let mut available = 0;
    let mut in_batch = 0;
    for (_, cost) in remaining {
        if *cost + 2 > ceiling {
            return json!({
                "imported": imported, "rows_written": rows, "remaining": remaining.len(), "estimated_days": null,
            });
        }
        let new_batch = in_batch == 0 || in_batch == BATCH_SIZE;
        let needed = cost + if new_batch { 2 } else { 0 };
        if available < needed {
            days += 1;
            available = ceiling - cost - 2;
            in_batch = 1;
        } else {
            available -= needed;
            in_batch = if new_batch { 1 } else { in_batch + 1 };
        }
    }
    json!({"imported": imported, "rows_written": rows, "remaining": remaining.len(), "estimated_days": days})
}

fn state_snapshot(client: &Client) -> Result<Value> {
    client
        .get(&client.url(&["state"], &[]), None, client.retry_for)?
        .json()
}

fn count(value: &Value, key: &str) -> Result<u64> {
    value[key]
        .as_u64()
        .ok_or_else(|| usage(format!("missing or invalid {key}")))
}

fn snapshots(source: &Tickets, directory: Option<PathBuf>, project: &str) -> Result<Tickets> {
    let prefix = prefix(source)?;
    let mut converted = convert(source, &prefix)?;
    let history = Histories::open(directory, project)?;
    // Work serially at lower priority so the one-time history scan does not
    // compete with concurrent Enact operations on the Dolt store.
    for (legacy, row) in source {
        let id = canonical_id(legacy, &prefix)?;
        history.attach(row, converted.get_mut(&id).expect("converted source"))?;
    }
    Ok(converted)
}

fn server_snapshots(client: &Client) -> Result<Tickets> {
    let mut tickets = Tickets::new();
    export_pages(client, |page| {
        for (id, ticket) in lines(Cursor::new(page))? {
            if tickets.insert(id.clone(), ticket).is_some() {
                return Err(usage(format!("server export repeated ticket {id}")));
            }
        }
        Ok(())
    })?;
    Ok(tickets)
}

fn difference<'a>(expected: &'a Value, actual: &Value) -> Vec<&'a str> {
    if actual.is_null() {
        return vec!["missing from ticket server"];
    }
    expected
        .as_object()
        .expect("ticket object")
        .iter()
        .filter_map(|(key, value)| {
            let same = match key.as_str() {
                "next_child" => actual[key].as_u64() >= value.as_u64(),
                "aliases" => value.as_array().is_some_and(|aliases| {
                    aliases.iter().all(|alias| {
                        actual[key]
                            .as_array()
                            .is_some_and(|present| present.contains(alias))
                    })
                }),
                _ => actual[key] == *value,
            };
            (!same).then_some(key.as_str())
        })
        .collect()
}

fn changed_rows<'a>(
    ticket: &'a Value,
    previous: &'a Value,
    family: &str,
    key: &str,
) -> Result<u64> {
    let index = |ticket: &'a Value| -> Result<BTreeMap<String, &'a Value>> {
        Ok(array(ticket, family)?
            .iter()
            .map(|row| (row[key].to_string(), row))
            .collect())
    };
    let new = index(ticket)?;
    let old = index(previous)?;
    let edits = new
        .iter()
        .filter(|(key, row)| old.get(*key) != Some(*row))
        .count();
    let removed = old.keys().filter(|key| !new.contains_key(*key)).count();
    Ok((edits + removed) as u64)
}

fn estimate(ticket: &Value, previous: &Value) -> Result<u64> {
    let fields = difference(ticket, previous);
    // Count the ticket row and both secondary indexes conservatively. An
    // alias and an edge can also consume an index write.
    let ticket_rows = if fields
        .iter()
        .any(|field| !["aliases", "edges", "comments", "history"].contains(field))
    {
        1 + u64::from(
            ticket["status"] != "closed"
                || previous["status"].as_str().is_some_and(|s| s != "closed"),
        ) + u64::from(!ticket["parent"].is_null() || !previous["parent"].is_null())
    } else {
        0
    };
    let previous_aliases = array(previous, "aliases")?;
    let aliases = array(ticket, "aliases")?
        .iter()
        .filter(|alias| !previous_aliases.contains(alias))
        .count() as u64;
    Ok(ticket_rows
        + aliases * 2
        + changed_rows(ticket, previous, "edges", "dst")? * 2
        + changed_rows(ticket, previous, "comments", "n")?
        + changed_rows(ticket, previous, "history", "seq")?)
}

pub fn compare(client: &Client, history_dir: Option<PathBuf>) -> Result<Value> {
    let export = legacy(&["export"], false)?;
    let source = lines(Cursor::new(export))?;
    let prefix = prefix(&source)?;
    let expected = snapshots(&source, history_dir, &client.project)?;
    let actual = server_snapshots(client)?;
    let ready = legacy(&["ready", "--json", "--brief", "--limit", "0"], false)?;
    let ready: Vec<Value> = serde_json::from_slice(&ready).map_err(usage)?;
    let ready = ready
        .iter()
        .map(|row| canonical_id(field(row, "id")?, &prefix))
        .collect::<Result<BTreeSet<_>>>()?;
    let graph = client
        .get(&client.url(&["graph"], &[]), None, client.retry_for)?
        .json()?;
    let server_ready = array(&graph, "tickets")?
        .iter()
        .map(|ticket| {
            Ok((
                field(ticket, "id")?,
                ticket["ready"]
                    .as_bool()
                    .ok_or_else(|| usage("missing graph readiness"))?,
            ))
        })
        .collect::<Result<BTreeMap<_, _>>>()?;
    let ids = expected
        .keys()
        .chain(actual.keys())
        .collect::<BTreeSet<_>>();
    let mut differences = Vec::new();
    let mut output = Vec::new();
    for id in ids {
        let changes = match (expected.get(id), actual.get(id)) {
            (Some(before), after) => difference(before, after.unwrap_or(&Value::Null)),
            (None, _) => vec!["missing from Beads"],
        };
        let readiness = (expected
            .get(id)
            .is_some_and(|ticket| ticket["status"] == "open")
            && actual.contains_key(id))
        .then(|| {
            let legacy = ready.contains(id);
            let server = server_ready.get(id.as_str()).copied().unwrap_or(false);
            (legacy != server).then_some((legacy, server))
        })
        .flatten();
        if !changes.is_empty() || readiness.is_some() {
            let mut description = changes.join(", ");
            if let Some((legacy, server)) = readiness {
                if !description.is_empty() {
                    description.push_str(", ");
                }
                description.push_str(&format!("ready: Beads {legacy}, server {server}"));
            }
            output.push(format!("{id}: {description}"));
            differences.push(json!({
                "id": id, "fields": changes,
                "ready": readiness.map(|(legacy, server)| json!({"beads": legacy, "server": server})),
            }));
        }
    }
    let result = json!({"compared": expected.len(), "differences": differences});
    if output.is_empty() {
        Ok(result)
    } else {
        let mut failure = usage(format!(
            "{} ticket(s) differ\n{}",
            output.len(),
            output.join("\n")
        ));
        failure.body = Some(result);
        Err(failure)
    }
}
