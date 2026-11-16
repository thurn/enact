import { DurableObject } from "cloudflare:workers";
import { version } from "../package.json";
import { cutoverState, dayMilliseconds, events, leases, ProjectWrite, type Identity } from "./project-state";
import { ProjectUsage } from "./project-usage";
import { exportTickets, importTickets } from "./ticket-transfer";
import { TicketStore } from "./ticket-store";
import { ApiError, bad } from "./ticket-types";

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

  // GitHub refresh will populate this set with each token's registered machine.
  private async authorize(request: Request): Promise<Identity> {
    const token = /^Bearer (\S+)$/i.exec(request.headers.get("Authorization") ?? "")?.[1];
    if (token) {
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
      const hash = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
      const accepted = JSON.parse(this.store.meta("token_hashes") ?? "[]") as string[] | Record<string, string>;
      const actor = request.headers.get("X-Actor")?.trim();
      const machine = Array.isArray(accepted) ? (accepted.includes(hash) ? actor ?? "" : undefined) : accepted[hash];
      if (machine !== undefined) return {actor: actor || bad("Missing X-Actor"), machine};
    }
    throw new ApiError(401, {error: "Missing or unknown token"});
  }

  private state(usage: ProjectUsage) {
    return {...cutoverState(this.store.sql), seq: Number(this.store.meta("seq") ?? 0), ...usage.state()};
  }

  private get(project: string, path: string, request: Request, query: URLSearchParams, usage: ProjectUsage, now: number) {
    const sql = this.store.sql;
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
      return {body: this.store.batch(project, identity.actor, body, (rows) => {
        usage.check(rows);
        usage.record(rows);
      })};
    }

    const write = new ProjectWrite(this.store.sql, now);
    const result = this.ctx.storage.transactionSync(() => {
      const reply = this.mutate(project, path, lease, identity, body, write);
      usage.check(write.rowsWritten);
      usage.record(write.rowsWritten);
      return reply;
    });
    if (path === "/import" && write.rowsWritten) this.store.invalidateIndex();
    else this.store.advanceSequence(write.seq);
    return result ?? {body: this.state(usage)};
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
        const identity = await this.authorize(request);
        const body = await request.json();
        if (path === "/events") {
          // Arm retention before writing the first event so an eviction cannot
          // strand events in a project that then receives no more requests.
          await this.ctx.blockConcurrencyWhile(async () => {
            if (await this.ctx.storage.getAlarm() === null) await this.scheduleRetention();
          });
        }
        const now = Date.now();
        const result = this.write(project, path, lease, identity, body, now, new ProjectUsage(this.store.sql, now));
        accounted = true;
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

  private scheduleRetention() {
    const nextDay = (Math.floor(Date.now() / dayMilliseconds) + 1) * dayMilliseconds;
    return this.ctx.storage.setAlarm(nextDay);
  }

  override async alarm(): Promise<void> {
    const now = Date.now();
    const usage = new ProjectUsage(this.store.sql, now);
    try {
      this.ctx.storage.transactionSync(() => {
        usage.check(0);
        const write = new ProjectWrite(this.store.sql, now);
        write.pruneEvents();
        usage.check(write.rowsWritten);
        usage.record(write.rowsWritten);
      });
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 429)) throw error;
      usage.record();
    }
    await this.scheduleRetention();
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
