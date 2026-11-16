import worker, {ProjectTickets as Tickets} from "../src/index";

export class ProjectTickets extends Tickets {
  override async fetch(request: Request): Promise<Response> {
    if (new URL(request.url).pathname === "/by/api/test-token" && request.method === "POST") {
      const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("dashboard-test-token"));
      const hash = [...new Uint8Array(bytes)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
      this.store.sql.exec("INSERT INTO meta VALUES ('token_hashes', ?)", JSON.stringify({[hash]: "test-mac"}));
      return new Response(null, {status: 204});
    }
    return super.fetch(request);
  }
}

export default worker;
