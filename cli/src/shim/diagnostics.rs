use super::{array, output, string};
use crate::client::{Client, Result};
use crate::commands;
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt::Write as _;

pub fn explain(client: &Client, as_json: bool) -> Result<u8> {
    let reply = commands::read(
        client,
        &["tickets"],
        &[("beads", "1".into()), ("explain", "1".into())],
    )?;
    let graph = commands::read(client, &["graph"], &[("all", "1".into())])?;
    let tickets = array(&reply);
    let by_id: HashMap<_, _> = array(&graph["tickets"])
        .iter()
        .chain(tickets)
        .map(|ticket| (string(&ticket["id"]), ticket))
        .collect();
    let mut ready = Vec::new();
    let mut blocked = Vec::new();
    for ticket in tickets {
        let mut row = output::row(client, ticket);
        if ticket["beads_ready"] == true {
            let resolved: Vec<_> = array(&ticket["edges"])
                .iter()
                .filter(|edge| {
                    matches!(
                        string(&edge["kind"]),
                        "blocks" | "conditional-blocks" | "waits-for"
                    )
                })
                .filter_map(|edge| edge["dst"].as_str())
                .map(|id| output::legacy_id(client, id))
                .collect();
            row["reason"] = json!(if resolved.is_empty() {
                "no blocking dependencies".into()
            } else {
                format!("{} blocker(s) resolved", resolved.len())
            });
            row["resolved_blockers"] = if resolved.is_empty() {
                Value::Null
            } else {
                json!(resolved)
            };
            for field in ["dependency_count", "dependent_count"] {
                row[field] = ticket[field].clone();
            }
            if let Some(parent) = ticket["parent"].as_str() {
                row["parent"] = json!(output::legacy_id(client, parent));
            }
            ready.push(row);
        } else if !array(&ticket["beads_blockers"]).is_empty() {
            let blockers: Vec<_> = array(&ticket["beads_blockers"])
                .iter()
                .map(|id| {
                    let id = string(id);
                    let blocker = by_id.get(id).copied().unwrap_or(&Value::Null);
                    json!({
                        "id": output::legacy_id(client, id),
                        "title": string(&blocker["title"]),
                        "status": string(&blocker["status"]),
                        "priority": blocker["priority"].as_u64().unwrap_or(0),
                    })
                })
                .collect();
            row["blocked_by_count"] = json!(blockers.len());
            row["blocked_by"] = json!(blockers);
            blocked.push(row);
        }
    }
    output::sort_rows(&mut ready, true);
    output::sort_rows(&mut blocked, false);
    let cycles = blocking_cycles(client, array(&graph["edges"]));
    if as_json {
        let mut answer = json!({"ready": ready, "blocked": blocked, "schema_version": 1,
            "summary": {"total_ready": ready.len(), "total_blocked": blocked.len(), "cycle_count": cycles.len()}});
        if !cycles.is_empty() {
            answer["cycles"] = json!(cycles);
        }
        output::json(&answer)?;
    } else {
        explain_text(&ready, &blocked, &cycles);
    }
    Ok(0)
}

fn blocking_cycles(client: &Client, edges: &[Value]) -> Vec<Vec<String>> {
    let mut outgoing = BTreeMap::<&str, Vec<&str>>::new();
    for edge in edges {
        if matches!(string(&edge["kind"]), "blocks" | "conditional-blocks") {
            outgoing
                .entry(string(&edge["src"]))
                .or_default()
                .push(string(&edge["dst"]));
        }
    }
    let mut visited = HashSet::new();
    let mut cycles = Vec::new();
    for &root in outgoing.keys() {
        if !visited.insert(root) {
            continue;
        }
        let mut stack = vec![(root, outgoing[&root].iter())];
        let mut active = HashMap::from([(root, 0)]);
        while let Some((id, neighbors)) = stack.last_mut() {
            if let Some(&target) = neighbors.next() {
                if let Some(&start) = active.get(target) {
                    cycles.push(
                        stack[start..]
                            .iter()
                            .map(|(id, _)| output::legacy_id(client, id))
                            .collect(),
                    );
                } else if visited.insert(target)
                    && let Some(next) = outgoing.get(target)
                {
                    active.insert(target, stack.len());
                    stack.push((target, next.iter()));
                }
            } else {
                active.remove(id);
                stack.pop();
            }
        }
    }
    cycles
}

