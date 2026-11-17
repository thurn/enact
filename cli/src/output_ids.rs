use serde_json::Value;

/// Remove large free-form fields without changing the IDs or selection of results.
pub fn brief(value: &mut Value) {
    match value {
        Value::Array(values) => values.iter_mut().for_each(brief),
        Value::Object(fields) => {
            for key in [
                "body",
                "acceptance",
                "design",
                "notes",
                "comments",
                "history",
                "payload",
                "waiters",
            ] {
                fields.remove(key);
            }
            for key in ["tickets", "results", "ticket"] {
                if let Some(value) = fields.get_mut(key) {
                    brief(value);
                }
            }
        }
        _ => {}
    }
}

pub fn display(value: &mut Value) {
    rewrite(value, display_id);
}

pub fn display_cycles(value: &mut Value) {
    for cycle in value.as_array_mut().expect("dependency cycles array") {
        for id in cycle.as_array_mut().expect("cycle members array") {
            rewrite_id(id, display_id);
        }
    }
}

fn display_id(id: &str) -> String {
    if id.starts_with('T') {
        id.to_owned()
    } else {
        format!("T{id}")
    }
}

pub fn canonical(value: &mut Value) {
    rewrite(value, |id| id.strip_prefix('T').unwrap_or(id).to_owned());
}

fn rewrite(value: &mut Value, spelling: fn(&str) -> String) {
    match value {
        Value::Array(rows) => {
            for row in rows {
                rewrite(row, spelling);
            }
        }
        Value::Object(row) => {
            for key in ["id", "parent", "src", "dst"] {
                if let Some(value) = row.get_mut(key) {
                    rewrite_id(value, spelling);
                }
            }
            if let Some(ticket) = row.get_mut("ticket") {
                if ticket.is_object() {
                    rewrite(ticket, spelling);
                } else {
                    rewrite_id(ticket, spelling);
                }
            }
            for key in [
                "tickets",
                "children",
                "closed",
                "ready",
                "conflicts",
                "live_children",
            ] {
                if let Some(Value::Array(ids)) = row.get_mut(key) {
                    for id in ids {
                        if id.is_object() {
                            rewrite(id, spelling);
                        } else {
                            rewrite_id(id, spelling);
                        }
                    }
                }
            }
            for key in [
                "edges",
                "blocked_by",
                "comments",
                "comment",
                "history",
                "results",
                "current",
                "ready",
                "differences",
                "expected_readiness_differences",
            ] {
                if let Some(nested) = row.get_mut(key) {
                    rewrite(nested, spelling);
                }
            }
            // Native history stores ID fields as old/new pairs. Imported history,
            // original aliases, and user-authored content retain their source data.
            if let Some(Value::Object(change)) = row.get_mut("change") {
                for key in ["id", "parent"] {
                    if let Some(Value::Array(values)) = change.get_mut(key) {
                        for value in values {
                            rewrite_id(value, spelling);
                        }
                    }
                }
            }
            if let Some(ticket) = row.get_mut("data").and_then(|data| data.get_mut("ticket")) {
                rewrite_id(ticket, spelling);
            }
        }
        _ => {}
    }
}

fn rewrite_id(value: &mut Value, spelling: fn(&str) -> String) {
    if let Value::String(id) = value {
        *id = spelling(id);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn round_trips_ticket_fields_without_rewriting_user_data() {
        let original = json!({
            "id": "q44s.7", "parent": "q44s", "children": ["q44s.7.1"],
            "edges": [{"src": "q44s.7", "dst": "tccc"}],
            "blocked_by": [{"id": "tccc", "rule": 2}],
            "comments": [{"ticket": "q44s.7", "text": "q44s"}],
            "history": [{"ticket": "q44s.7", "change": {"parent": [null, "q44s"]}}],
            "aliases": ["by-moved.7"], "metadata": {"id": "keep", "parent": "keep"},
        });
        let mut output = original.clone();
        display(&mut output);
        assert_eq!(
            output,
            json!({
                "id": "Tq44s.7", "parent": "Tq44s", "children": ["Tq44s.7.1"],
                "edges": [{"src": "Tq44s.7", "dst": "Ttccc"}],
                "blocked_by": [{"id": "Ttccc", "rule": 2}],
                "comments": [{"ticket": "Tq44s.7", "text": "q44s"}],
                "history": [{"ticket": "Tq44s.7", "change": {"parent": [null, "Tq44s"]}}],
                "aliases": ["by-moved.7"], "metadata": {"id": "keep", "parent": "keep"},
            })
        );
        canonical(&mut output);
        assert_eq!(output, original);
    }
}
