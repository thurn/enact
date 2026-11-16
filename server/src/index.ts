import { DurableObject } from "cloudflare:workers";
import { version } from "../package.json";
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

  // GitHub refresh and machine identification will populate and use this same
  // persisted set. A project with no accepted hashes rejects all writes.
  private async authorize(request: Request): Promise<string> {
    const token = /^Bearer (\S+)$/i.exec(request.headers.get("Authorization") ?? "")?.[1];
    if (token) {
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
      const hash = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
      const accepted = JSON.parse(this.store.meta("token_hashes") ?? "[]") as string[];
      if (accepted.includes(hash)) return request.headers.get("X-Actor")?.trim() || bad("Missing X-Actor");
    }
    throw new ApiError(401, {error: "Missing or unknown token"});
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const match = apiPath(url.pathname);
    const [, project, path] = match ?? [];
    try {
      if (match && project === this.ctx.id.name) {
        if (request.method === "GET") {
          if (path === "/graph") return this.store.graph(request.headers.get("If-None-Match"), url.searchParams);
          if (path === "/tickets") return Response.json(this.store.list(project, url.searchParams));
          const ticket = /^\/tickets\/([^/]+)$/.exec(path)?.[1];
          if (ticket) return Response.json(this.store.show(project, decodeURIComponent(ticket), url.searchParams));
        }
        if (request.method === "POST" && path === "/batch") {
          const actor = await this.authorize(request);
          const body = await request.json();
          return Response.json(this.store.batch(project, actor, body));
        }
      }
      return Response.json({error: "Unknown route"}, {status: 404});
    } catch (error) {
      if (error instanceof ApiError) return Response.json(error.body, {status: error.status});
      if (error instanceof SyntaxError || error instanceof URIError) {
        return Response.json({error: "Malformed request"}, {status: 400});
      }
      throw error;
    }
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
