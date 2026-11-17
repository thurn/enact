use crate::client::{Client, Failure, Result, usage};
use crate::commands::export_pages;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Cursor};
use std::path::Path;

const BATCH_SIZE: usize = 20;
// A changed batch writes one sequence row, one feed row and one usage row.
const BATCH_OVERHEAD: u64 = 3;
// The server caps writes at 90%. Leave room inside that cap for normal
// operations, including the shared sequence writes in the budget table.
const NORMAL_ROWS: u64 = 36_000;

pub fn restore(client: &Client, file: &Path, row_budget: Option<u64>) -> Result<Value> {
    let input = File::open(file).map_err(|error| usage(format!("{}: {error}", file.display())))?;
    let mut expected = lines_with(BufReader::new(input), crate::output_ids::canonical)?;
    let mut actual = server_snapshots(client)?;
    for ticket in expected.values_mut().chain(actual.values_mut()) {
        // Import allocates new versions. Compare the ticket and all related
        // records so a partially restored backup can be resumed safely.
        ticket
            .as_object_mut()
            .expect("ticket with ID")
            .remove("version");
    }
    for (id, ticket) in &actual {
        if expected.get(id) != Some(ticket) {
            return Err(usage(format!(
                "T{id}: target contains tickets outside this restore. Use a fresh project."
            )));
        }
    }
    upload(client, expected, &actual, row_budget, &json!({}))
}

fn upload(
    client: &Client,
    expected: Tickets,
    actual: &Tickets,
    row_budget: Option<u64>,
    fence: &Value,
) -> Result<Value> {
    let mut pending = Vec::new();
    for (id, mut ticket) in expected {
        let previous = actual.get(&id).unwrap_or(&Value::Null);
        if import_difference(&ticket, previous).is_empty() {
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
        let mut required = BATCH_OVERHEAD;
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
        let mut body = fence.clone();
        body["tickets"] = json!(tickets);
        body["daily_row_ceiling"] = json!(ceiling);
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
                if !import_difference(ticket, current.get(id).unwrap_or(&Value::Null)).is_empty() {
                    return Err(usage(format!(
                        "T{id}: server ticket changed during import or is newer than the input"
                    )));
                }
            }
        }
        imported += changed;
        written += rows;
        if changed == size as u64 {
            available = available.saturating_sub(rows + BATCH_OVERHEAD - 1);
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
                "import paused after {imported} tickets and {written} rows. {tickets} tickets remain, {remaining}. Rerun the command to resume."
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
        if *cost + BATCH_OVERHEAD > ceiling {
            return json!({
                "imported": imported, "rows_written": rows, "remaining": remaining.len(), "estimated_days": null,
            });
        }
        let new_batch = in_batch == 0 || in_batch == BATCH_SIZE;
        let needed = cost + if new_batch { BATCH_OVERHEAD } else { 0 };
        if available < needed {
            days += 1;
            available = ceiling - cost - BATCH_OVERHEAD;
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
        .filter_map(|(key, value)| (actual.get(key) != Some(value)).then_some(key.as_str()))
        .collect()
}

fn import_difference<'a>(expected: &'a Value, actual: &Value) -> Vec<&'a str> {
    let mut fields = difference(expected, actual);
    // An import may supplement a prior snapshot. Preserve aliases and child
    // numbers that have already been allocated, even during reconciliation.
    fields.retain(|field| match *field {
        "next_child" => actual["next_child"].as_u64() < expected["next_child"].as_u64(),
        "aliases" => !expected["aliases"].as_array().is_some_and(|aliases| {
            actual["aliases"]
                .as_array()
                .is_some_and(|present| aliases.iter().all(|alias| present.contains(alias)))
        }),
        _ => true,
    });
    fields
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
    let fields = import_difference(ticket, previous);
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

type Tickets = BTreeMap<String, Value>;

fn lines(input: impl BufRead) -> Result<Tickets> {
    lines_with(input, |_| {})
}

fn lines_with(input: impl BufRead, prepare: impl Fn(&mut Value)) -> Result<Tickets> {
    let mut tickets = BTreeMap::new();
    for (index, line) in input.lines().enumerate() {
        let line = line.map_err(usage)?;
        if line.trim().is_empty() {
            continue;
        }
        let mut ticket: Value = serde_json::from_str(&line)
            .map_err(|error| usage(format!("line {}: {error}", index + 1)))?;
        prepare(&mut ticket);
        let id = field(&ticket, "id")?.to_owned();
        if tickets.insert(id.clone(), ticket).is_some() {
            return Err(usage(format!("duplicate ticket {id}")));
        }
    }
    Ok(tickets)
}

fn field<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key]
        .as_str()
        .ok_or_else(|| usage(format!("missing or invalid {key}")))
}

fn array<'a>(value: &'a Value, key: &str) -> Result<&'a [Value]> {
    match &value[key] {
        Value::Null => Ok(&[]),
        Value::Array(items) => Ok(items),
        _ => Err(usage(format!("invalid {key}, expected a JSON array"))),
    }
}
