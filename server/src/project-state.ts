import {bad, choice, integer, name, object} from "./ticket-types";
import type {Identity} from "./token-auth";

export const dayMilliseconds = 86_400_000;
const waiterLifetime = 60_000;
const eventKinds = ["session", "ci", "machine", "cutover", "usage"] as const;
const cutoverStates = ["before", "paused", "after"] as const;
type Cutover = typeof cutoverStates[number];

export type Waiter = {holder: string; since: string; machine?: string};
type Lease = {
  name: string;
  holder: string;
  machine: string;
  expires_at: string;
  payload: Record<string, unknown>;
  waiters: Waiter[];
};
type LeaseRow = Omit<Lease, "payload" | "waiters"> & {payload: string; waiters: string};
type EventRow = {seq: number; at: string; kind: string; machine: string; actor: string; data: string};

function lease(row: LeaseRow): Lease {
  return {...row, payload: JSON.parse(row.payload), waiters: JSON.parse(row.waiters)};
}

export function waiterExpires({since}: Waiter): number {
  return Date.parse(since) + waiterLifetime;
}

function liveWaiters(current: Lease, now: number): Waiter[] {
  return current.waiters.filter((waiter) => waiterExpires(waiter) > now);
}

export function leases(sql: SqlStorage, now: number): Lease[] {
  return sql.exec<LeaseRow>("SELECT * FROM leases ORDER BY name").toArray().map((row) => {
    const current = lease(row);
    return {...current, waiters: liveWaiters(current, now)};
  }).filter((current) => Date.parse(current.expires_at) > now || current.waiters.length > 0);
}

export function events(sql: SqlStorage, query: URLSearchParams) {
  const requestedKind = query.get("kind");
  const kind = requestedKind === null ? null : choice(requestedKind, "kind", eventKinds);
  const since = queryInteger(query, "since", 0);
  const limit = queryInteger(query, "limit", 200);
  // With no cursor, return the latest page. A cursor reads forward so a
  // consumer can catch up one page at a time without skipping events.
  const order = query.has("since") ? "ASC" : "DESC";
  const rows = kind === null
    ? sql.exec<EventRow>(`SELECT * FROM events WHERE seq > ? ORDER BY seq ${order} LIMIT ?`, since, limit)
    : sql.exec<EventRow>(`SELECT * FROM events WHERE kind = ? AND seq > ? ORDER BY seq ${order} LIMIT ?`, kind, since, limit);
  return rows.toArray().map((row) => ({...row, data: JSON.parse(row.data)}));
}

function queryInteger(query: URLSearchParams, field: string, fallback: number): number {
  const value = query.get(field);
  if (value === null) return fallback;
  if (!/^\d+$/.test(value)) bad(`${field} must be a nonnegative integer`);
  return integer(Number(value), field);
}

export function cutoverState(sql: SqlStorage): {cutover: Cutover; cutover_at: string | null} {
  const rows = sql.exec<{key: string; value: string}>("SELECT * FROM meta WHERE key IN ('cutover', 'cutover_at')");
  const meta = Object.fromEntries([...rows].map(({key, value}) => [key, value]));
  return {cutover: (meta.cutover ?? "before") as Cutover, cutover_at: meta.cutover_at ?? null};
}

export class ProjectWrite {
  readonly tickets: string[] = [];
  readonly leases: string[] = [];
  readonly events: string[] = [];
  rowsWritten = 0;
  private sequence?: number;

  constructor(readonly sql: SqlStorage, readonly now: number) {}

  get seq(): number {
    return this.sequence ?? Number(
      this.sql.exec<{value: string}>("SELECT value FROM meta WHERE key = 'seq'").toArray()[0]?.value ?? 0,
    );
  }

  write(query: string, ...values: SqlStorageValue[]): number {
    const rows = this.sql.exec(query, ...values).rowsWritten;
    this.rowsWritten += rows;
    return rows;
  }

  advance(after = 0): number {
    if (this.sequence !== undefined) return this.sequence;
    this.sequence = Math.max(this.seq, after) + 1;
    this.meta("seq", String(this.sequence));
    return this.sequence;
  }

