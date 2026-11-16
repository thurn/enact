import {eventKinds} from "./project-state";
import {ancestors, liveDescendants, readiness, TicketIndex, type BatchChanges} from "./ticket-index";
import {
  ApiError, array, bad, byId, choice, edgeKinds, integer, name, object, statuses, string, strings,
  ticketTypes, timestamp, type Edge, type Ticket,
} from "./ticket-types";

type TicketInput = Partial<Pick<Ticket,
  "title" | "body" | "acceptance" | "design" | "notes" | "type" | "status" |
  "priority" | "assignee" | "parent" | "last" | "labels" | "metadata" | "defer_until"
>>;
type LegacyComment = {ticket: string; n: number; author: string; at: string; text: string; legacy_id: string | null};
type OperationResult = {id: string; closed?: string[]; ready?: string[]; comment?: LegacyComment};
type BatchResult = {seq: number; results: OperationResult[]};

const alphabet = "0123456789abcdefghjkmnpqrstvwxyz";
const dayMilliseconds = 86_400_000;

function input(value: unknown): TicketInput {
  const fields = object(value, "ticket fields");
  const result: TicketInput = {};
  for (const [field, value] of Object.entries(fields)) {
    switch (field) {
      case "title":
        result.title = name(value, field);
        break;
      case "body":
      case "acceptance":
      case "design":
      case "notes":
        result[field] = string(value, field);
        break;
      case "type":
        result.type = choice(value, field, ticketTypes);
        break;
      case "status":
        result.status = choice(value, field, statuses);
        break;
      case "priority":
        result.priority = integer(value, field, 4);
        break;
      case "assignee":
      case "parent":
        result[field] = value === null ? null : name(value, field);
        break;
      case "last":
        result.last = typeof value === "boolean" ? (value ? 1 : 0) : integer(value, field, 1) as 0 | 1;
        break;
      case "labels":
        result.labels = [...new Set(strings(value, field))];
        break;
      case "metadata":
        result.metadata = object(value, field);
        break;
      case "defer_until":
        result.defer_until = value === null ? null : timestamp(value, field);
        break;
      default:
        bad(`Unknown ticket field ${field}`);
    }
  }
  return result;
}

function entries(fields: Partial<Ticket>): [string, SqlStorageValue][] {
  return Object.entries(fields).map(([key, value]) =>
    [key, key === "labels" || key === "metadata" ? JSON.stringify(value) : value as SqlStorageValue],
  );
}

export function resolveId(sql: SqlStorage, project: string, name: string): string {
  const stripped = name.startsWith(`${project}-`) ? name.slice(project.length + 1) : name;
  return sql.exec<{id: string}>(
    "SELECT id FROM aliases WHERE alias = ? OR alias = ? ORDER BY alias = ? DESC LIMIT 1",
    stripped, name, stripped,
  ).toArray()[0]?.id ?? stripped;
}

export class TicketBatch {
  private readonly tickets = new Map<string, Ticket>();
  private readonly originals = new Map<string, Ticket | null>();
  private readonly outgoing = new Map<string, Map<string, Edge>>();
  private readonly changed = new Set<string>();
  private readonly relatedChanges = new Set<string>();
  private readonly created = new Map<string, string>();
  private readonly at: string;
  private seq: number;
  private descendants?: Map<string, string[]>;

  rowsWritten = 0;

  private readonly events = new Set<string>();

  constructor(
    private readonly sql: SqlStorage,
    private readonly index: TicketIndex,
    private readonly project: string,
    private readonly actor: string,
    private readonly now: number,
    private readonly machine: string,
  ) {
    this.seq = index.seq;
    this.at = new Date(now).toISOString();
  }

  changes(): BatchChanges {
    return {
      seq: this.seq, tickets: this.tickets, originals: this.originals, outgoing: this.outgoing, changed: this.changed,
      modified: new Set([...this.tickets.keys(), ...this.relatedChanges]),
      events: [...this.events],
    };
  }

  private write(query: string, ...values: SqlStorageValue[]): void {
    this.rowsWritten += this.sql.exec(query, ...values).rowsWritten;
  }

  private lookup(id: string): Ticket | null {
    return this.tickets.get(id) ?? this.index.lookup(id);
  }

  private resolve(value: unknown, field: string): string {
    const id = name(value, field);
    if (!id.startsWith("$")) return resolveId(this.sql, this.project, id);
    return this.created.get(id) ?? bad(`Unknown batch reference ${id}`);
  }

  private ticket(value: unknown, field = "id"): Ticket {
    const id = this.resolve(value, field);
    const ticket = this.lookup(id);
    if (!ticket) throw new ApiError(404, {error: `Unknown ticket ${id}`});
    return ticket;
  }

