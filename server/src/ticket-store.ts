import schema from "../contract/schema.sql";
import {resolveId, TicketBatch} from "./ticket-batch";
import {TicketIndex, type BatchChanges} from "./ticket-index";
import {ApiError, bad, byId, choice, decodeTicket, integer, statuses, ticketTypes, type Edge, type Ticket, type TicketRow} from "./ticket-types";

export class TicketStore {
  readonly sql: SqlStorage;
  private index?: TicketIndex;

  constructor(private readonly storage: DurableObjectStorage) {
    this.sql = storage.sql;
    if (!this.sql.exec("SELECT 1 FROM sqlite_master WHERE name = 'meta' AND type = 'table'").toArray().length) {
      storage.transactionSync(() => this.sql.exec(schema));
    }
    this.sql.exec("CREATE INDEX IF NOT EXISTS aliases_id ON aliases(id)");
    if (!this.sql.exec("SELECT 1 FROM pragma_table_info('comments') WHERE name = 'legacy_id'").toArray().length) {
      this.sql.exec("ALTER TABLE comments ADD COLUMN legacy_id TEXT");
    }
  }

  get tickets(): TicketIndex {
    return this.index ??= new TicketIndex(this.sql, Number(this.meta("seq") ?? 0));
  }

  // Use after an import, which writes the contract tables directly.
  invalidateIndex(): void {
    this.index = undefined;
  }

  advanceSequence(seq: number): void {
    this.index?.advanceSequence(seq);
  }

  meta(key: string): string | undefined {
    return this.sql.exec<{value: string}>("SELECT value FROM meta WHERE key = ?", key).toArray()[0]?.value;
  }

  graph(project: string, ifNoneMatch: string | null, filter: URLSearchParams): Response {
    return this.tickets.graph(project, ifNoneMatch, flag(filter, "all"));
  }

  show(project: string, requested: string, filter: URLSearchParams): object {
    const id = resolveId(this.sql, project, requested);
    const ticket = this.tickets.lookup(id);
    if (!ticket) throw new ApiError(404, {error: `Unknown ticket ${id}`});
    const edges = this.sql.exec<Edge>(
      `SELECT * FROM edges WHERE src = ?
       UNION ALL SELECT * FROM edges WHERE dst = ? AND src != ?
       ORDER BY src, dst`, id, id, id,
    ).toArray();
    const children = this.sql.exec<{id: string}>("SELECT id FROM tickets WHERE parent = ? ORDER BY id", id)
      .toArray().map((row) => row.id);
    const comments = flag(filter, "comments") ? {
      comments: this.sql.exec("SELECT * FROM comments WHERE ticket = ? ORDER BY n", id).toArray(),
    } : {};
    const history = flag(filter, "history") ? {
      history: this.sql.exec<{change: string}>("SELECT * FROM history WHERE ticket = ? ORDER BY seq", id)
        .toArray().map((row) => ({...row, change: JSON.parse(row.change)})),
    } : {};
    const beads = flag(filter, "beads") ? this.beadsCounts(id) : {};
    return {...ticket, edges, children, ...this.tickets.readiness(ticket, Date.now()), ...comments, ...history, ...beads};
  }

  list(project: string, filter: URLSearchParams): object[] {
    const statusValue = filter.get("status");
    const status = statusValue === null ? null : choice(statusValue, "status", statuses);
    const typeValue = filter.get("type");
    const type = typeValue === null ? null : choice(typeValue, "type", ticketTypes);
    const assignee = filter.get("assignee");
    const parentValue = filter.get("parent");
    const parent = parentValue === null ? null : resolveId(this.sql, project, parentValue);
    const ancestorValue = filter.get("ancestor");
    const ancestor = ancestorValue === null ? null : resolveId(this.sql, project, ancestorValue);
    const labels = filter.getAll("label");
    const excludedTypes = filter.getAll("exclude_type");
    const unassigned = flag(filter, "unassigned");
    const includeClosed = flag(filter, "all") || status === "closed";
    const readyOnly = flag(filter, "ready");
    const beads = flag(filter, "beads");
    const epicCounts = flag(filter, "epic_counts");
    const explain = beads && flag(filter, "explain");
    const title = filter.get("title")?.toLowerCase();
    const ids = new Set(filter.getAll("id").map((id) => resolveId(this.sql, project, id)));
    const limitValue = filter.get("limit");
    if (limitValue !== null && !/^\d+$/.test(limitValue)) bad("limit must be a nonnegative integer");
    const limit = limitValue === null ? Infinity : integer(Number(limitValue), "limit");
    if (limit === 0 || (readyOnly && status !== null && status !== "open")) return [];

    const tickets = this.tickets;
    const archived = includeClosed && !readyOnly;
    const candidates = ids.size !== 0
      ? [...ids].map((id) => tickets.lookup(id)).filter((ticket): ticket is Ticket => ticket !== null && (archived || ticket.status !== "closed"))
      : archived
        ? this.archivedCandidates(status, type, assignee, parent, ancestor)
        : [...tickets.live.values()].sort(byId);
    const now = Date.now();
    const results: object[] = [];
    for (const ticket of candidates) {
      if (status !== null && ticket.status !== status || type !== null && ticket.type !== type
        || assignee !== null && ticket.assignee !== assignee || parent !== null && ticket.parent !== parent
        || unassigned && ticket.assignee !== null || excludedTypes.includes(ticket.type)
        || title !== undefined && !ticket.title.toLowerCase().includes(title)
        || !labels.every((label) => ticket.labels.includes(label))) continue;

      if (ancestor !== null && (!archived || ids.size !== 0)) {
        let current = ticket.parent;
        while (current !== null && current !== ancestor) current = tickets.parentOf(current);
        if (current === null) continue;
      }
      const readiness = tickets.readiness(ticket, now);
      const blockers = beads && (readyOnly || explain) ? this.beadsBlockers(ticket) : [];
      const beadsReady = ticket.status === "open" && (!ticket.defer_until || Date.parse(ticket.defer_until) < now)
        && blockers.length === 0;
      if (readyOnly && !(beads ? beadsReady : readiness.ready)) continue;
      const counts = beads ? {
        ...this.beadsCounts(ticket.id, archived),
        edges: [...tickets.edgesFrom(ticket.id).values()],
      } : {};
      const children = epicCounts ? this.sql.exec(
        "SELECT count(*) AS total_children, count(*) FILTER (WHERE status = 'closed') AS closed_children FROM tickets WHERE parent = ?",
        ticket.id,
      ).one() : {};
      const explanation = explain ? {beads_ready: beadsReady, beads_blockers: blockers} : {};
      results.push({...ticket, ...readiness, ...counts, ...children, ...explanation});
      if (results.length === limit) break;
    }
    return results;
  }

