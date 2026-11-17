import schema from "../contract/schema.sql";
import {resolveId, TicketBatch} from "./ticket-batch";
import {readableTicketText} from "./ticket-references";
import {ancestors, TicketIndex, type BatchChanges} from "./ticket-index";
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

  private readableText(project: string, query: URLSearchParams) {
    if (!flag(query, "readable")) return null;
    const resolved = new Map<string, string | undefined>();
    return (value: string) => readableTicketText(project, value, (name) => {
      if (!resolved.has(name)) {
        const id = resolveId(this.sql, project, name);
        resolved.set(name, this.tickets.lookup(id)?.id);
      }
      return resolved.get(name);
    });
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
    const text = this.readableText(project, filter);
    const comments = flag(filter, "comments")
      ? this.sql.exec<{text: string}>("SELECT * FROM comments WHERE ticket = ? ORDER BY n", id).toArray()
      : undefined;
    const history = flag(filter, "history") ? {
      history: this.sql.exec<{change: string}>("SELECT * FROM history WHERE ticket = ? ORDER BY seq", id)
        .toArray().map((row) => ({...row, change: JSON.parse(row.change)})),
    } : {};
    const beads = flag(filter, "beads") ? this.beadsCounts(id) : {};
    return {...ticket, ...(text && readableFields(ticket, text)), edges, children,
      ...this.tickets.readiness(ticket, Date.now()),
      ...(comments && {comments: text ? comments.map((comment) => ({...comment, text: text(comment.text)})) : comments}),
      ...history, ...beads};
  }

  list(project: string, filter: URLSearchParams): object[] {
    const statusValue = filter.get("status");
    const search = filter.get("search") ?? undefined;
    const selectedStatuses = statusValue === null || (search !== undefined && statusValue === "all")
      ? [] : (search === undefined ? [statusValue] : statusValue.split(","))
        .map((value) => choice(value, "status", statuses));
    const status = selectedStatuses.length === 1 ? selectedStatuses[0] : null;
    const typeValue = filter.get("type");
    const type = typeValue === null ? null : choice(typeValue, "type", ticketTypes);
    const assignee = filter.get("assignee");
    const parentValue = filter.get("parent");
    const parent = parentValue === null ? null : resolveId(this.sql, project, parentValue);
    const ancestorValue = filter.get("ancestor");
    const ancestor = ancestorValue === null ? null : resolveId(this.sql, project, ancestorValue);
    const labels = filter.getAll("label");
    const anyLabels = filter.getAll("label_any");
    const excludedTypes = filter.getAll("exclude_type");
    const unassigned = flag(filter, "unassigned");
    const includeClosed = flag(filter, "all") || selectedStatuses.includes("closed")
      || search !== undefined && selectedStatuses.length === 0;
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
    const candidates = search !== undefined
      ? this.searchCandidates(project, search, selectedStatuses)
      : ids.size !== 0
      ? [...ids].map((id) => tickets.lookup(id)).filter((ticket): ticket is Ticket => ticket !== null && (archived || ticket.status !== "closed"))
      : archived
        ? this.archivedCandidates(status, type, assignee, parent, ancestor)
        : [...tickets.live.values()].sort(byId);
    const now = Date.now();
    const text = this.readableText(project, filter);
    const results: object[] = [];
    for (const ticket of candidates) {
      if (selectedStatuses.length !== 0 && !selectedStatuses.includes(ticket.status) || type !== null && ticket.type !== type
        || assignee !== null && ticket.assignee !== assignee || parent !== null && ticket.parent !== parent
        || unassigned && ticket.assignee !== null || excludedTypes.includes(ticket.type)
        || title !== undefined && !ticket.title.toLowerCase().includes(title)) continue;
      if (anyLabels.length === 0) {
        if (!labels.every((label) => ticket.labels.includes(label))) continue;
      } else if (!this.matchesLabels(ticket, labels, anyLabels, selectedStatuses)) {
        continue;
      }

      if (ancestor !== null && (!archived || ids.size !== 0 || search !== undefined)) {
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
        ...this.beadsCounts(ticket.id, archived || search !== undefined),
        ...(search === undefined ? {edges: [...tickets.edgesFrom(ticket.id).values()]} : {}),
      } : {};
      const children = epicCounts ? this.sql.exec(
        "SELECT count(*) AS total_children, count(*) FILTER (WHERE status = 'closed') AS closed_children FROM tickets WHERE parent = ?",
        ticket.id,
      ).one() : {};
      const explanation = explain ? {beads_ready: beadsReady, beads_blockers: blockers} : {};
      results.push({...ticket, ...(text && readableFields(ticket, text)), ...readiness, ...counts, ...children, ...explanation});
      if (results.length === limit) break;
    }
    return results;
  }

  private matchesLabels(ticket: Ticket, all: string[], any: string[], selectedStatuses: readonly string[]): boolean {
    const matches = (labels: string[]) => all.every((label) => labels.includes(label)) && any.some((label) => labels.includes(label));
    if (matches(ticket.labels)) return true;
    // Beads includes descendants of label matches while still applying
    // filters like status to each returned ticket.
    for (const id of ancestors(ticket.id, (id) => this.tickets.parentOf(id))) {
      const parent = this.tickets.lookup(id);
      if (parent && (selectedStatuses.length === 0 || selectedStatuses.includes(parent.status))
        && matches(parent.labels)) return true;
    }
    return false;
  }

  private *searchCandidates(project: string, phrase: string, selectedStatuses: readonly string[]): Iterable<Ticket> {
    if (!phrase.trim()) bad("search query is required");
    const query = phrase.toLowerCase();
    const prefix = `${project}-`;
    const displayedId = phrase.startsWith("T");
    const idPrefix = displayedId ? query.slice(1) : query.startsWith(prefix) ? query.slice(prefix.length) : query;
    const aliases = new Set<string>();
    if (idPrefix && !displayedId) {
      // Prefix matching must also find aliases that point to a different ID.
      for (const {id} of this.sql.exec<{id: string}>(
        "SELECT id FROM aliases WHERE instr(alias, ?) = 1 OR instr(alias, ?) = 1", query, idPrefix,
      )) aliases.add(id);
    }
    // Apply the Beads search order before the limit. Fetch large fields and
    // compatibility counts only for rows that can appear in the results.
    const where = selectedStatuses.length === 0 ? "" : `WHERE status IN (${selectedStatuses.map(() => "?").join(", ")})`;
    for (const {id, title} of this.sql.exec<{id: string; title: string}>(
      `SELECT id, title FROM tickets ${where} ORDER BY priority, created_at DESC, id`, ...selectedStatuses,
    )) {
      if (title.toLowerCase().includes(query) || id.startsWith(query) || (idPrefix && id.startsWith(idPrefix)) || aliases.has(id)) {
        const ticket = this.tickets.lookup(id);
        if (ticket) yield ticket;
      }
    }
  }

  private beadsCounts(id: string, includeClosed = false) {
    return this.sql.exec(
      `SELECT (SELECT count(*) FROM edges WHERE src = ? AND kind = 'blocks') AS dependency_count,
              (SELECT count(*) FROM edges JOIN tickets ON tickets.id = edges.src
                WHERE dst = ? AND kind = 'blocks' AND (? OR tickets.status != 'closed')) AS dependent_count,
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

function readableFields(ticket: Ticket, text: (value: string) => string) {
  return {
    body: text(ticket.body), acceptance: text(ticket.acceptance), design: text(ticket.design),
    notes: text(ticket.notes), close_reason: ticket.close_reason === null ? null : text(ticket.close_reason),
  };
}
