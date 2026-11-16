import type {ProjectWrite} from "./project-state";
import {
  array, bad, choice, decodeTicket, edgeKinds, integer, name, object, statuses, string, strings,
  ticketTypes, timestamp, type Ticket, type TicketRow,
} from "./ticket-types";

const pageSize = 200;

function ticketId(value: unknown, field: string): string {
  const id = name(value, field);
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[1-9]\d*)*$/.test(id)) bad(`${field} must be a ticket ID`);
  return id;
}

function fields(row: Record<string, unknown>): Omit<Ticket, "version"> {
  const nextChild = integer(row.next_child, "next_child");
  if (nextChild === 0) bad("next_child must be positive");
  return {
    id: ticketId(row.id, "id"),
    parent: row.parent === null ? null : ticketId(row.parent, "parent"),
    next_child: nextChild,
    title: string(row.title, "title"),
    body: string(row.body, "body"),
    acceptance: string(row.acceptance, "acceptance"),
    design: string(row.design, "design"),
    notes: string(row.notes, "notes"),
    type: choice(row.type, "type", ticketTypes),
    status: choice(row.status, "status", statuses),
    priority: integer(row.priority, "priority", 4),
    assignee: row.assignee === null ? null : string(row.assignee, "assignee"),
    last: integer(row.last, "last", 1) as Ticket["last"],
    labels: strings(row.labels, "labels"),
    metadata: object(row.metadata, "metadata"),
    defer_until: row.defer_until === null ? null : timestamp(row.defer_until, "defer_until"),
    close_reason: row.close_reason === null ? null : string(row.close_reason, "close_reason"),
    created_at: timestamp(row.created_at, "created_at"),
    created_by: string(row.created_by, "created_by"),
    updated_at: timestamp(row.updated_at, "updated_at"),
    closed_at: row.closed_at === null ? null : timestamp(row.closed_at, "closed_at"),
  };
}

function encode(ticket: Omit<Ticket, "version">): Omit<TicketRow, "version"> {
  return {...ticket, labels: JSON.stringify(ticket.labels), metadata: JSON.stringify(ticket.metadata)};
}

function related<T extends Record<string, SqlStorageValue>>(
  write: ProjectWrite,
  table: "edges" | "comments" | "history",
  owner: keyof T & string,
  key: keyof T & string,
  id: string,
  values: T[],
): void {
  const current = new Map([...write.sql.exec<T>(`SELECT * FROM ${table} WHERE ${owner} = ?`, id)]
    .map((row) => [row[key], row]));
  for (const row of values) {
    const previous = current.get(row[key]);
    const columns = Object.keys(row);
    if (!previous || columns.some((column) => row[column] !== previous[column])) {
      write.write(`
        INSERT INTO ${table} (${columns.join(", ")}) VALUES (${columns.map(() => "?").join(", ")})
        ON CONFLICT(${owner}, ${key}) DO UPDATE SET ${columns.slice(2).map((column) => `${column} = excluded.${column}`).join(", ")}
      `, ...Object.values(row));
    }
    current.delete(row[key]);
  }
  for (const obsolete of current.keys()) {
    write.write(`DELETE FROM ${table} WHERE ${owner} = ? AND ${key} = ?`, id, obsolete);
  }
}

function record(value: unknown, owner: string, id: string) {
  const row = object(value, owner === "src" ? "edge" : "ticket record");
  if (row[owner] !== id) bad(`${owner} must match ${id}`);
  return row;
}

function unique<T, K>(rows: T[], key: (row: T) => K): T[] {
  if (new Set(rows.map(key)).size !== rows.length) bad("Duplicate import row");
  return rows;
}

function importRelated(write: ProjectWrite, project: string, id: string, input: Record<string, unknown>): void {
  const edges = array(input.edges, "edges").map((value) => {
    const row = record(value, "src", id);
    return {src: id, dst: ticketId(row.dst, "dst"), kind: choice(row.kind, "kind", edgeKinds)};
  });
  const comments = array(input.comments, "comments").map((value) => {
    const row = record(value, "ticket", id);
    return {ticket: id, n: integer(row.n, "n"), author: string(row.author, "author"),
      at: timestamp(row.at, "at"), text: string(row.text, "text"),
      legacy_id: row.legacy_id == null ? null : name(row.legacy_id, "legacy_id")};
  });
  const history = array(input.history, "history").map((value) => {
    const row = record(value, "ticket", id);
    const change = row.change;
    if (!Array.isArray(change)) object(change, "change");
    return {ticket: id, seq: integer(row.seq, "history seq"), actor: string(row.actor, "actor"),
      at: timestamp(row.at, "at"), change: JSON.stringify(change)};
  });
  related(write, "edges", "src", "dst", id, unique(edges, (row) => row.dst));
  related(write, "comments", "ticket", "n", id, unique(comments, (row) => row.n));
  related(write, "history", "ticket", "seq", id, unique(history, (row) => row.seq));
  for (const alias of strings(input.aliases ?? [], "aliases")) {
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[1-9]\d*)*$/.test(alias)) bad("Invalid ticket alias");
    const owner = write.sql.exec<{id: string}>("SELECT id FROM aliases WHERE alias = ?", alias).toArray()[0];
    if (owner && owner.id !== id) bad(`Alias ${alias} already belongs to ${owner.id}`);
    const canonical = alias.startsWith(`${project}-`) ? alias.slice(project.length + 1) : alias;
    if (canonical !== id && write.sql.exec("SELECT 1 FROM tickets WHERE id = ?", canonical).toArray().length) {
      bad(`Alias ${alias} collides with a ticket ID`);
    }
    if (!owner) write.write("INSERT INTO aliases VALUES (?, ?)", alias, id);
  }
}

