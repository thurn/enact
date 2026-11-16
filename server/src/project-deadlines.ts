import type {TicketStore} from "./ticket-store";

type Deadline = {kind: "tickets" | "leases"; id: string; at: number};

const deadlineCursor = "feed_deadline_cursor";

export class ProjectDeadlines {
  constructor(private readonly store: TicketStore) {}

  private *unannounced(): Iterable<Deadline> {
    const cursor = Number(this.store.meta(deadlineCursor) ?? 0);
    for (const row of this.store.sql.exec<{name: string; expires_at: string}>("SELECT name, expires_at FROM leases")) {
      const at = Date.parse(row.expires_at);
      if (at > cursor) yield {kind: "leases", id: row.name, at};
    }
    for (const ticket of this.store.tickets.live.values()) {
      if (ticket.status !== "open" || !ticket.defer_until) continue;
      // A ticket stays deferred through the recorded millisecond.
      const at = Date.parse(ticket.defer_until) + 1;
      if (at > cursor) yield {kind: "tickets", id: ticket.id, at};
    }
  }

  scan(now: number) {
    const due = {tickets: [] as string[], leases: [] as string[]};
    let next = Infinity;
    for (const {kind, id, at} of this.unannounced()) {
      if (at <= now) due[kind].push(id);
      else next = Math.min(next, at);
    }
    return {...due, next};
  }

  // Advance with the journal and seq so alarm retries cannot repeat a wake.
  acknowledge(now: number): number {
    return this.store.sql.exec(`
      INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value
    `, deadlineCursor, now).rowsWritten;
  }
}