  private parentOf = (id: string): string | null => {
    const staged = this.tickets.get(id);
    return staged ? staged.parent : this.index.parentOf(id);
  };

  private liveTickets(): Map<string, Ticket> {
    const live = new Map(this.index.live);
    for (const [id, ticket] of this.tickets) {
      if (ticket.status === "closed") live.delete(id);
      else live.set(id, ticket);
    }
    return live;
  }

  private liveDescendants(): Map<string, string[]> {
    return this.descendants ??= liveDescendants(this.liveTickets().values(), this.parentOf);
  }

  private readyTickets(): string[] {
    const live = this.liveTickets();
    const descendants = this.liveDescendants();
    const edges = (id: string) => this.outgoing.get(id) ?? this.index.edgesFrom(id);
    return [...live.values()].sort(byId)
      .filter((ticket) => readiness(ticket, this.now, live, descendants, this.parentOf, edges).ready)
      .map(({id}) => id);
  }

  private stage(ticket: Ticket, before: Ticket | null): void {
    if (!this.originals.has(ticket.id)) this.originals.set(ticket.id, before);
    this.tickets.set(ticket.id, ticket);
    if (ticket.parent !== before?.parent || (ticket.status === "closed") !== (before?.status === "closed")) {
      this.descendants = undefined;
    }
  }

  private save(before: Ticket, changes: Partial<Ticket>): Ticket {
    const fields = Object.fromEntries(
      Object.entries(changes).filter(([key, value]) => value !== before[key as keyof Ticket]),
    );
    if (!Object.keys(fields).length) return before;
    const version = ++this.seq;
    const patch = {...fields, updated_at: this.at, version};
    const values = entries(patch);
    this.write(
      `UPDATE tickets SET ${values.map(([key]) => `${key} = ?`).join(", ")} WHERE id = ?`,
      ...values.map(([, value]) => value), before.id,
    );
    const change = Object.fromEntries(
      Object.entries(patch).map(([key, value]) => [key, [before[key as keyof Ticket], value]]),
    );
    this.write("INSERT INTO history VALUES (?, ?, ?, ?, ?)", before.id, version, this.actor, this.at, JSON.stringify(change));
    const ticket = {...before, ...patch};
    this.stage(ticket, before);
    this.changed.add(ticket.id);
    return ticket;
  }

  private allocateRoot(): string {
    while (true) {
      const bytes = crypto.getRandomValues(new Uint8Array(4));
      // Rejection sampling keeps all 22 permitted initial letters equally likely.
      if (bytes[0] >= 242) continue;
      const id = alphabet[10 + bytes[0] % 22] + [...bytes.subarray(1)].map((byte) => alphabet[byte % 32]).join("");
      if (!this.sql.exec(
        "SELECT id FROM tickets WHERE id = ? UNION ALL SELECT id FROM aliases WHERE alias IN (?, ?) LIMIT 1",
        id, id, `${this.project}-${id}`,
      ).toArray().length) return id;
    }
  }

  private create(op: Record<string, unknown>): string {
    const key = op.request_key === undefined ? null : `create:${name(op.request_key, "request_key")}`;
    if (key) {
      const row = this.sql.exec<{value: string}>("SELECT value FROM meta WHERE key = ?", key).toArray()[0];
      if (row) {
        const saved = JSON.parse(row.value) as {id: string; at: string};
        if (Date.parse(saved.at) + dayMilliseconds > this.now) return saved.id;
      }
    }

    const fields = input(op.fields);
    if (fields.title === undefined) bad("A ticket needs a title");
    const parentValue = op.parent === undefined ? fields.parent : op.parent;
    const parent = parentValue == null ? null : this.ticket(parentValue, "parent");
    let id: string;
    if (op.legacy_id !== undefined) {
      const legacyId = name(op.legacy_id, "legacy_id");
      if (parent || !legacyId.startsWith(`${this.project}-`)) bad("Invalid legacy root ID");
      id = legacyId.slice(this.project.length + 1);
      if (!/^[a-z0-9][a-z0-9-]*$/.test(id)) bad("Invalid legacy root ID");
      if (this.lookup(id) || this.sql.exec(
        "SELECT 1 FROM aliases WHERE alias IN (?, ?) LIMIT 1", id, legacyId,
      ).toArray().length) bad(`Ticket already exists: ${legacyId}`);
    } else {
      id = parent ? `${parent.id}.${parent.next_child}` : this.allocateRoot();
    }
    if (parent) {
      const next_child = parent.next_child + 1;
      this.write("UPDATE tickets SET next_child = ? WHERE id = ?", next_child, parent.id);
      this.stage({...parent, next_child}, parent);
      this.changed.add(parent.id);
    }

    const ticket: Ticket = {
      body: "", acceptance: "", design: "", notes: "", type: "task", status: "open",
      priority: 2, assignee: null, last: 0, labels: [], metadata: {}, defer_until: null,
      ...fields, title: fields.title, id, parent: parent?.id ?? null, next_child: 1,
      close_reason: null, created_at: this.at, created_by: this.actor, updated_at: this.at,
      closed_at: fields.status === "closed" ? this.at : null, version: ++this.seq,
    };
    const values = entries(ticket);
    this.write(
      `INSERT INTO tickets (${values.map(([key]) => key).join(", ")}) VALUES (${values.map(() => "?").join(", ")})`,
      ...values.map(([, value]) => value),
    );
    const change = Object.fromEntries(Object.entries(ticket).map(([key, value]) => [key, [null, value]]));
    this.write("INSERT INTO history VALUES (?, ?, ?, ?, ?)", id, ticket.version, this.actor, this.at, JSON.stringify(change));
    this.stage(ticket, null);
    this.changed.add(id);

    for (const value of array(op.deps === undefined ? [] : op.deps, "deps")) {
      const dep = typeof value === "string" ? {on: value} : object(value, "dependency");
      this.dependency(id, dep, false);
    }
    if (key) {
      this.write(
        "INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        key, JSON.stringify({id, at: this.at}),
      );
    }
    return id;
  }

