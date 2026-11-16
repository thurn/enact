import {waiterExpires, type Waiter} from "./project-state";
import type {TicketStore} from "./ticket-store";

type Deadline = {kind: "tickets" | "leases"; id: string; at: number};

const deadlineCursor = "feed_deadline_cursor";

export class ProjectDeadlines {
  constructor(private readonly store: TicketStore) {}

  private *unannounced(now: number): Iterable<Deadline> {
    const cursor = Number(this.store.meta(deadlineCursor) ?? 0);
    for (const row of this.store.sql.exec<{name: string; expires_at: string; waiters: string}>(
      "SELECT name, expires_at, waiters FROM leases",
    )) {
      const expires = Date.parse(row.expires_at);
      if (expires > cursor) yield {kind: "leases", id: row.name, at: expires};
      if (expires > now || row.waiters === "[]") continue;

      // An expired lease may still belong to the queue head for a short
      // time. Wake the remaining waiters when that reservation lapses.
      const queue = (JSON.parse(row.waiters) as Waiter[]).map(waiterExpires);
      const head = queue.find((at) => at > Math.max(cursor, expires));
      const current = queue.findIndex((at) => at > now);
      if (current < 0) continue;
      if (head !== undefined && head <= now) yield {kind: "leases", id: row.name, at: head};
      if (queue.slice(current + 1).some((at) => at > queue[current])) {
        yield {kind: "leases", id: row.name, at: queue[current]};
      }
    }
    for (const ticket of this.store.tickets.live.values()) {
      if (ticket.status !== "open" || !ticket.defer_until) continue;
      // A ticket stays deferred through the recorded millisecond.
      const at = Date.parse(ticket.defer_until) + 1;
      if (at > cursor) yield {kind: "tickets", id: ticket.id, at};
    }
  }

  scan(now: number) {
    const due = {tickets: new Set<string>(), leases: new Set<string>()};
    let next = Infinity;
    for (const {kind, id, at} of this.unannounced(now)) {
      if (at <= now) due[kind].add(id);
      else next = Math.min(next, at);
    }
    return {tickets: [...due.tickets], leases: [...due.leases], next};
  }

  // Advance with the journal and seq so alarm retries cannot repeat a wake.
  acknowledge(now: number): number {
    return this.store.sql.exec(`
      INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value
    `, deadlineCursor, now).rowsWritten;
  }
}
