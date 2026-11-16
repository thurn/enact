import * as c from "../cirrus/components";
import {ProjectUsage} from "../project-usage";
import {resolveId} from "../ticket-batch";
import {ancestors, type BatchChanges} from "../ticket-index";
import type {TicketRow} from "../ticket-types";
import {brand, ticketUrl} from "./common";
import {homeData, progress, ticketData, type Progress} from "./data";
import {home} from "./home";
import {ProjectStatus} from "./status";
import {ticketPage} from "./ticket";

export function dashboardRoute(pathname: string) {
  const match = /^\/([a-z0-9][a-z0-9-]*)(?:\/(?:(tickets|beads)\/([^/]+)\/?)?)?$/.exec(pathname);
  if (!match) return null;
  const [, project, tail, ticket] = match;
  return {project, ticket, legacy: tail === "beads"};
}

type Page = {
  seq: string;
  fingerprint: string;
  options: c.PageOptions;
  regions: string;
  full?: {body: string; etag: Promise<string>};
};
const encoder = new TextEncoder();

async function etag(body: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(body));
  return `W/"${[...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("")}"`;
}

export class Dashboard {
  private home?: Page;
  private readonly status: ProjectStatus;
  private readonly pages = new Map<string, Page>();
  private readonly counts = new Map<string, Progress | null>();

  constructor(private readonly sql: SqlStorage) {
    this.status = new ProjectStatus(sql);
  }

  async fetch(request: Request, route: NonNullable<ReturnType<typeof dashboardRoute>>, now = Date.now(), usage = new ProjectUsage(this.sql, now)): Promise<Response> {
    const {project, ticket, legacy} = route;
    const url = new URL(request.url);
    const root = `/${project}/`;
    const fragment = url.searchParams.get("fragment");
    const head = request.method === "HEAD";
    const seq = this.sql.exec<{value: string}>("SELECT value FROM meta WHERE key = 'seq'").toArray()[0]?.value ?? "0";
    const readProgress = (ids: Iterable<string>) => this.readProgress(ids);
    if (!ticket) {
      if (url.pathname !== root) return this.redirect(url, root);
      if (fragment === "status") return this.fragment(head, seq, this.status.render(root, now, usage.state()));
      this.home = this.page(this.home, seq, () => homeData(this.sql, readProgress), (data) => home(root, data, seq));
      if (fragment === "all") {
        const status = this.status.render(root, now, usage.state());
        return this.fragment(head, seq, this.regions(this.home) + `<template id="c-status-fragment">${status}</template>`);
      }
      return this.response(request, this.home);
    }

    const decoded = decodeURIComponent(ticket);
    const name = legacy ? decoded.replace(/\.html$/, "") : decoded;
    let page = this.pages.get(name);
    // A warm canonical request needs only the revision query. After a write
    // re-resolve aliases, which imports may have added along with ticket rows.
    if (legacy || page?.seq !== seq || url.pathname !== ticketUrl(root, name)) {
      const id = resolveId(this.sql, project, name);
      const row = this.sql.exec<TicketRow>("SELECT * FROM tickets WHERE id = ?", id).toArray()[0];
      if (!row) {
        const body = c.title("Not found", "", "No ticket has this address.")
          + `<p>${c.link(root, "Back to tickets", null, "c-muted")}</p>`;
        return new Response(head ? null : c.page({
          title: `Not found · ${brand(root)}`, body, root, assetRoot: "/",
          crumbs: [["Tickets", root]], brand: brand(root),
        }), {status: 404, headers: {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}});
      }
      const canonical = ticketUrl(root, id);
      if (url.pathname !== canonical) return this.redirect(url, canonical);
      page = this.page(page, seq, () => ticketData(this.sql, project, row, readProgress), (data) => ticketPage(root, project, data, seq));
    }
    this.pages.delete(name);
    this.pages.set(name, page);
    // Bound the memory retained across requests on a long-lived project.
    if (this.pages.size > 32) this.pages.delete(this.pages.keys().next().value!);
    return fragment === "all" ? this.fragment(head, seq, this.regions(page)) : this.response(request, page);
  }

  invalidateProgress(): void {
    this.counts.clear();
  }

  applyTicketChanges(changes: BatchChanges, parentOf: (id: string) => string | null): void {
    if (!this.counts.size) return;
    for (const [id, ticket] of changes.tickets) {
      const before = changes.originals.get(id);
      if (before && before.parent !== ticket.parent) {
        // Moving a ticket moves all of its descendants between ancestor groups.
        this.invalidateProgress();
        return;
      }
      if (before?.status === ticket.status) continue;
      for (const parent of ancestors(id, parentOf)) {
        const cached = this.counts.get(parent);
        if (cached === undefined) continue;
        const count = cached ?? {id: parent, closed: 0, working: 0, total: 0};
        count.total += Number(!before);
        count.closed += Number(ticket.status === "closed") - Number(before?.status === "closed");
        count.working += Number(ticket.status === "in_progress") - Number(before?.status === "in_progress");
        this.counts.set(parent, count);
      }
    }
  }

  private readProgress(ids: Iterable<string>): Progress[] {
    // A project's archive can outlive this object's in-memory graph.
    if (this.counts.size >= 8192) this.invalidateProgress();
    const all = [...ids];
    const missing = all.filter((id) => !this.counts.has(id));
    if (missing.length) {
      for (const id of missing) this.counts.set(id, null);
      for (const count of progress(this.sql, missing)) this.counts.set(count.id, count);
    }
    return all.flatMap((id) => this.counts.get(id) ?? []);
  }

  private page<Data>(cached: Page | undefined, seq: string, read: () => Data, render: (data: Data) => c.PageOptions): Page {
    if (cached?.seq === seq) return cached;
    const data = read();
    const fingerprint = JSON.stringify(data);
    if (cached?.fingerprint === fingerprint) return {...cached, seq};
    const options = render(data);
    return {seq, fingerprint, options: {...options, body: ""}, regions: c.pageRegions(options)};
  }

  private regions(page: Page): string {
    return `<title>${c.esc(page.options.title)}</title>${page.regions}`;
  }

  private fragment(head: boolean, seq: string, body: string): Response {
    return new Response(head ? null : body, {headers: {
      "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Cirrus-Sequence": seq,
    }});
  }

  private async response(request: Request, page: Page): Promise<Response> {
    // Live updates only need the regions. Build and hash the full document
    // once a browser asks for it through the navigation path.
    if (!page.full) {
      const body = c.page(page.options, page.regions);
      page.full = {body, etag: etag(body)};
    }
    const {body, etag: tagPromise} = page.full;
    const tag = await tagPromise;
    const headers = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache", ETag: tag};
    const unchanged = request.headers.get("If-None-Match")?.split(",").some((value) => {
      const candidate = value.trim();
      return candidate === "*" || candidate.replace(/^W\//, "") === tag.slice(2);
    });
    return new Response(unchanged || request.method === "HEAD" ? null : body, {
      status: unchanged ? 304 : 200, headers,
    });
  }

  private redirect(url: URL, pathname: string): Response {
    url.pathname = pathname;
    return Response.redirect(url.href, 308);
  }
}
