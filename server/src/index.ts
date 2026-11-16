import { DurableObject } from "cloudflare:workers";
import { version } from "../package.json";
import { ChangeFeed, type FeedChange } from "./change-feed";
import { ProjectDeadlines } from "./project-deadlines";
import { cutoverState, dayMilliseconds, events, leases, ProjectWrite } from "./project-state";
import { ProjectUsage } from "./project-usage";
import { exportTickets, importTickets } from "./ticket-transfer";
import { TicketStore } from "./ticket-store";
import { ApiError } from "./ticket-types";
import { authorizeWrite, registeredMachine, type Identity } from "./token-auth";

declare global {
  namespace Cloudflare {
    interface Env {
      ASSETS: Fetcher;
      TICKETS: DurableObjectNamespace<ProjectTickets>;
    }

    interface GlobalProps {
      mainModule: typeof import("./index");
      durableNamespaces: "ProjectTickets";
    }
  }
}

function apiPath(pathname: string): RegExpExecArray | null {
  return /^\/([a-z0-9][a-z0-9-]*)\/api(\/.*)$/.exec(pathname);
}

export class ProjectTickets extends DurableObject<Cloudflare.Env> {
  readonly store = new TicketStore(this.ctx.storage);

  private readonly feed = new ChangeFeed(this.ctx, this.store);
  private readonly deadlines = new ProjectDeadlines(this.store);

  override webSocketMessage(socket: WebSocket, message: string | ArrayBuffer): void {
    this.feed.subscribe(socket, message);
  }

  private state(usage: ProjectUsage) {
    return {...cutoverState(this.store.sql), seq: Number(this.store.meta("seq") ?? 0), ...usage.state()};
  }

  private get(project: string, path: string, request: Request, query: URLSearchParams, usage: ProjectUsage, now: number) {
    const sql = this.store.sql;
    if (path === "/feed") return this.feed.upgrade(request);
    if (path === "/graph") return this.store.graph(request.headers.get("If-None-Match"), query);
    if (path === "/tickets") return Response.json(this.store.list(project, query));
    if (path === "/state") return Response.json(this.state(usage));
    if (path === "/events") return Response.json(events(sql, query));
    if (path === "/leases") return Response.json(leases(sql, now));
    if (path === "/export") return exportTickets(sql, query.get("after"));
    const ticket = /^\/tickets\/([^/]+)$/.exec(path)?.[1];
    if (ticket) return Response.json(this.store.show(project, decodeURIComponent(ticket), query));
    return Response.json({error: "Unknown route"}, {status: 404});
  }

  private write(project: string, path: string, lease: string | undefined, identity: Identity, body: unknown, now: number, usage: ProjectUsage) {
    usage.check(0);
    if (path === "/batch") {
      const previous = this.store.tickets.seq;
      let change: FeedChange | undefined;
      const reply = this.store.batch(project, identity.actor, body, (rows, changes) => {
        if (changes.seq !== previous) change = {previous, seq: changes.seq, tickets: [...changes.changed]};
        this.record(usage, rows, change);
      });
      if (change) this.feed.publish(change);
      const armDeadlines = change?.tickets?.some((id) => {
        const ticket = this.store.tickets.live.get(id);
        return ticket?.status === "open" && ticket.defer_until && Date.parse(ticket.defer_until) >= now;
      });
      return {body: reply, status: 200, armDeadlines};
    }

    const write = new ProjectWrite(this.store.sql, now);
    const previous = write.seq;
    let change: FeedChange | undefined;
    const result = this.ctx.storage.transactionSync(() => {
      const reply = this.mutate(project, path, lease, identity, body, write);
      if (write.seq !== previous) {
        change = {previous, seq: write.seq, tickets: write.tickets, leases: write.leases, events: write.events};
      }
      this.record(usage, write.rowsWritten, change);
      return reply;
    });
    if (path === "/import" && change) this.store.invalidateIndex();
    else this.store.advanceSequence(write.seq);
    if (change) this.feed.publish(change);
    const armDeadlines = change && (lease !== undefined || path === "/import");
    return {...(result ?? {body: this.state(usage)}), armDeadlines};
  }