fn explain_text(ready: &[Value], blocked: &[Value], cycles: &[Vec<String>]) {
    println!("\n📊 Ready Work Explanation\n");
    if ready.is_empty() {
        println!("○ No ready work\n");
    } else {
        println!("● Ready ({} issues):\n", ready.len());
        for row in ready {
            println!(
                "  {} [P{}] {}\n    Reason: {}",
                string(&row["id"]),
                row["priority"],
                string(&row["title"]),
                string(&row["reason"])
            );
            let resolved: Vec<_> = array(&row["resolved_blockers"])
                .iter()
                .map(string)
                .collect();
            if !resolved.is_empty() {
                println!("    Resolved blockers: {}", resolved.join(", "));
            }
            if let Some(count) = row["dependent_count"].as_u64().filter(|count| *count != 0) {
                println!("    Unblocks: {count} issue(s)");
            }
            println!();
        }
    }
    if !blocked.is_empty() {
        println!("● Blocked ({} issues):\n", blocked.len());
        for row in blocked {
            println!(
                "  {} [P{}] {}",
                string(&row["id"]),
                row["priority"],
                string(&row["title"])
            );
            for blocker in array(&row["blocked_by"]) {
                println!(
                    "    ← blocked by {}: {} [{}]",
                    string(&blocker["id"]),
                    string(&blocker["title"]),
                    string(&blocker["status"])
                );
            }
            println!();
        }
    }
    if !cycles.is_empty() {
        println!("⚠ Cycles detected ({}):\n", cycles.len());
        for cycle in cycles {
            println!("  {} → {}", cycle.join(" → "), cycle[0]);
        }
        println!();
    }
    print!(
        "─ Summary: {} ready, {} blocked",
        ready.len(),
        blocked.len()
    );
    if !cycles.is_empty() {
        print!(", {} cycle(s)", cycles.len());
    }
    println!("\n");
}

fn date(value: &Value) -> &str {
    let value = string(value);
    value.get(..10).unwrap_or(value)
}

fn status_icon(row: &Value) -> &str {
    match string(&row["status"]) {
        "open" => "○",
        "in_progress" => "◐",
        "blocked" => "●",
        "closed" => "✓",
        "deferred" => "❄",
        "pinned" => "📌",
        _ => "◇",
    }
}

pub fn show_text(rows: &[Value]) {
    let mut output = String::new();
    for (index, row) in rows.iter().enumerate() {
        if index != 0 {
            let _ = writeln!(output, "\n{}\n", "─".repeat(60));
        }
        let kind = string(&row["issue_type"]);
        let badge = match kind {
            "epic" => " [EPIC]",
            "bug" => " [BUG]",
            _ => "",
        };
        let _ = writeln!(
            output,
            "{} {}{badge} · {}   [P{} · {}]",
            status_icon(row),
            string(&row["id"]),
            string(&row["title"]),
            row["priority"],
            string(&row["status"]).to_uppercase()
        );
        for (label, field) in [("Created by", "created_by"), ("Assignee", "assignee")] {
            if let Some(value) = row[field].as_str() {
                let _ = write!(output, "{label}: {value} · ");
            }
        }
        let _ = write!(
            output,
            "Type: {kind}\nCreated: {}",
            date(&row["created_at"])
        );
        for (label, field) in [
            ("Started", "started_at"),
            ("Updated", "updated_at"),
            ("Due", "due_at"),
            ("Deferred", "defer_until"),
        ] {
            if !row[field].is_null() {
                let _ = write!(output, " · {label}: {}", date(&row[field]));
            }
        }
        output.push('\n');
        if let Some(reason) = row["close_reason"].as_str() {
            let _ = writeln!(output, "Close reason: {reason}");
        }
        for (heading, field) in [
            ("DESCRIPTION", "description"),
            ("DESIGN", "design"),
            ("NOTES", "notes"),
            ("ACCEPTANCE CRITERIA", "acceptance_criteria"),
        ] {
            let text = row[field]
                .as_str()
                .or((field == "description").then_some("(none)"));
            if let Some(text) = text {
                let _ = writeln!(output, "\n{heading}");
                for line in text.trim_end().lines() {
                    let _ = writeln!(output, "  {line}");
                }
            }
        }
        if let Some(labels) = row["labels"].as_array() {
            let labels: Vec<_> = labels.iter().map(string).collect();
            let _ = writeln!(output, "\nLABELS: {}", labels.join(", "));
        }
        if let Some(metadata) = row["metadata"].as_object() {
            output.push_str("\nMETADATA\n");
            for (key, value) in metadata {
                let _ = writeln!(
                    output,
                    "  {key}: {}",
                    value
                        .as_str()
                        .map_or_else(|| value.to_string(), str::to_owned)
                );
            }
        }
        show_relations(&mut output, row);
        if let Some(comments) = row["comments"].as_array() {
            output.push_str("\nCOMMENTS\n");
            for comment in comments {
                let date = string(&comment["created_at"]);
                let minute = date.get(..16).unwrap_or(date).replace('T', " ");
                let _ = writeln!(output, "  {minute} {}", string(&comment["author"]));
                for line in string(&comment["text"]).trim_end().lines() {
                    let _ = writeln!(output, "      {line}");
                }
            }
        }
        output.push('\n');
    }
    print!("{output}");
}