  private beadsCounts(id: string, includeClosed = false) {
    return this.sql.exec(
      `SELECT (SELECT count(*) FROM edges WHERE src = ?) AS dependency_count,
              (SELECT count(*) FROM edges JOIN tickets ON tickets.id = edges.src
                WHERE dst = ? AND (? OR tickets.status != 'closed')) AS dependent_count,
              (SELECT count(*) FROM comments WHERE ticket = ?) AS comment_count`,
      id, id, Number(includeClosed), id,
    ).one();
  }

  private beadsBlockers(ticket: Ticket): string[] {
    // Beads reports direct active blockers first and names the immediate
    // parent when a blocker is inherited from higher in the hierarchy.
    for (let id: string | null = ticket.id; id !== null; id = this.tickets.parentOf(id)) {
      const blockers = [...this.tickets.edgesFrom(id).values()]
        .filter((edge) => ["blocks", "conditional-blocks", "waits-for"].includes(edge.kind)
          && this.tickets.live.has(edge.dst))
        .map((edge) => edge.dst);
      if (blockers.length !== 0) return id === ticket.id ? blockers : [ticket.parent!];
    }
    return [];
  }

  private *archivedCandidates(
    status: string | null, type: string | null, assignee: string | null, parent: string | null, ancestor: string | null,
  ): Iterable<Ticket> {
    const where: string[] = [];
    const values: string[] = [];
    for (const [column, value] of [["status", status], ["type", type], ["assignee", assignee], ["parent", parent]]) {
      if (value !== null) {
        where.push(`tickets.${column} = ?`);
        values.push(value);
      }
    }
    let sources = "FROM tickets";
    if (ancestor !== null) {
      sources = `
        FROM (WITH RECURSIVE descendants(id) AS (
          SELECT id FROM tickets WHERE parent = ?
          UNION ALL
          SELECT tickets.id FROM descendants JOIN tickets ON tickets.parent = descendants.id
        ) SELECT id FROM descendants) JOIN tickets USING (id)
      `;
      values.unshift(ancestor);
    }
    const filter = where.length ? `WHERE ${where.join(" AND ")}` : "";
    for (const row of this.sql.exec<TicketRow>(`SELECT tickets.* ${sources} ${filter} ORDER BY id`, ...values)) {
      yield decodeTicket(row);
    }
  }

  batch(project: string, actor: string, body: unknown, beforeCommit?: (rowsWritten: number, changes: BatchChanges) => void, machine = "") {
    const index = this.tickets;
    const batch = new TicketBatch(this.sql, index, project, actor, Date.now(), machine);
    const {result, changes} = this.storage.transactionSync(() => {
      const result = batch.applyBatch(body);
      const changes = batch.changes();
      beforeCommit?.(batch.rowsWritten, changes);
      return {result, changes};
    });
    try {
      index.commit(changes);
    } catch (error) {
      // The database has committed. Reload it before serving another request.
      this.invalidateIndex();
      throw error;
    }
    return result;
  }
}

function flag(query: URLSearchParams, field: string): boolean {
  const value = query.get(field);
  if (value !== null && value !== "1" && value !== "0") bad(`${field} must be 0 or 1`);
  return value === "1";
}