function checkParents(sql: SqlStorage, parents: Map<string, string | null>): void {
  const checked = new Set<string>();
  for (const id of parents.keys()) {
    const path = new Set<string>();
    let current: string | null = id;
    while (current !== null && !checked.has(current)) {
      if (path.has(current)) bad(`Parent cycle at ${current}`);
      path.add(current);
      const parent: string | null =
        parents.has(current) ? parents.get(current)! : sql.exec<{parent: string | null}>(
          "SELECT parent FROM tickets WHERE id = ?", current,
        ).toArray()[0]?.parent ?? null;
      current = parent;
    }
    for (const ancestor of path) checked.add(ancestor);
  }
}

export function importTickets(write: ProjectWrite, project: string, body: unknown) {
  const inputs = array(object(body, "import").tickets, "tickets");
  if (inputs.length > pageSize) bad(`At most ${pageSize} tickets can be imported at once`);
  const snapshots = inputs.map((value) => object(value, "ticket"));
  // A restored history can contain sequences from another project. The next
  // native update must allocate above every restored history entry.
  let historyFloor = 0;
  for (const snapshot of snapshots) {
    for (const value of array(snapshot.history, "history")) {
      const sequence = integer(object(value, "history row").seq, "history seq", Number.MAX_SAFE_INTEGER - 2);
      historyFloor = Math.max(historyFloor, sequence);
    }
  }
  const parents = new Map<string, string | null>();
  const seen = new Set<string>();
  for (const input of snapshots) {
    const ticket = fields(input);
    const {id} = ticket;
    if (seen.has(id)) bad(`Duplicate ticket ${id}`);
    seen.add(id);

    const current = write.sql.exec<TicketRow>("SELECT * FROM tickets WHERE id = ?", id).toArray()[0];
    if (write.sql.exec(
      "SELECT 1 FROM aliases WHERE alias IN (?, ?) AND id != ?", id, `${project}-${id}`, id,
    ).toArray().length) bad(`Ticket ${id} collides with an alias`);
    if (current && Date.parse(current.updated_at) > Date.parse(ticket.updated_at)) continue;
    if (current) ticket.next_child = Math.max(ticket.next_child, current.next_child);
    const encoded = encode(ticket);
    const columns = Object.keys(encoded) as (keyof typeof encoded)[];
    const before = write.rowsWritten;
    if (!current || columns.some((column) => encoded[column] !== current[column])) {
      const changedColumns = columns.filter((column) => current?.[column] !== encoded[column]);
      write.write(`
        INSERT INTO tickets (${columns.join(", ")}, version) VALUES (${columns.map(() => "?").join(", ")}, ?)
        ON CONFLICT(id) DO UPDATE SET ${changedColumns.map((column) => `${column} = excluded.${column}`).join(", ")},
          version = excluded.version
      `, ...Object.values(encoded), write.advance(historyFloor));
      if (!current || ticket.parent !== current.parent) parents.set(id, ticket.parent);
    }
    importRelated(write, project, id, input);
    if (write.rowsWritten !== before) write.tickets.push(id);
  }
  checkParents(write.sql, parents);
  if (write.tickets.length) write.advance(historyFloor);
  return {seq: write.seq, imported: write.tickets.length, rows_written: write.rowsWritten};
}

type ExportRow = TicketRow & {edges: string; comments: string; history: string; aliases: string};

export function exportTickets(sql: SqlStorage, after: string | null): Response {
  const cursor = after === null ? "" : ticketId(after, "after");
  const rows = sql.exec<ExportRow>(`
    SELECT tickets.*,
      (SELECT json_group_array(json_object('src', src, 'dst', dst, 'kind', kind) ORDER BY dst)
       FROM edges WHERE src = tickets.id) AS edges,
      (SELECT json_group_array(json_object('ticket', ticket, 'n', n, 'author', author, 'at', at, 'text', text, 'legacy_id', legacy_id) ORDER BY n)
       FROM comments WHERE ticket = tickets.id) AS comments,
      (SELECT json_group_array(json_object('ticket', ticket, 'seq', seq, 'actor', actor, 'at', at, 'change', json(change)) ORDER BY seq)
       FROM history WHERE ticket = tickets.id) AS history,
      (SELECT json_group_array(alias ORDER BY alias) FROM aliases WHERE id = tickets.id) AS aliases
    FROM tickets WHERE id > ? ORDER BY id LIMIT ?
  `, cursor, pageSize);
  const lines = [...rows].map(({edges, comments, history, aliases, ...row}) => JSON.stringify({
    ...decodeTicket(row), edges: JSON.parse(edges), comments: JSON.parse(comments),
    history: JSON.parse(history), aliases: JSON.parse(aliases),
  }));
  return new Response(lines.length ? lines.join("\n") + "\n" : "", {
    headers: {"Content-Type": "application/x-ndjson"},
  });
}
