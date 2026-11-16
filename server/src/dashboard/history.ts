import * as c from "../cirrus/components";
import {person, priorityChip, statusDot} from "./common";
import type {History} from "./data";
import {statuses, type Ticket} from "../ticket-types";

type Entry = Omit<History, "change"> & {what: string; detail?: string; quiet?: boolean};

const fieldWords: Record<string, string> = {
  body: "edited the description",
  description: "edited the description",
  acceptance: "edited the acceptance criteria",
  acceptance_criteria: "edited the acceptance criteria",
  design: "edited the design",
  notes: "edited the notes",
  type: "changed the type",
  issue_type: "changed the type",
  parent: "changed the parent",
  defer_until: "changed the deferral",
  last: "changed when this ticket runs",
};
const bookkeeping = new Set(["metadata", "updated_at", "version", "next_child", "closed_at"]);

function status(value: unknown): string {
  return statuses.includes(value as Ticket["status"]) ? statusDot(value as Ticket["status"]) : c.esc(String(value));
}

function valueOf(value: unknown): unknown {
  return Array.isArray(value) && value.length === 2 ? value[1] : value;
}

function describe(row: Omit<History, "change">, changed: Record<string, unknown>, kind?: string): Entry {
  const {actor, at} = row;
  const delta = kind ? changed : Object.fromEntries(Object.entries(changed).map(([field, value]) => [field, valueOf(value)]));
  if (delta.id !== undefined) return {actor, at, what: "created this"};
  const action = kind === "claimed" && !delta.assignee ? "claimed this" : delta.status === "closed" ? "closed this" : (kind === "claimed" || delta.status === "in_progress") && delta.assignee
    ? `claimed this for ${person(String(delta.assignee))}` : "";
  const parts = action ? [action] : [];
  for (const [field, value] of Object.entries(delta)) {
    if (bookkeeping.has(field)) continue;
    if (field === "status") {
      if (!action) parts.push(`changed status to ${status(value)}`);
    } else if (field === "priority") {
      parts.push(`set priority to ${priorityChip(Number(value))}`);
    } else if (field === "assignee") {
      if (!action) parts.push(value ? `assigned ${person(String(value))}` : "unassigned it");
    } else if (field === "title") {
      parts.push(`renamed it to “${c.esc(String(value))}”`);
    } else if (field === "labels" && Array.isArray(value)) {
      const previous = Array.isArray(changed.labels) && Array.isArray(changed.labels[0]) ? changed.labels[0] : [];
      for (const added of value.filter((label) => !previous.includes(label))) parts.push(`added label ${c.chip(String(added), "muted")}`);
      for (const removed of previous.filter((label) => !value.includes(label))) parts.push(`removed label ${c.chip(String(removed), "muted")}`);
    } else if (field !== "close_reason") {
      parts.push(Object.hasOwn(fieldWords, field) ? fieldWords[field] : `changed ${c.esc(field.replaceAll("_", " "))}`);
    }
  }
  const detail = typeof delta.close_reason === "string" ? delta.close_reason : undefined;
  return {actor, at, what: parts.join(", "), detail, quiet: !parts.length && !detail};
}

type ImportedEvent = {
  actor?: string;
  created_at?: string;
  event_type?: string;
  comment?: string;
  new_value?: unknown;
};

function describeImported(event: ImportedEvent): Entry {
  const actor = event.actor ?? "";
  const at = event.created_at ?? "";
  const kind = event.event_type ?? "event";
  const source = event.new_value;
  let detail = event.comment || "";
  let fields: Record<string, unknown> = {};
  if (typeof source === "string" && source.startsWith("{")) {
    try {
      const value = JSON.parse(source);
      if (value && !Array.isArray(value) && typeof value === "object") fields = value;
    } catch {
      // Older stores sometimes recorded prose instead of a JSON object.
      detail ||= source;
    }
  } else if (typeof source === "string") detail ||= source;

  switch (kind) {
    case "created": return {actor, at, what: "created this"};
    case "started": return {actor, at, what: "started work"};
    case "closed": return {actor, at, what: "closed this", detail};
    case "reopened": return {actor, at, what: "reopened this"};
    case "label_added":
    case "label_removed": {
      const action = kind === "label_added" ? "added" : "removed";
      const label = detail.slice(detail.indexOf(":") + 1).trim();
      return {actor, at, what: `${action} label ${c.chip(label, "muted")}`};
    }
    case "claimed":
    case "updated": return describe({actor, at}, fields, kind);
    default: return {actor, at, what: c.esc(kind.replaceAll("_", " ")), detail};
  }
}

export function timeline(ticket: Ticket, history: History[], link: (escaped: string) => string, started: string): string {
  const entries = history.flatMap((row) => {
    const change = JSON.parse(row.change) as Record<string, unknown> | ImportedEvent[];
    return Array.isArray(change)
      ? change.map(describeImported).sort((left, right) => left.at < right.at ? 1 : left.at > right.at ? -1 : 0)
      : [describe(row, change)];
  });
  const recorded = entries.length > 0;
  if (!recorded) {
    if (ticket.closed_at) entries.push({actor: "", at: ticket.closed_at, what: "closed this", detail: ticket.close_reason ?? ""});
    if (started) entries.push({actor: "", at: started, what: "started work"});
    entries.push({actor: ticket.created_by, at: ticket.created_at, what: "created this"});
  }
  const lines: string[] = [];
  for (let index = 0; index < entries.length;) {
    const event = entries[index];
    let end = index + 1;
    while (end < entries.length) {
      const next = entries[end];
      if (event.quiet ? !next.quiet : next.quiet || event.detail || next.detail || next.actor !== event.actor || next.what !== event.what) break;
      end++;
    }
    const count = end - index;
    const times = count > 1 ? ` <span class="c-muted">×${count}</span>` : "";
    const detail = event.detail ? `<span class="c-timeline-detail">${link(c.esc(event.detail))}</span>` : "";
    const body = event.quiet ? `${count} bookkeeping ${count === 1 ? "update" : "updates"} by Enact` : event.what + times + detail;
    lines.push(c.timelineEntry(body, c.time(event.at), event.quiet || !event.actor ? "" : person(event.actor), event.quiet));
    index = end;
  }
  return c.timeline(lines) + (recorded ? "" : c.empty("No events were recorded. These dates come from the ticket."));
}
