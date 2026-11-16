import {byId, decodeTicket, type Blocker, type Edge, type Ticket, type TicketRow} from "./ticket-types";

type ParentOf = (id: string) => string | null;
type Archive = {
  tickets: Map<string, string>;
  edges: Map<string, string>;
  encoded?: {tickets: string; edges: string};
};
type GraphCache = {key: string; body: string; expires: number};

export function* ancestors(id: string, parentOf: ParentOf): Iterable<string> {
  for (let parent = parentOf(id); parent !== null; parent = parentOf(parent)) {
    yield parent;
  }
}

// Closed tickets may still have live descendants. Index the whole path of
// each live ticket, including any closed parents along that path.
export function liveDescendants(live: Iterable<Ticket>, parentOf: ParentOf): Map<string, string[]> {
  const descendants = new Map<string, string[]>();
  for (const ticket of live) {
    for (const parent of ancestors(ticket.id, parentOf)) {
      const group = descendants.get(parent);
      if (group) group.push(ticket.id);
      else descendants.set(parent, [ticket.id]);
    }
  }
  return descendants;
}

export type BatchChanges = {
  seq: number;
  tickets: ReadonlyMap<string, Ticket>;
  outgoing: ReadonlyMap<string, ReadonlyMap<string, Edge>>;
  changed: ReadonlySet<string>;
  modified: ReadonlySet<string>;
};

export class TicketIndex {
  readonly live = new Map<string, Ticket>();
  private readonly closedParents = new Map<string, string | null>();
  private readonly outgoing = new Map<string, ReadonlyMap<string, Edge>>();
  private descendants: Map<string, string[]>;
  private readonly graphCache = new Map<boolean, GraphCache>();
  private archive?: Archive;

  constructor(private readonly sql: SqlStorage, public seq: number) {
    for (const row of sql.exec<TicketRow>("SELECT * FROM tickets WHERE status != 'closed' ORDER BY id")) {
      this.live.set(row.id, decodeTicket(row));
      this.outgoing.set(row.id, new Map());
    }
    this.descendants = liveDescendants(this.live.values(), (id) => this.parentOf(id));
    for (const id of this.descendants.keys()) this.outgoing.set(id, new Map());

    // The recursive query reads outgoing edges of live tickets and their
    // ancestors, including blockers whose target tickets are already closed.
    const edges = new Map<string, Map<string, Edge>>();
    for (const edge of sql.exec<Edge>(`
      WITH RECURSIVE sources(id, parent) AS (
        SELECT id, parent FROM tickets WHERE status != 'closed'
        UNION
        SELECT tickets.id, tickets.parent FROM sources JOIN tickets ON tickets.id = sources.parent
      )
      SELECT src, dst, kind FROM sources JOIN edges ON edges.src = sources.id ORDER BY src, dst
    `)) {
      let group = edges.get(edge.src);
      if (!group) edges.set(edge.src, group = new Map());
      group.set(edge.dst, edge);
    }
    for (const [id, group] of edges) this.outgoing.set(id, group);
  }

  lookup(id: string): Ticket | null {
    const live = this.live.get(id);
    if (live) return live;
    const row = this.sql.exec<TicketRow>("SELECT * FROM tickets WHERE id = ?", id).toArray()[0];
    return row ? decodeTicket(row) : null;
  }

  parentOf(id: string): string | null {
    const live = this.live.get(id);
    if (live) return live.parent;
    const cached = this.closedParents.get(id);
    if (cached !== undefined) return cached;
    const row = this.sql.exec<{parent: string | null}>("SELECT parent FROM tickets WHERE id = ?", id).toArray()[0];
    if (row) this.closedParents.set(id, row.parent);
    return row?.parent ?? null;
  }

  edgesFrom(id: string): ReadonlyMap<string, Edge> {
    return this.outgoing.get(id) ?? new Map(
      [...this.sql.exec<Edge>("SELECT * FROM edges WHERE src = ? ORDER BY dst", id)].map((edge) => [edge.dst, edge]),
    );
  }

  advanceSequence(seq: number): void {
    if (seq === this.seq) return;
    this.seq = seq;
    this.graphCache.clear();
  }

