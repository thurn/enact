import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../../server/package.json", import.meta.url));
const { build } = require("esbuild");
const { Miniflare, convertV4MiniflareOptions } = require("miniflare");
const { outputFiles: [bundle] } = await build({
  entryPoints: [fileURLToPath(new URL("shim-worker.ts", import.meta.url))],
  bundle: true,
  format: "esm",
  external: ["cloudflare:workers"],
  loader: { ".sql": "text" },
  write: false,
});

// Wrangler's dev proxy can stop answering on Windows while workerd still
// serves the Worker. Bind the fixtures directly to Miniflare's HTTP listener.
const worker = new Miniflare(convertV4MiniflareOptions({
  host: "127.0.0.1",
  port: 0,
  modules: true,
  script: bundle.text,
  compatibilityDate: "2026-11-16",
  // The fixture provisions tokens locally for these projects.
  bindings: { TICKET_REPOSITORIES: { by: "", en: "", gate: "", migration: "", snooze: "" } },
  durableObjects: { TICKETS: { className: "ProjectTickets", useSQLite: true } },
}));
console.log((await worker.ready).origin);
process.stdin.resume();
process.stdin.on("end", () => worker.dispose());
