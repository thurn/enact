import { ChangeFeed, type FeedChange } from "./change-feed";
import { Dashboard, dashboardRoute } from "./dashboard";
import { storedProject } from "./project-paths";
import { ProjectDeadlines } from "./project-deadlines";
import { cutoverState, dayMilliseconds, events, leases, ProjectWrite } from "./project-state";
import { type UsageMeter } from "./project-usage";
import { exportTickets, importTickets } from "./ticket-transfer";
import type { BatchChanges } from "./ticket-index";
import { TicketStore } from "./ticket-store";
import { ApiError, integer, object } from "./ticket-types";
import { authorizeWrite, TokenRegistry, type Identity } from "./token-auth";

import type {FeedTransport, TicketStorage} from "./ticket-runtime";

export type ProjectRuntime = {
  storage: TicketStorage;
  feed: FeedTransport;
  usage(now: number): UsageMeter;
  getAlarm(): Promise<number | null>;
  setAlarm(at: number): Promise<void>;
  readOnly?: boolean;
};

export function apiPath(pathname: string) {
  const match = /^\/([a-z0-9][a-z0-9-]*)\/api(\/.*)$/.exec(pathname);
  if (match) match[1] = storedProject(match[1]);
  return match;
}

export class TicketProject {
  readonly store: TicketStore;
  readonly feed: ChangeFeed;
  private readonly dashboard: Dashboard;
  private readonly deadlines: ProjectDeadlines;
  private readonly tokens: TokenRegistry;

  constructor(readonly name: string, private readonly repository: string | undefined,
    private readonly runtime: ProjectRuntime) {
    this.store = new TicketStore(runtime.storage);
    this.dashboard = new Dashboard(this.store.sql);
    this.feed = new ChangeFeed(runtime.feed, this.store);
    this.deadlines = new ProjectDeadlines(this.store);
    this.tokens = new TokenRegistry(this.store, () => runtime.usage(Date.now()),
      () => this.writable());
  }

  writable(): boolean {
    return !this.runtime.readOnly;
  }

  async start(): Promise<void> {
    this.store.tickets;
    if (this.writable()) await this.reschedule(Date.now());
  }

  private async reschedule(at: number): Promise<void> {
    const {next, tickets, leases} = this.deadlines.scan(at);
    const due = tickets.length || leases.length || this.deadlines.dueSnoozes(at).length;
    // Retention must also catch up after a cold start.
    const day = new Date(at).toISOString().slice(0, 10);
    await this.scheduleAlarm(due || this.store.meta("last_retention_day") !== day ? at : next);
  }

  private state(usage: UsageMeter) {
    return {...cutoverState(this.store.sql), seq: Number(this.store.meta("seq") ?? 0), ...usage.state()};
  }

  private get(project: string, path: string, request: Request, query: URLSearchParams, usage: UsageMeter, now: number) {
    const sql = this.store.sql;
    if (path === "/feed") return this.runtime.feed.upgrade(request);
    if (path === "/graph") return this.store.graph(project, request.headers.get("If-None-Match"), query);
    if (path === "/tickets") return Response.json(this.store.list(project, query));
    if (path === "/state") return Response.json(this.state(usage));
    if (path === "/events") return Response.json(events(sql, query));
    if (path === "/leases") return Response.json(leases(sql, now));
    if (path === "/export") return exportTickets(sql, query.get("after"));
    const ticket = /^\/tickets\/([^/]+)$/.exec(path)?.[1];
    if (ticket) return Response.json(this.store.show(project, decodeURIComponent(ticket), query));
    return Response.json({error: "Unknown route"}, {status: 404});
  }