  commit(changes: BatchChanges): void {
    if (this.seq === changes.seq) return;
    this.advanceSequence(changes.seq);
    let topologyChanged = false;
    for (const [id, ticket] of changes.tickets) {
      const previous = this.live.get(id);
      const isLive = ticket.status !== "closed";
      topologyChanged ||= Boolean(previous) !== isLive || this.parentOf(id) !== ticket.parent;
      if (isLive) this.live.set(id, ticket);
      else this.live.delete(id);
      this.closedParents.delete(id);
    }
    if (topologyChanged) {
      this.descendants = liveDescendants(this.live.values(), (id) => this.parentOf(id));
      for (const id of this.outgoing.keys()) {
        if (!this.live.has(id) && !this.descendants.has(id)) this.outgoing.delete(id);
      }
      for (const id of [...this.live.keys(), ...this.descendants.keys()]) {
        if (!this.outgoing.has(id)) this.outgoing.set(id, this.edgesFrom(id));
      }
    }
    for (const [id, group] of changes.outgoing) {
      if (this.live.has(id) || this.descendants.has(id)) this.outgoing.set(id, group);
    }
    this.refreshArchive(changes);
  }

  readiness(ticket: Ticket, now: number): {ready: boolean; blocked_by: Blocker[]} {
    return readiness(ticket, now, this.live, this.descendants, (id) => this.parentOf(id), (id) => this.edgesFrom(id));
  }

  private loadArchive(): Archive {
    const tickets = new Map<string, string>();
    const edges = new Map<string, string>();
    // Serialize in SQLite so a cold all-ticket graph doesn't decode and
    // then re-encode every archived ticket and edge in JavaScript.
    for (const {id, fragment} of this.sql.exec<{id: string; fragment: string}>(`
      SELECT id, json_object(
        'id', id, 'parent', parent, 'next_child', next_child, 'title', title,
        'acceptance', acceptance, 'type', type, 'status', status, 'priority', priority,
        'assignee', assignee, 'last', last, 'labels', json(labels), 'metadata', json(metadata),
        'defer_until', defer_until, 'close_reason', close_reason, 'created_at', created_at,
        'created_by', created_by, 'updated_at', updated_at, 'closed_at', closed_at, 'version', version,
        'ready', json('false')
      ) AS fragment FROM tickets WHERE status = 'closed' ORDER BY id
    `)) {
      tickets.set(id, fragment);
    }
    for (const {src, fragment} of this.sql.exec<{src: string; fragment: string}>(`
      SELECT src, group_concat(json_object('src', src, 'dst', dst, 'kind', kind) ORDER BY dst) AS fragment
      FROM edges JOIN tickets ON tickets.id = edges.src
      WHERE tickets.status = 'closed' GROUP BY src ORDER BY src
    `)) {
      edges.set(src, fragment);
    }
    return {tickets, edges};
  }

  private refreshArchive(changes: BatchChanges): void {
    const archive = this.archive;
    if (!archive) return;
    for (const [id, ticket] of changes.tickets) {
      if (ticket.status === "closed") {
        archive.encoded = undefined;
        archive.tickets.set(id, JSON.stringify({...graphFields(ticket), ready: false}));
        if (!archive.edges.has(id)) archive.edges.set(id, encodeEdges(this.edgesFrom(id)));
      } else {
        if (archive.tickets.delete(id)) archive.encoded = undefined;
        archive.edges.delete(id);
      }
    }
    for (const [id, group] of changes.outgoing) {
      if (archive.tickets.has(id)) {
        archive.encoded = undefined;
        archive.edges.set(id, encodeEdges(group));
      }
    }
  }

