import { DurableObject } from "cloudflare:workers";
import { version } from "../package.json";
import { dashboardRoute } from "./dashboard";
import { TicketProject, apiPath } from "./project";
import { ProjectUsage, type RequestReservation } from "./project-usage";

declare global {
  namespace Cloudflare {
    interface Env {
      ASSETS: Fetcher;
      TICKETS: DurableObjectNamespace<ProjectTickets>;
      TICKET_MIGRATION_TOKEN?: string;
      TICKET_REPOSITORIES: Record<string, string>;
    }

    interface GlobalProps {
      mainModule: typeof import("./index");
      durableNamespaces: "ProjectTickets";
    }
  }
}

const feedTag = "change-feed";

export class ProjectTickets extends DurableObject<Cloudflare.Env> {
  private readonly usageRequests: RequestReservation = {day: "", used: 0, ceiling: 0};
  private readonly project = new TicketProject(this.ctx.id.name!, this.env.TICKET_REPOSITORIES?.[this.ctx.id.name!], {
    storage: this.ctx.storage,
    feed: {
      sockets: () => this.ctx.getWebSockets(feedTag),
      waitUntil: (promise) => this.ctx.waitUntil(promise),
      upgrade: (request) => {
        if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
          return Response.json({error: "Expected a WebSocket upgrade"}, {status: 400});
        }
        const {0: client, 1: server} = new WebSocketPair();
        this.ctx.acceptWebSocket(server, [feedTag]);
        return new Response(null, {status: 101, webSocket: client});
      },
    },
    usage: (now) => new ProjectUsage(this.ctx.storage.sql, now, this.usageRequests),
    getAlarm: () => this.ctx.storage.getAlarm(),
    setAlarm: (at) => this.ctx.storage.setAlarm(at),
    migrationToken: this.env.TICKET_MIGRATION_TOKEN,
  });

  get store() { return this.project.store; }

  override webSocketMessage(socket: WebSocket, message: string | ArrayBuffer): void {
    if (this.project.writable()) this.project.feed.subscribe(socket, message);
    else socket.close(1012, "Ticket project frozen");
  }

  override fetch(request: Request): Promise<Response> { return this.project.fetch(request); }
  override alarm(): Promise<void> { return this.project.alarm(); }
}

export default {
  fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (request.method === "GET" && pathname === "/version") {
      return new Response(version);
    }
    const project = apiPath(pathname)?.[1]
      ?? ((request.method === "GET" || request.method === "HEAD") ? dashboardRoute(pathname)?.project : undefined);
    if (!project) return env.ASSETS.fetch(request);
    if (!Object.hasOwn(env.TICKET_REPOSITORIES ?? {}, project)) {
      return Response.json({error: "Unknown route"}, {status: 404});
    }
    return env.TICKETS.getByName(project).fetch(request);
  },
} satisfies ExportedHandler<Cloudflare.Env>;
