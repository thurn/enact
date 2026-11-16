// Provision only the test token here. Replay uses the production API, storage
// and feed through Wrangler's HTTP server.
import worker, {ProjectTickets as Tickets} from "../../server/src/index";

export class ProjectTickets extends Tickets {
  override async fetch(request: Request): Promise<Response> {
    if (new URL(request.url).pathname === "/test-token") {
      const token = await request.text();
      const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token)))]
        .map((byte) => byte.toString(16).padStart(2, "0")).join("");
      this.store.sql.exec("INSERT INTO meta VALUES ('token_hashes', ?)", JSON.stringify({[hash]: "shim-test"}));
      return new Response(null, {status: 204});
    }
    return super.fetch(request);
  }
}

export default {
  async fetch(request: Request, env: Cloudflare.Env) {
    const project = /^\/test-token\/([a-z]+)$/.exec(new URL(request.url).pathname)?.[1];
    if (project) return env.TICKETS.getByName(project).fetch(new Request("http://test/test-token", {method: "POST", body: await request.text()}));
    return worker.fetch(request, env);
  },
};
