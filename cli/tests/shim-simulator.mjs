import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { unstable_dev } from "../../server/node_modules/wrangler/wrangler-dist/cli.js";

const config = resolve("wrangler.json");
const entry = fileURLToPath(new URL("shim-worker.ts", import.meta.url));
writeFileSync(config, JSON.stringify({
  name: "shim-replay",
  main: entry,
  compatibility_date: "2026-11-16",
  durable_objects: { bindings: [{ name: "TICKETS", class_name: "ProjectTickets" }] },
  migrations: [{ tag: "v1", new_sqlite_classes: ["ProjectTickets"] }],
  rules: [{ type: "Text", globs: ["**/*.sql"], fallthrough: true }],
}));

const worker = await unstable_dev(entry, {
  config, ip: "127.0.0.1", port: 0, local: true, logLevel: "none",
  experimental: { disableExperimentalWarning: true },
});
console.log(`http://${worker.address}:${worker.port}`);
process.stdin.resume();
process.stdin.on("end", () => worker.stop());
