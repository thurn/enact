import worker, {ProjectTickets as Tickets} from "../../server/src/index.ts";

// Private routes on the local simulator let the integration test register
// fake machines and expire a dead holder without a wall-clock sleep.
export class ProjectTickets extends Tickets {
  async fetch(request) {
    const {pathname} = new URL(request.url);
    if (pathname.endsWith("/seed") && pathname.startsWith("/__coordination/")) {
      this.store.sql.exec("INSERT INTO meta VALUES ('token_hashes', ?)", JSON.stringify(await request.json()));
      return Response.json({});
    }
    if (pathname.endsWith("/expire") && pathname.startsWith("/__coordination/")) {
      const {name} = await request.json();
      this.store.sql.exec("UPDATE leases SET expires_at = ? WHERE name = ?", new Date(0).toISOString(), name);
      return Response.json({});
    }
    return super.fetch(request);
  }
}

export default {
  fetch(request, env) {
    const match = /^\/__coordination\/([^/]+)\/(?:seed|expire)$/.exec(new URL(request.url).pathname);
    if (match) return env.TICKETS.getByName(match[1]).fetch(request);
    return worker.fetch(request, env);
  },
};