  private write(project: string, path: string, lease: string | undefined, identity: Identity, body: unknown, now: number, usage: UsageMeter) {
    if (path === "/batch") {
      const {cutover, cutover_id} = cutoverState(this.store.sql);
      if (cutover === "paused" && cutover_id !== null) {
        throw new ApiError(409, {error: "Ticket writes are paused for cutover"});
      }
      const previous = this.store.tickets.seq;
      let change: FeedChange | undefined;
      let committed: BatchChanges | undefined;
      const reply = this.store.batch(project, identity.actor, body, (rows, changes) => {
        if (changes.seq !== previous) {
          change = {previous, seq: changes.seq, tickets: [...changes.changed],
            native: [...changes.modified], events: changes.events};
        }
        this.record(usage, rows, change);
        committed = changes;
      }, identity.machine);
      if (committed) this.dashboard.applyTicketChanges(committed, (id) => this.store.tickets.parentOf(id));
      if (committed?.tickets.size) this.deadlines.invalidateSnoozes();
      if (change) this.feed.publish(change);
      const armDeadlines = change?.tickets?.some((id) => {
        const ticket = this.store.tickets.live.get(id);
        return ticket?.defer_until && (ticket.status === "deferred"
          || ticket.status === "open" && Date.parse(ticket.defer_until) >= now);
      });
      return {body: reply, status: 200, armDeadlines};
    }

    const write = new ProjectWrite(this.store.sql, now);
    const previous = write.seq;
    let change: FeedChange | undefined;
    const result = this.runtime.storage.transactionSync(() => {
      const reply = this.mutate(project, path, lease, identity, body, write);
      if (write.seq !== previous) {
        change = {previous, seq: write.seq, tickets: write.tickets, leases: write.leases, events: write.events};
      }
      const ceiling = path === "/import" ? object(body, "import").daily_row_ceiling : undefined;
      if (ceiling !== undefined) integer(ceiling, "daily_row_ceiling");
      this.record(usage, write.rowsWritten, change);
      return reply;
    });
    if (path === "/import" && change) {
      this.store.invalidateIndex();
      this.dashboard.invalidateProgress();
    } else this.store.advanceSequence(write.seq);
    if (change) this.feed.publish(change);
    const changedSnoozes = change !== undefined && (path === "/import" || path === "/state");
    if (changedSnoozes) this.deadlines.invalidateSnoozes();
    const armDeadlines = changedSnoozes || (lease !== undefined && write.rowsWritten > 0);
    return {...(result ?? {body: this.state(usage)}), armDeadlines};
  }

  private record(usage: UsageMeter, rows: number, change?: FeedChange, requests = 1): void {
    const total = rows + (change ? this.feed.record(change) : 0);
    usage.record(total, requests);
  }