fn show_relations(output: &mut String, row: &Value) {
    let headings = [
        ("parent-child", ("PARENT", "↑"), ("CHILDREN", "↳")),
        ("blocks", ("DEPENDS ON", "→"), ("BLOCKS", "←")),
        (
            "conditional-blocks",
            ("CONDITIONALLY DEPENDS ON", "→"),
            ("CONDITIONALLY BLOCKS", "←"),
        ),
        ("waits-for", ("WAITS FOR", "→"), ("AWAITED BY", "←")),
        (
            "discovered-from",
            ("DISCOVERED FROM", "◊"),
            ("DISCOVERED", "◊"),
        ),
    ];
    let mut related = BTreeMap::new();
    for (field, incoming) in [("dependencies", false), ("dependents", true)] {
        let relations = array(&row[field]);
        for (kind, outgoing, inbound) in headings {
            let (heading, arrow) = if incoming { inbound } else { outgoing };
            let mut count = 0;
            let mut closed = 0;
            for ticket in relations
                .iter()
                .filter(|row| row["dependency_type"] == kind)
            {
                if count == 0 {
                    let _ = writeln!(output, "\n{heading}");
                }
                show_relation(output, arrow, ticket);
                count += 1;
                closed += usize::from(ticket["status"] == "closed");
            }
            if count != 0 && incoming && kind == "parent-child" && row["issue_type"] == "epic" {
                if closed == count {
                    let _ = writeln!(
                        output,
                        "  ✓ {closed}/{count} complete (100%) — eligible for close"
                    );
                } else {
                    let _ = writeln!(
                        output,
                        "  ◐ {closed}/{count} complete ({}%)",
                        closed * 100 / count
                    );
                }
            }
        }
        for ticket in relations
            .iter()
            .filter(|row| row["dependency_type"] == "relates-to")
        {
            related.insert(string(&ticket["id"]), ticket);
        }
    }
    if !related.is_empty() {
        output.push_str("\nRELATED\n");
        for ticket in related.into_values() {
            show_relation(output, "↔", ticket);
        }
    }
}

fn show_relation(output: &mut String, arrow: &str, ticket: &Value) {
    let kind = match (string(&ticket["status"]), string(&ticket["issue_type"])) {
        ("closed", _) => "",
        (_, "epic") => "(EPIC) ",
        (_, "bug") => "(BUG) ",
        _ => "",
    };
    let _ = writeln!(
        output,
        "  {arrow} {} {}: {kind}{} P{}",
        status_icon(ticket),
        string(&ticket["id"]),
        string(&ticket["title"]),
        ticket["priority"]
    );
}
