use serde_json::Value;
use std::io::{self, Write};

/// Keep lists compact while making `show` expose the full ticket a reader needs.
pub fn details(output: &mut impl Write, value: &Value) -> io::Result<()> {
    if let Some(tickets) = value.as_array() {
        for (index, ticket) in tickets.iter().enumerate() {
            if index != 0 {
                writeln!(output)?;
            }
            details(output, ticket)?;
        }
        return Ok(());
    }
    super::human(output, value)?;
    for (label, key) in [
        ("Type", "type"),
        ("Priority", "priority"),
        ("Assignee", "assignee"),
        ("Parent", "parent"),
        ("Labels", "labels"),
        ("Ready", "ready"),
        ("Blockers", "blocked_by"),
        ("Children", "children"),
        ("Dependencies", "edges"),
        ("Deferred until", "defer_until"),
        ("Created", "created_at"),
        ("Created by", "created_by"),
        ("Run after siblings", "last"),
        ("Updated", "updated_at"),
        ("Closed", "closed_at"),
        ("Version", "version"),
    ] {
        if let Some(value) = nonempty(&value[key]) {
            writeln!(output, "{label}: {value}")?;
        }
    }
    for (label, key) in [
        ("Description", "body"),
        ("Acceptance", "acceptance"),
        ("Design", "design"),
        ("Notes", "notes"),
        ("Close reason", "close_reason"),
        ("Metadata", "metadata"),
        ("Comments", "comments"),
        ("History", "history"),
    ] {
        if let Some(value) = nonempty(&value[key]) {
            writeln!(output, "\n{label}:\n{value}")?;
        }
    }
    Ok(())
}

fn nonempty(value: &Value) -> Option<String> {
    match value {
        Value::Null => None,
        Value::String(text) if text.is_empty() => None,
        Value::Array(items) if items.is_empty() => None,
        Value::Object(fields) if fields.is_empty() => None,
        Value::String(text) => Some(text.clone()),
        Value::Array(_) | Value::Object(_) => serde_json::to_string_pretty(value).ok(),
        _ => Some(value.to_string()),
    }
}
