let seq = 0;
const requests = [];
const closedProjects = new Set();

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const [, project, api, route, ...parts] = url.pathname.split("/");
    if (api !== "api") return new Response(null, { status: 404 });
    const target = parts.map(decodeURIComponent).join("/");
    const args = [...url.searchParams];
    if (project === "trace") return Response.json(requests.splice(0));
    if (project === "quota") return Response.json({ limit: "rows_written" }, { status: 429 });
    const body = request.method === "POST" ? await request.json() : null;
    const actor = request.headers.get("X-Actor");
    const auth = request.headers.get("Authorization");
    const etag = request.headers.get("If-None-Match");
    const trace = { project, route, target, args, actor, auth, etag, body };
    requests.push(trace);
    if (body !== null && (auth !== "Bearer simulator-token" || !actor)) {
      return Response.json({ error: "auth required" }, { status: 401 });
    }
    if (route === "feed") {
      const { 0: client, 1: server } = new WebSocketPair();
      server.accept();
      server.addEventListener("message", ({ data }) => {
        const { since } = JSON.parse(data);
        if (since === 99) return;
        closedProjects.add(project);
        server.send(JSON.stringify({
          seq: since + 1, tickets: ["k7mq"], leases: ["vizier/epic/one"], events: [], cutover: "before",
        }));
      });
      return new Response(null, { status: 101, webSocket: client });
    }
    if (project === "unsafe" || project === "retry") {
      const attempts = requests.filter((entry) => entry.project === project);
      if (attempts.length === 1) return new Response(null, { status: 503 });
    }
    if (route === "state") return Response.json({ cutover: body?.cutover ?? "before", seq });
    if (route === "graph") {
      if (etag === '"42"') return new Response(null, { status: 304 });
      return Response.json({ tickets: [], edges: [], seq });
    }
    if (route === "tickets" && target) {
      if (target === "missing") return Response.json({ error: "unknown ticket" }, { status: 404 });
      return Response.json({ id: "k7mq", title: "Simulator ticket", status: closedProjects.has(project) ? "closed" : "open", trace });
    }
    if (route === "tickets") {
      if (project === "wait" && !closedProjects.has(project)) return Response.json([]);
      return Response.json([{ id: "k7mq", trace }]);
    }
    if (route === "batch") {
      if (body.ops[0]?.id === "conflict") {
        return Response.json({ failed: 0, current: { id: "conflict" } }, { status: 409 });
      }
      return Response.json({ seq: ++seq, results: body.ops.map(() => ({ id: "k7mq" })), trace });
    }
    if (route === "export") {
      const pages = new Map([[null, { id: "aaaa" }], ["aaaa", { ticket: { id: "bbbb" } }]]);
      const page = pages.get(url.searchParams.get("after"));
      return new Response(page ? `${JSON.stringify(page)}\n` : "");
    }
    return Response.json(trace);
  },
};