  graph(ifNoneMatch: string | null, all = false, now = Date.now()): Response {
    let cache = this.graphCache.get(all);
    if (!cache || now >= cache.expires) {
      const live = [...this.live.values()].sort(byId);
      let expires = Infinity;
      let lastExpiry = 0;
      const tickets = live.map((ticket) => {
        const defer = ticket.defer_until ? Date.parse(ticket.defer_until) : 0;
        const {ready, blocked_by} = this.readiness(ticket, now);
        if (ticket.status === "open" && defer && !blocked_by.some((blocker) => blocker.rule !== 1)) {
          if (defer >= now) expires = Math.min(expires, defer + 1);
          else lastExpiry = Math.max(lastExpiry, defer);
        }
        return {...graphFields(ticket), ready, blocked_by: blocked_by.filter(({rule}) => rule !== 1)};
      });
      const key = `${this.seq}${lastExpiry ? `-${lastExpiry}` : ""}`;
      // A revalidation should not load archived rows, even after the
      // Durable Object has woken with an empty archive cache.
      if (matchesTag(ifNoneMatch, key)) return graphResponse(null, key, 304);
      const edges = live.flatMap(({id}) => [...this.edgesFrom(id).values()]
        .filter((edge) => all || this.live.has(edge.dst))
        .sort((left, right) => compare(left.dst, right.dst)));
      const archive = all ? this.archive ??= this.loadArchive() : undefined;
      const archived = archive ? archive.encoded ??= {
        tickets: encodeMap(archive.tickets), edges: encodeMap(archive.edges),
      } : undefined;
      const body = `{"tickets":[${joinSegments(encodeItems(tickets), archived?.tickets ?? "")}],`
        + `"edges":[${joinSegments(encodeItems(edges), archived?.edges ?? "")}],"seq":${this.seq}}`;
      cache = {key, body, expires};
      this.graphCache.set(all, cache);
    }
    return matchesTag(ifNoneMatch, cache.key)
      ? graphResponse(null, cache.key, 304)
      : graphResponse(cache.body, cache.key, 200);
  }
}

function matchesTag(ifNoneMatch: string | null, key: string): boolean {
  return ifNoneMatch?.split(",").some((tag) =>
    ["*", `"${key}"`, key].includes(tag.trim().replace(/^W\//, ""))) ?? false;
}

function graphResponse(body: string | null, key: string, status: number): Response {
  return new Response(body, {status, headers: {"ETag": `"${key}"`, "Content-Type": "application/json"}});
}

function graphFields({body, design, notes, ...ticket}: Ticket) {
  return ticket;
}

function encodeItems(items: Iterable<object>): string {
  return [...items].map((item) => JSON.stringify(item)).join(",");
}

function encodeEdges(edges: ReadonlyMap<string, Edge>): string {
  return encodeItems([...edges.values()].sort((left, right) => compare(left.dst, right.dst)));
}

function joinSegments(live: string, archive: string): string {
  if (!archive) return live;
  return live ? `${live},${archive}` : archive;
}

function compare(left: string, right: string): number {
  return left < right ? -1 : Number(left > right);
}

function encodeMap(values: Map<string, string>): string {
  return [...values.keys()].sort().map((key) => values.get(key)!).filter(Boolean).join(",");
}

export function readiness(
  ticket: Ticket,
  now: number,
  live: ReadonlyMap<string, Ticket>,
  descendants: ReadonlyMap<string, string[]>,
  parentOf: ParentOf,
  edgesFrom: (id: string) => ReadonlyMap<string, Edge>,
): {ready: boolean; blocked_by: Blocker[]} {
  const {id, parent} = ticket;
  const blockers: Blocker[] = [];
  const checkEdges = (source: string, rule: 2 | 3) => {
    for (const edge of edgesFrom(source).values()) {
      if (edge.kind === "related" || edge.kind === "discovered-from") continue;
      if (live.has(edge.dst) || descendants.has(edge.dst)) {
        blockers.push({id: edge.dst, rule});
      }
    }
  };
  if (ticket.status !== "open" || (ticket.defer_until && Date.parse(ticket.defer_until) >= now)) {
    blockers.push({id, rule: 1});
  }
  checkEdges(id, 2);
  for (const parent of ancestors(id, parentOf)) checkEdges(parent, 3);
  if (ticket.last && parent) {
    for (const sibling of descendants.get(parent) ?? []) {
      if (sibling !== id && !live.get(sibling)!.last) blockers.push({id: sibling, rule: 4});
    }
  }
  for (const child of descendants.get(id) ?? []) {
    if (live.get(child)!.parent === id) blockers.push({id: child, rule: 5});
  }
  blockers.sort((left, right) => left.rule - right.rule || byId(left, right));
  return {ready: blockers.length === 0, blocked_by: blockers};
}