  private dependency(id: string, op: Record<string, unknown>, remove: boolean): void {
    const dst = this.ticket(op.on, "on").id;
    const kind = op.kind === undefined ? undefined : choice(op.kind, "kind", edgeKinds);
    let group = this.outgoing.get(id);
    if (!group) this.outgoing.set(id, group = new Map(this.index.edgesFrom(id)));
    const existing = group.get(dst)?.kind;
    if (remove) {
      if (!existing || (kind && kind !== existing)) return;
      this.write("DELETE FROM edges WHERE src = ? AND dst = ?", id, dst);
      group.delete(dst);
    } else {
      const edge: Edge = {src: id, dst, kind: kind ?? "blocks"};
      if (existing === edge.kind) return;
      this.write(
        "INSERT INTO edges VALUES (?, ?, ?) ON CONFLICT(src, dst) DO UPDATE SET kind = excluded.kind",
        id, dst, edge.kind,
      );
      group.set(dst, edge);
    }
    this.seq++;
    this.changed.add(id);
    this.changed.add(dst);
    this.relatedChanges.add(id);
  }

  private update(ticket: Ticket, op: Record<string, unknown>): OperationResult {
    const fields = input(op.set === undefined ? {} : op.set);
    if (fields.parent) {
      const parent = this.ticket(fields.parent, "parent").id;
      if (parent === ticket.id || [...ancestors(parent, this.parentOf)].includes(ticket.id)) {
        bad("A ticket cannot become its own ancestor");
      }
      fields.parent = parent;
    }
    if (op.add_labels !== undefined || op.remove_labels !== undefined) {
      const add = strings(op.add_labels === undefined ? [] : op.add_labels, "add_labels");
      const remove = new Set(strings(op.remove_labels === undefined ? [] : op.remove_labels, "remove_labels"));
      fields.labels = [...new Set([...(fields.labels ?? ticket.labels), ...add])].filter((label) => !remove.has(label));
    }
    if (op.set_meta !== undefined || op.unset_meta !== undefined) {
      fields.metadata = {...(fields.metadata ?? ticket.metadata), ...object(op.set_meta === undefined ? {} : op.set_meta, "set_meta")};
      for (const key of strings(op.unset_meta === undefined ? [] : op.unset_meta, "unset_meta")) delete fields.metadata[key];
    }

    const wasClosed = ticket.status === "closed";
    if (fields.status === "closed" && !wasClosed) {
      this.save(ticket, {...fields, closed_at: this.at});
      return {id: ticket.id, closed: this.closeAncestors(ticket.id, null)};
    }
    const reopening = wasClosed && fields.status !== undefined && fields.status !== "closed";
    this.save(ticket, reopening ? {...fields, closed_at: null, close_reason: null} : fields);
    return {id: ticket.id};
  }

  private closeAncestors(id: string, reason: string | null): string[] {
    const closed = new Set<string>();
    if (this.parentOf(id) === null) return [];
    const descendants = this.liveDescendants();
    for (const parent of ancestors(id, this.parentOf)) {
      // Ancestors closed earlier in this loop were in the snapshot.
      if (descendants.get(parent)?.some((child) => !closed.has(child))) break;
      const ticket = this.lookup(parent);
      if (ticket && ticket.status !== "closed") {
        this.save(ticket, {status: "closed", closed_at: this.at, close_reason: reason});
        closed.add(parent);
      }
    }
    return [...closed];
  }