  private mutate(project: string, path: string, lease: string | undefined, identity: Identity, body: unknown, write: ProjectWrite):
    {body: object; status?: number} | undefined {
    if (path === "/import") {
      const {cutover, cutover_id} = cutoverState(this.store.sql);
      const input = object(body, "import");
      const id = input.pause_id ?? null;
      if (input.before_cutover === true && cutover !== "before") {
        throw new ApiError(409, {error: "Staged import requires Beads to be authoritative"});
      }
      if ((id !== null || (cutover === "paused" && cutover_id !== null))
          && (cutover !== "paused" || id !== cutover_id)) {
        throw new ApiError(409, {error: "The managed cutover pause has changed"});
      }
      const nativeEdits = cutover === "after"
        ? this.feed.nativeEditsSince(Number(this.store.meta("cutover_seq")), write.seq)
        : undefined;
      return {body: importTickets(write, project, body, nativeEdits)};
    }
    if (path === "/events") return {body: write.event(identity, body)};
    if (lease !== undefined) return write.updateLease(decodeURIComponent(lease), identity.machine, body);
    write.cutover(body);
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const [, apiProject, path] = apiPath(url.pathname) ?? [];
    const dashboard = request.method === "GET" || request.method === "HEAD" ? dashboardRoute(url.pathname) : null;
    const project = apiProject ?? dashboard?.project;
    if (!project || project !== this.name) return Response.json({error: "Unknown route"}, {status: 404});

    if (!this.writable() && request.method !== "GET" && request.method !== "HEAD") {
      return Response.json({error: "Ticket service is read-only"}, {status: 503});
    }

    const lease = /^\/leases\/(.+)$/.exec(path)?.[1];
    let accounted = false;
    try {
      if (request.method === "POST" && (lease !== undefined || ["/batch", "/state", "/events", "/import"].includes(path))) {
        const identity = await authorizeWrite(request, (hash) => this.tokens.lookup(this.repository, hash));
        const body = await request.json();
        const operations = path === "/batch" ? object(body, "batch").ops : undefined;
        if (path === "/events" || (Array.isArray(operations)
            && operations.some((op: {op?: string} | null) => op?.op === "event"))) {
          // Arm retention before writing the first event so an eviction cannot
          // strand events in a project that then receives no more requests.
          await this.scheduleAlarm();
        }
        const now = Date.now();
        const result = this.write(project, path, lease, identity, body, now, this.runtime.usage(now));
        accounted = true;
        if (result.armDeadlines) {
          const at = Date.now();
          const {next, tickets, leases} = this.deadlines.scan(at);
          const due = tickets.length || leases.length || this.deadlines.dueSnoozes(at).length;
          await this.scheduleAlarm(due ? at : next);
        }
        return Response.json(result.body, {status: result.status ?? 200});
      }
      const now = Date.now();
      const usage = this.runtime.usage(now);
      const readyRead = request.method === "GET"
        && (path === "/graph" || path === "/tickets" && url.searchParams.get("ready") === "1");
      if (this.writable() && (!readyRead || !this.wakeSnoozes(project, now, usage))) usage.record();
      accounted = true;
      if (dashboard) return await this.dashboard.fetch(request, dashboard, now, usage);
      if (request.method === "GET") return this.get(project, path, request, url.searchParams, usage, now);
      return Response.json({error: "Unknown route"}, {status: 404});
    } catch (error) {
      if (!accounted && this.writable()) this.runtime.usage(Date.now()).record();
      if (error instanceof ApiError) return Response.json(error.body, {status: error.status});
      if (error instanceof SyntaxError || error instanceof URIError) {
        return Response.json({error: "Malformed request"}, {status: 400});
      }
      throw error;
    }
  }

  private async scheduleAlarm(deadline = Infinity): Promise<void> {
    const nextDay = (Math.floor(Date.now() / dayMilliseconds) + 1) * dayMilliseconds;
    const next = Math.min(nextDay, deadline);
    if (!this.writable()) return;
    const current = await this.runtime.getAlarm();
    if (this.writable() && (current === null || next < current)) await this.runtime.setAlarm(next);
  }

  private wakeSnoozes(project: string, now: number, usage: UsageMeter): boolean {
    const ids = this.deadlines.dueSnoozes(now);
    if (!ids.length) return false;
    this.write(project, "/batch", undefined, {actor: "ticket deadline", machine: ""}, {
      ops: ids.map((id) => ({op: "update", id: `${project}-${id}`, set: {status: "open", defer_until: null}})),
    }, now, usage);
    return true;
  }

  async alarm(): Promise<void> {
    if (!this.writable()) return;
    const now = Date.now();
    const usage = this.runtime.usage(now);
    const woke = this.wakeSnoozes(this.name, now, usage);
    const {tickets, leases, next} = this.deadlines.scan(now);
    const write = new ProjectWrite(this.store.sql, now);
    const day = usage.state().day;
    let change: FeedChange | undefined;
    this.runtime.storage.transactionSync(() => {
      if (this.store.meta("last_retention_day") !== day) {
        write.pruneEvents();
        write.write("INSERT OR REPLACE INTO meta VALUES ('last_retention_day', ?)", day);
      }
      if (tickets.length || leases.length) {
        const previous = write.seq;
        change = {previous, seq: write.advance(), tickets, leases};
        write.rowsWritten += this.deadlines.acknowledge(now);
      }
      this.record(usage, write.rowsWritten, change, woke ? 0 : 1);
    });
    if (change) {
      this.store.advanceSequence(change.seq);
      this.feed.publish(change);
    }
    await this.scheduleAlarm(next);
  }
}