  private record(usage: ProjectUsage, rows: number, change?: FeedChange): void {
    const total = rows + (change ? this.feed.record(change) : 0);
    usage.check(total);
    usage.record(total);
  }

  private mutate(project: string, path: string, lease: string | undefined, identity: Identity, body: unknown, write: ProjectWrite):
    {body: object; status?: number} | undefined {
    if (path === "/import") return {body: importTickets(write, project, body)};
    if (path === "/events") return {body: write.event(identity, body)};
    if (lease !== undefined) return write.updateLease(decodeURIComponent(lease), identity.machine, body);
    write.cutover(body);
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const [, project, path] = apiPath(url.pathname) ?? [];
    if (!project || project !== this.ctx.id.name) return Response.json({error: "Unknown route"}, {status: 404});

    const lease = /^\/leases\/(.+)$/.exec(path)?.[1];
    let accounted = false;
    try {
      if (request.method === "POST" && (lease !== undefined || ["/batch", "/state", "/events", "/import"].includes(path))) {
        const identity = await authorizeWrite(request, (hash) => registeredMachine(this.store.meta("token_hashes"), hash));
        const body = await request.json();
        if (path === "/events") {
          // Arm retention before writing the first event so an eviction cannot
          // strand events in a project that then receives no more requests.
          await this.scheduleAlarm();
        }
        const now = Date.now();
        const result = this.write(project, path, lease, identity, body, now, new ProjectUsage(this.store.sql, now));
        accounted = true;
        if (result.armDeadlines) {
          const at = Date.now();
          const {next, tickets, leases} = this.deadlines.scan(at);
          await this.scheduleAlarm(tickets.length || leases.length ? at : next);
        }
        return Response.json(result.body, {status: result.status ?? 200});
      }
      const now = Date.now();
      const usage = new ProjectUsage(this.store.sql, now);
      usage.record();
      accounted = true;
      if (request.method === "GET") return this.get(project, path, request, url.searchParams, usage, now);
      return Response.json({error: "Unknown route"}, {status: 404});
    } catch (error) {
      if (!accounted) new ProjectUsage(this.store.sql, Date.now()).record();
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
    const current = await this.ctx.storage.getAlarm();
    if (current === null || next < current) await this.ctx.storage.setAlarm(next);
  }

  override async alarm(): Promise<void> {
    const now = Date.now();
    const usage = new ProjectUsage(this.store.sql, now);
    const {tickets, leases, next} = this.deadlines.scan(now);
    const write = new ProjectWrite(this.store.sql, now);
    const day = usage.state().day;
    let change: FeedChange | undefined;
    try {
      this.ctx.storage.transactionSync(() => {
        usage.check(0);
        if (this.store.meta("last_retention_day") !== day) {
          write.pruneEvents();
          write.write("INSERT OR REPLACE INTO meta VALUES ('last_retention_day', ?)", day);
        }
        if (tickets.length || leases.length) {
          const previous = write.seq;
          change = {previous, seq: write.advance(), tickets, leases};
          write.rowsWritten += this.deadlines.acknowledge(now);
        }
        this.record(usage, write.rowsWritten, change);
      });
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 429)) throw error;
      usage.record();
      await this.scheduleAlarm();
      return;
    }
    if (change) {
      this.store.advanceSequence(change.seq);
      this.feed.publish(change);
    }
    await this.scheduleAlarm(next);
  }
}

export default {
  fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (request.method === "GET" && pathname === "/version") {
      return new Response(version);
    }
    const project = apiPath(pathname)?.[1];
    if (project) return env.TICKETS.getByName(project).fetch(request);
    return env.ASSETS.fetch(request);
  },
} satisfies ExportedHandler<Cloudflare.Env>;
