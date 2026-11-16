import {resolveId} from "../ticket-batch";
import {byId, decodeTicket, type Edge, type Ticket, type TicketRow} from "../ticket-types";

export type Card = Pick<Ticket, "id" | "parent" | "title" | "type" | "status" | "priority" | "assignee" | "updated_at">;
export type HomeCard = Card & {labels: string};
export type Progress = {id: string; closed: number; working: number; total: number};
export type Comment = {author: string; at: string; text: string};
export type History = {actor: string; at: string; change: string};
const cardFields = ["id", "parent", "title", "type", "status", "priority", "assignee", "updated_at"]
  .map((column) => `tickets.${column}`).join(", ");
const targetKinds = new Set<Edge["kind"]>(["blocks", "conditional-blocks", "waits-for"]);
const statusOrder: Record<Ticket["status"], number> = {
  in_progress: 0, blocked: 1, open: 2, deferred: 3, closed: 4,
};

export function byAttention(left: Card, right: Card): number {
  return statusOrder[left.status] - statusOrder[right.status]
    || left.priority - right.priority
    || (left.updated_at < right.updated_at ? 1 : left.updated_at > right.updated_at ? -1 : byId(left, right));
}

export function mentions(project: string, text: string, replace: (id: string) => string): string {
  return text.replace(/\b[a-z0-9-]+(?:\.[0-9]+)*\b/g, (word) => {
    // Bare roots can be ordinary words. Link canonical roots only in explicit
    // relationships. Dotted IDs and legacy prefixed IDs also link in prose.
    return word.startsWith(project + "-") || /^[a-z0-9]+\.[0-9]/.test(word) ? replace(word) : word;
  });
}

export type ReadProgress = (ids: Iterable<string>) => Progress[];

export function progress(sql: SqlStorage, ids: Iterable<string>): Progress[] {
  // Read the parent index rather than scanning archived tickets on each page.
  return sql.exec<Progress>(`
    WITH RECURSIVE descendants(root, id, status) AS (
      SELECT wanted.value, t.id, t.status
      FROM json_each(?) AS wanted JOIN tickets t ON t.parent = wanted.value
      UNION ALL
      SELECT d.root, t.id, t.status FROM descendants d JOIN tickets t ON t.parent = d.id
    )
    SELECT root AS id, COUNT(*) AS total, SUM(status = 'closed') AS closed,
           SUM(status = 'in_progress') AS working
    FROM descendants GROUP BY root ORDER BY root
  `, JSON.stringify([...ids])).toArray();
}

export function homeData(sql: SqlStorage, readProgress: ReadProgress) {
  const cards = sql.exec<HomeCard>(`
    SELECT ${cardFields}, labels FROM tickets
    WHERE status != 'closed'
      AND (parent IS NULL OR status IN ('in_progress', 'deferred') OR labels != '[]')
  `).toArray().sort(byAttention);
  return {cards, counts: readProgress(cards.map(({id}) => id))};
}

export function ticketData(sql: SqlStorage, project: string, row: TicketRow, readProgress: ReadProgress) {
  const ticket = decodeTicket(row);
  const {id} = ticket;
  const comments = sql.exec<Comment>("SELECT author, at, text FROM comments WHERE ticket = ? ORDER BY n", id).toArray();
  const history = sql.exec<History>("SELECT actor, at, change FROM history WHERE ticket = ? ORDER BY seq DESC", id).toArray();
  const children = sql.exec<Card>(`SELECT ${cardFields} FROM tickets WHERE parent = ?`, id).toArray().sort(byAttention);
  const edges = sql.exec<Edge>(`
    SELECT * FROM edges WHERE src = ?
    UNION ALL SELECT * FROM edges WHERE dst = ? AND src != ?
    ORDER BY src, dst
  `, id, id, id).toArray();

  const ids = new Set(children.map((child) => child.id));
  const sections: Record<"Blocked by" | "Blocking" | "Related", Set<string>> = {
    "Blocked by": new Set(), Blocking: new Set(), Related: new Set(),
  };
  for (const edge of edges) {
    const outgoing = edge.src === id;
    const other = outgoing ? edge.dst : edge.src;
    const group = targetKinds.has(edge.kind) ? (outgoing ? "Blocked by" : "Blocking") : "Related";
    sections[group].add(other);
    ids.add(other);
  }

  const chain: string[] = [];
  for (let current = ticket.parent; current && chain.length < 4 && !chain.includes(current);) {
    chain.push(current);
    ids.add(current);
    current = sql.exec<{parent: string | null}>("SELECT parent FROM tickets WHERE id = ?", current).toArray()[0]?.parent ?? null;
  }
  chain.reverse();

  const links = new Map<string, string>();
  for (const source of [ticket.body, ticket.acceptance, ticket.design, ticket.notes, ticket.close_reason ?? "",
    ...comments.map(({text}) => text), ...history.map(({change}) => change)]) {
    mentions(project, source, (name) => {
      if (!links.has(name)) {
        const target = resolveId(sql, project, name);
        links.set(name, target);
        ids.add(target);
      }
      return name;
    });
  }

  const references = sql.exec<Card>(`
    SELECT ${cardFields} FROM json_each(?) AS wanted JOIN tickets ON tickets.id = wanted.value ORDER BY tickets.id
  `, JSON.stringify([...ids])).toArray();
  return {
    ticket, comments, history, children, chain, links: [...links], references, watched: [...ids],
    groups: Object.entries(sections).map(([heading, entries]) => [heading, [...entries]] as const),
    counts: readProgress(references.map((reference) => reference.id)),
  };
}