  private conflict(index: number, current: Ticket | null): never {
    throw new ApiError(409, {failed: index, current});
  }

  private check(value: unknown, index: number): void {
    const condition = object(value, "precondition");
    const keys = Object.keys(condition);
    if (keys.length === 1 && keys[0] === "seq") {
      if (integer(condition.seq, "seq") !== this.seq) this.conflict(index, null);
      return;
    }
    if (keys.length !== 2 || !keys.includes("id")) bad("Malformed precondition");
    const id = this.resolve(condition.id, "precondition id");
    const ticket = this.lookup(id);
    let passed: boolean;
    switch (keys.find((key) => key !== "id")) {
      case "version":
        passed = integer(condition.version, "version") === ticket?.version;
        break;
      case "assignee": {
        const assignee = condition.assignee === null ? null : string(condition.assignee, "assignee");
        passed = assignee === ticket?.assignee;
        break;
      }
      case "status":
        passed = choice(condition.status, "status", statuses) === ticket?.status;
        break;
      default:
        bad("Unknown precondition");
    }
    if (!passed) this.conflict(index, ticket);
  }

  private apply(value: unknown, index: number): OperationResult {
    const op = object(value, "operation");
    if (op.op === "create") {
      const id = this.create(op);
      this.created.set(`$${index}`, id);
      return {id};
    }
    if (op.op === "event") {
      const {id} = this.ticket(op.id);
      const kind = choice(op.kind, "kind", eventKinds);
      const data = {...object(op.data, "data"), ticket: id};
      this.write("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
        ++this.seq, this.at, kind, this.machine, this.actor, JSON.stringify(data));
      this.events.add(kind);
      return {id};
    }
    const action = choice(op.op, "op", ["update", "claim", "close", "reopen", "defer", "comment", "dep_add", "dep_remove"]);
    const ticket = this.ticket(op.id);
    const {id} = ticket;
    switch (action) {
      case "update":
        return this.update(ticket, op);
      case "claim":
        if (ticket.assignee !== null && ticket.assignee !== this.actor) {
          const original = this.originals.get(id);
          this.conflict(index, original === undefined ? ticket : original);
        }
        this.save(ticket, {assignee: this.actor, status: "in_progress", closed_at: null, close_reason: null});
        break;
      case "close": {
        const reason = string(op.reason, "reason");
        if (op.include_ready !== undefined && typeof op.include_ready !== "boolean") {
          bad("include_ready must be a boolean");
        }
        if (ticket.status !== "closed") this.save(ticket, {status: "closed", closed_at: this.at, close_reason: reason});
        // Beads closes eligible epics through its explicit epic command.
        const closed = op.legacy === true ? [] : this.closeAncestors(id, reason);
        return op.include_ready ? {id, closed, ready: this.readyTickets()} : {id, closed};
      }
      case "reopen":
        this.save(ticket, {status: "open", closed_at: null, close_reason: null});
        break;
      case "defer":
        this.save(ticket, {status: "deferred", defer_until: timestamp(op.until, "until"), closed_at: null, close_reason: null});
        break;
      case "comment": {
        const legacyId = op.legacy === true ? crypto.randomUUID() : null;
        const inserted = this.sql.exec<LegacyComment>(`
          INSERT INTO comments (ticket, n, author, at, text, legacy_id)
          SELECT ?, COALESCE(MAX(n), 0) + 1, ?, ?, ?, ? FROM comments WHERE ticket = ?
          RETURNING *
        `, id, this.actor, this.at, string(op.text, "text"), legacyId, id);
        const comment = inserted.one();
        this.rowsWritten += inserted.rowsWritten;
        this.seq++;
        this.changed.add(id);
        this.relatedChanges.add(id);
        return legacyId === null ? {id} : {id, comment};
      }
      case "dep_add":
      case "dep_remove":
        this.dependency(id, op, action === "dep_remove");
        break;
    }
    return {id};
  }

  applyBatch(value: unknown): BatchResult {
    const batch = object(value, "batch");
    const conditions = array(batch.if === undefined ? [] : batch.if, "if");
    const operations = array(batch.ops, "ops");
    conditions.forEach((condition, index) => this.check(condition, index));
    const results = operations.map((operation, index) => this.apply(operation, index));
    if (this.seq !== this.index.seq) {
      this.write("INSERT INTO meta VALUES ('seq', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", this.seq);
    }
    return {seq: this.seq, results};
  }
}