  private meta(key: string, value: string): void {
    this.write("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, value);
  }

  cutover(body: unknown): void {
    const wanted = choice(object(body, "state").cutover, "cutover", cutoverStates);
    const {cutover} = cutoverState(this.sql);
    if (cutover === wanted) return;
    if (cutover === "after" || (cutover === "before" && wanted !== "paused")) {
      bad(`Cannot move cutover from ${cutover} to ${wanted}`);
    }
    this.meta("cutover", wanted);
    // Retain the start of the pause for the final reconciliation pull.
    if (wanted === "paused") this.meta("cutover_at", new Date(this.now).toISOString());
    if (wanted === "before") this.write("DELETE FROM meta WHERE key = 'cutover_at'");
    const seq = this.advance();
    if (wanted === "after") this.meta("cutover_seq", String(seq));
  }

  event(identity: Identity, body: unknown) {
    const input = object(body, "event");
    const kind = choice(input.kind, "kind", eventKinds);
    if (!("data" in input)) bad("Missing event data");
    const {actor, machine} = identity;
    const event = {seq: this.advance(), at: new Date(this.now).toISOString(), kind, machine, actor, data: input.data};
    this.write("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
      event.seq, event.at, kind, machine, actor, JSON.stringify(event.data));
    this.events.push(kind);
    return event;
  }

  pruneEvents(): void {
    this.write("DELETE FROM events WHERE at < ?", new Date(this.now - 30 * dayMilliseconds).toISOString());
  }

  updateLease(leaseName: string, machine: string, body: unknown): {status: number; body: object} {
    const input = object(body, "lease");
    const op = choice(input.op, "op", ["acquire", "renew", "release"]);
    const holder = name(input.holder, "holder");
    const ttl = op === "release" ? 0 : integer(input.ttl_s, "ttl_s", Math.floor((8.64e15 - this.now) / 1000));
    if (op !== "release" && ttl === 0) bad("ttl_s must be positive");
    const payload = input.payload === undefined ? undefined : object(input.payload, "payload");
    const row = this.sql.exec<LeaseRow>("SELECT * FROM leases WHERE name = ?", leaseName).toArray()[0];
    const current = row && lease(row);
    const active = current && Date.parse(current.expires_at) > this.now;
    const held = active && current.holder === holder && current.machine === machine;
    const waiters = current ? liveWaiters(current, this.now) : [];
    const caller = (waiter: Waiter) => waiter.holder === holder && (waiter.machine ?? machine) === machine;
    const at = new Date(this.now).toISOString();

    if (op !== "acquire" && !held) {
      return {status: 409, body: {holder: active ? current.holder : null, expires_at: active ? current.expires_at : null}};
    }
    if (op === "acquire" && current && !held && (active || (waiters.length > 0 && !caller(waiters[0])))) {
      const index = waiters.findIndex(caller);
      const position = index === -1 ? waiters.length + 1 : index + 1;
      waiters[position - 1] = {holder, since: at, machine};
      const serialized = JSON.stringify(waiters);
      if (serialized !== row.waiters) this.write("UPDATE leases SET waiters = ? WHERE name = ?", serialized, leaseName);
      return {status: 409, body: {holder: current.holder, expires_at: current.expires_at, position, seq: this.seq}};
    }

    if (op === "release" && !waiters.length) {
      this.write("DELETE FROM leases WHERE name = ?", leaseName);
      this.advance();
      this.leases.push(leaseName);
      return {status: 200, body: {...current, expires_at: at, waiters}};
    }
    const next = {
      name: leaseName, machine, holder,
      expires_at: op === "release" ? at : new Date(this.now + ttl * 1000).toISOString(),
      payload: op === "release" ? current!.payload : payload ?? (held ? current.payload : {}),
      waiters: waiters.filter((waiter) => !caller(waiter)),
    };
    this.write(`
      INSERT INTO leases VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(name) DO UPDATE SET holder = excluded.holder, machine = excluded.machine,
        expires_at = excluded.expires_at, payload = excluded.payload, waiters = excluded.waiters
    `, next.name, holder, machine, next.expires_at, JSON.stringify(next.payload), JSON.stringify(next.waiters));
    this.advance();
    this.leases.push(leaseName);
    return {status: 200, body: next};
  }
}
