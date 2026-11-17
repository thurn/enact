import {mkdtemp, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {startServer} from "../.local/tickets.mjs";

const projects = Object.fromEntries(process.argv.slice(2).map(name => [name, ""]));
const directory = await mkdtemp(join(tmpdir(), "enact-sqlite-test-"));
try {
  const server = await startServer({data_dir: directory, projects, port: 0}, {
    async fixture(request, mounted) {
      const match = /^\/__coordination\/([^/]+)\/(seed|expire)$/.exec(new URL(request.url).pathname);
      if (!match || request.method !== "POST") return;
      const {sql} = mounted.get(match[1]).storage;
      const body = await request.json();
      if (match[2] === "seed") sql.exec("INSERT INTO meta VALUES ('token_hashes', ?)", JSON.stringify(body));
      else sql.exec("UPDATE leases SET expires_at = ? WHERE name = ?", new Date(0).toISOString(), body.name);
      return Response.json({});
    },
  });
  console.log(server.url);
  process.stdin.resume();
  await new Promise(resolve => process.stdin.once("end", resolve));
  await server.close();
} finally {
  await rm(directory, {recursive: true, force: true});
}
