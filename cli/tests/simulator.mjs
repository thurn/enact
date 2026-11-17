// Exercise the real HTTP and WebSocket stack in Wrangler's local runtime.
import { unstable_dev } from "../../server/node_modules/wrangler/wrangler-dist/cli.js";
import { fileURLToPath } from "node:url";

const [mode, ...projects] = process.argv.slice(2);
const coordination = mode === "--coordination";
const config = coordination ? fileURLToPath(new URL("../../server/wrangler.toml", import.meta.url)) : undefined;
const entry = coordination ? "coordination-worker.mjs" : "simulator-worker.mjs";
const vars = coordination ? { TICKET_REPOSITORIES: Object.fromEntries(projects.map(project => [project, ""])) } : undefined;
if (coordination) process.chdir(fileURLToPath(new URL("../../server/", import.meta.url)));
const worker = await unstable_dev(
  fileURLToPath(new URL(entry, import.meta.url)),
  { config, vars, persist: false, ip: "127.0.0.1", port: 0, local: true, logLevel: "none", experimental: { disableExperimentalWarning: true } },
);
console.log(`http://${worker.address}:${worker.port}`);
process.stdin.resume();
process.stdin.on("end", () => worker.stop());
