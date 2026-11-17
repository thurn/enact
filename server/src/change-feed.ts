import type {FeedSocket, FeedTransport, TicketSql} from "./ticket-runtime";
import type {TicketStore} from "./ticket-store";

export type FeedChange = {
  previous: number;
  seq: number;
  tickets?: readonly string[];
  leases?: readonly string[];
  events?: readonly string[];
  native?: readonly string[];
};

type ChangeRow = {seq: number; previous: number; changes: string};
type ChangeItems = Pick<FeedChange, "tickets" | "leases" | "events"> & {resync?: true};
type ChangeFrame = {seq: number; tickets: string[]; leases: string[]; events: string[]; cutover: string};

const coalesceMilliseconds = 100;
const replayTicketLimit = 500;
const replayCommitLimit = 1_000;
const replaySequenceSpan = 4_096;
const resync = JSON.stringify({resync: true});

function frame(changes: Iterable<FeedChange & ChangeItems>, since: number, seq: number, cutover: string): ChangeFrame | null {
  const tickets = new Set<string>();
  const leases = new Set<string>();
  const events = new Set<string>();
  let reached = since;
  for (const change of changes) {
    if (change.seq <= reached) continue;
    if (change.previous > reached || change.resync) return null;
    for (const ticket of change.tickets ?? []) tickets.add(ticket);
    if (tickets.size > replayTicketLimit) return null;
    for (const lease of change.leases ?? []) leases.add(lease);
    for (const event of change.events ?? []) events.add(event);
    reached = change.seq;
  }
  if (reached !== seq) return null;
  return {seq, tickets: [...tickets], leases: [...leases], events: [...events], cutover};
}

export function ensureFeedJournal(sql: TicketSql): void {
  sql.exec(`
    CREATE TABLE IF NOT EXISTS feed_changes (
      slot INTEGER PRIMARY KEY, seq INTEGER NOT NULL, previous INTEGER NOT NULL, changes TEXT NOT NULL
    )
  `);
}

export class ChangeFeed {
  private pending: FeedChange[] = [];

  constructor(private readonly transport: FeedTransport, private readonly store: TicketStore) {
    ensureFeedJournal(store.sql);
  }

  subscribe(socket: FeedSocket, message: string | ArrayBuffer): void {
    let since: unknown;
    try {
      if (typeof message === "string" && message.length <= 128) since = JSON.parse(message)?.since;
    } catch {
      // Reject an invalid subscription with the same close code as a bad cursor.
    }
    if (typeof since !== "number" || !Number.isSafeInteger(since) || since < 0 || socket.deserializeAttachment() !== null) {
      socket.close(1008, "Expected one subscription with a nonnegative since");
      return;
    }

    const seq = Number(this.store.meta("seq") ?? 0);
    if (since === seq) {
      socket.serializeAttachment(seq);
      return;
    }
    const reply = seq > since && seq - since <= replaySequenceSpan
      ? frame(this.replay(since, seq), since, seq, this.cutover())
      : null;
    this.send(socket, reply ? JSON.stringify(reply) : resync, seq);
  }

  // Call inside the same transaction that advances meta.seq, and include
  // this row in its quota. A reconnect can replay even after eviction.
  record({seq, previous, ...items}: FeedChange): number {
    const changes = (items.tickets?.length ?? 0) > replayTicketLimit ? resync : JSON.stringify(items);
    return this.store.sql.exec(`
      INSERT INTO feed_changes VALUES (?, ?, ?, ?)
      ON CONFLICT(slot) DO UPDATE SET seq = excluded.seq, previous = excluded.previous, changes = excluded.changes
    `, seq % replaySequenceSpan, seq, previous, changes).rowsWritten;
  }

  // A late import may replace existing data only when the journal covers
  // every write since cutover and identifies any intervening native edits.
  nativeEditsSince(since: number, seq: number): Set<string> | null {
    if (!Number.isSafeInteger(since) || since > seq || seq - since > replaySequenceSpan) return null;
    const edited = new Set<string>();
    if (since === seq) return edited;
    let reached = since;
    for (const change of this.replay(since, seq)) {
      if (change.previous !== reached || change.resync) return null;
      for (const id of change.native ?? []) edited.add(id);
      reached = change.seq;
    }
    return reached === seq ? edited : null;
  }

  // Keep one row per sequence slot without paying for an index or pruning.
  // Read the two ranges in sequence order if the replay crosses slot zero.
  private *replay(since: number, seq: number): Iterable<FeedChange & ChangeItems> {
    const first = (since + 1) % replaySequenceSpan;
    const last = seq % replaySequenceSpan;
    const ranges = first <= last
      ? [[first, last]]
      : [[first, replaySequenceSpan - 1], [0, last]];
    let remaining = replayCommitLimit;
    for (const [from, to] of ranges) {
      if (remaining === 0) return;
      const rows = this.store.sql.exec<ChangeRow>(`
        SELECT seq, previous, changes FROM feed_changes
        WHERE slot BETWEEN ? AND ? AND seq > ? ORDER BY slot LIMIT ?
      `, from, to, since, remaining);
      for (const row of rows) {
        remaining--;
        yield {seq: row.seq, previous: row.previous, ...JSON.parse(row.changes) as ChangeItems};
      }
    }
  }

  // Call only once the write has committed. No timers run while the feed is idle.
  publish(change: FeedChange): void {
    if (this.transport.sockets()[Symbol.iterator]().next().done) return;
    const startWindow = this.pending.length === 0;
    this.pending.push(change);
    if (startWindow) {
      this.transport.waitUntil(new Promise<void>((resolve) => setTimeout(resolve, coalesceMilliseconds))
        .then(() => this.flush()));
    }
  }

  private cutover(): string {
    return this.store.meta("cutover") ?? "before";
  }

  private flush(): void {
    const changes = this.pending;
    this.pending = [];
    const seq = changes[changes.length - 1].seq;
    let cutover: string | undefined;
    const frames = new Map<number, string>();
    for (const socket of this.transport.sockets()) {
      const since = socket.deserializeAttachment() as number | null;
      if (since === null || since >= seq) continue;
      let message = frames.get(since);
      if (message === undefined) {
        const reply = frame(changes, since, seq, cutover ??= this.cutover());
        message = reply ? JSON.stringify(reply) : resync;
        frames.set(since, message);
      }
      this.send(socket, message, seq);
    }
  }

  private send(socket: FeedSocket, message: string, seq: number): void {
    try {
      socket.send(message);
      socket.serializeAttachment(seq);
    } catch {
      // A disconnected subscriber must not interrupt delivery to the others.
    }
  }
}
