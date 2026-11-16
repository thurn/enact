import * as c from "../cirrus/components";
import {cutoverState, events, leases} from "../project-state";
import type {ProjectUsage} from "../project-usage";
import {person, ticketUrl} from "./common";

type Event = ReturnType<typeof events>[number];
type Usage = ReturnType<ProjectUsage["state"]>;

function field(event: Event, key: string): string | undefined {
  const value: unknown = event.data?.[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

function activeSessions(root: string, sql: SqlStorage, entries: Event[]) {
  const latest = new Map<string, Event>();
  for (const event of entries) {
    const id = field(event, "session") ?? event.actor;
    if (!latest.has(id)) latest.set(id, event);
  }
  const project = root.slice(1, -1);
  const active = [];
  for (const [session, event] of latest) {
    const action = field(event, "action") ?? field(event, "state") ?? field(event, "status");
    const ticket = field(event, "ticket") ?? field(event, "bead");
    if (!ticket || (action && !["spawn", "start", "started", "active", "running", "in_progress", "claimed"].includes(action))) continue;
    const id = ticket.startsWith(project + "-") ? ticket.slice(project.length + 1) : ticket;
    active.push({session, machine: event.machine, id});
  }
  const tickets = sql.exec<{id: string; title: string; status: string}>(`
    SELECT id, title, status FROM tickets WHERE id IN (SELECT value FROM json_each(?))
  `, JSON.stringify(active.map(({id}) => id)));
  const summaries = new Map([...tickets].map((ticket) => [ticket.id, ticket]));
  const sessions = active.flatMap(({session, machine, id}) => {
    const work = summaries.get(id);
    if (work?.status === "closed") return [];
    const label = c.link(ticketUrl(root, id), c.esc(work?.title ?? id));
    return c.row(person(session) + c.muted(" · ") + label, c.esc(machine));
  });
  return sessions.length ? c.rows(sessions) : c.empty("No active sessions reported.");
}

const resultLabels: Record<string, [c.Tone, string]> = {
  passed: ["green", "Passed"], pass: ["green", "Passed"], green: ["green", "Passed"],
  failed: ["red", "Failed"], fail: ["red", "Failed"], red: ["red", "Failed"],
  running: ["blue", "Running"],
};

const osNames = new Map([["darwin", "macOS"], ["macos", "macOS"], ["windows", "Windows"], ["linux", "Linux"]]);

function ci(entries: Event[]) {
  const latest = new Map<string, Event>();
  for (const event of entries) {
    const os = field(event, "os");
    if (!os) continue;
    const name = osNames.get(os.toLowerCase()) ?? os;
    if (!latest.has(name)) latest.set(name, event);
  }
  const results = [...latest].sort(([a], [b]) => a.localeCompare(b)).map(([os, event]) => {
    const passed: unknown = event.data?.passed;
    const result = typeof passed === "boolean" ? (passed ? "passed" : "failed")
      : field(event, "result") ?? field(event, "state") ?? field(event, "status") ?? "Reported";
    const [tone, label] = Object.hasOwn(resultLabels, result) ? resultLabels[result] : ["neutral", result] as const;
    const sha = field(event, "tip") ?? field(event, "sha");
    const revision = sha ? c.muted(" · ") + c.mono(sha.slice(0, 8)) : "";
    return c.row(c.tip(c.esc(os), event.machine),
      c.dot(tone, label) + revision + c.muted(" · ") + c.time(event.at));
  });
  return results.length ? c.rows(results) : c.empty("No slow CI results reported.");
}

export class ProjectStatus {
  private ciRetentionDay?: string;
  private ciSequence?: number;
  private readonly ciByOs = new Map<string, Event>();
  private ciMarkup = "";
  private sessionsKey?: string;
  private sessions: Event[] = [];

  constructor(private readonly sql: SqlStorage) {}

  private latestSequence(kind: string): number | undefined {
    return this.sql.exec<{seq: number}>(`
      SELECT seq FROM events WHERE kind = ? ORDER BY seq DESC LIMIT 1
    `, kind).toArray()[0]?.seq;
  }

  private recentSessions(day: string): Event[] {
    const key = `${day}:${this.latestSequence("session")}`;
    if (key !== this.sessionsKey) {
      this.sessions = events(this.sql, new URLSearchParams({kind: "session"}));
      this.sessionsKey = key;
    }
    return this.sessions;
  }

  private latestCi(day: string): string {
    const {sql} = this;
    const seq = this.latestSequence("ci");
    const retained = sql.exec<{value: string}>("SELECT value FROM meta WHERE key = 'last_retention_day'").toArray()[0]?.value;
    const retentionDay = retained ?? day;
    const sameRetention = this.ciRetentionDay === retentionDay;
    if (sameRetention && seq === this.ciSequence) return this.ciMarkup;

    // A new result only changes one OS. Read the retained history on the first
    // render and after daily retention, then group only newer rows.
    if (!sameRetention) this.ciByOs.clear();
    const rows = sql.exec<Omit<Event, "data"> & {data: string}>(`
      SELECT * FROM events WHERE seq IN (
        SELECT MAX(seq) FROM events
        WHERE kind = 'ci' AND seq > ? AND json_type(data, '$.os') = 'text'
        GROUP BY lower(json_extract(data, '$.os'))
      ) ORDER BY seq DESC
    `, sameRetention ? this.ciSequence ?? Number.MIN_SAFE_INTEGER : Number.MIN_SAFE_INTEGER);
    for (const row of rows) {
      const event = {...row, data: JSON.parse(row.data)};
      const os = field(event, "os")?.toLowerCase();
      if (os) this.ciByOs.set(os, event);
    }
    this.ciRetentionDay = retentionDay;
    this.ciSequence = seq;
    this.ciMarkup = ci([...this.ciByOs.values()]);
    return this.ciMarkup;
  }

  render(root: string, now: number, usage: Usage): string {
    const {sql} = this;
    const staging = leases(sql, now).find((lease) => lease.name === "staging");
    const held = staging && Date.parse(staging.expires_at) > now;
    const holder = held ? person(staging.holder) + c.muted(" · ") + c.esc(staging.machine) : c.muted("Available");
    const queue = staging?.waiters.map(({holder}) => person(holder));
    const {cutover, cutover_at} = cutoverState(sql);
    const cutoverLabel = {before: "Before cutover", paused: "Writes paused", after: "Ticket server active"}[cutover];
    const state = c.dot(cutover === "after" ? "green" : cutover === "paused" ? "amber" : "neutral", cutoverLabel);
    return c.subsection("Sessions", activeSessions(root, sql, this.recentSessions(usage.day)))
      + c.subsection("Slow CI", this.latestCi(usage.day))
      + c.subsection("Staging lease", c.meta([
        ["Holder", holder],
        ...(held ? [["Expires", c.time(staging.expires_at)] as const] : []),
        ["Waiting", queue?.length ? queue.join(", ") : c.muted("Nobody")],
      ]))
      + c.subsection("Cutover", state + (cutover_at ? c.muted(" · ") + c.time(cutover_at) : ""))
      + c.subsection("Daily usage", c.meta([
        ["UTC day", c.esc(usage.day)],
        ["Requests", c.progress(usage.requests, usage.limits.requests)],
        ["Rows written", c.progress(usage.rows_written, usage.limits.rows_written)],
      ]));
  }
}
