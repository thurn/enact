// Exercise the real HTTP and WebSocket stack in Wrangler's local runtime.
import { unstable_dev } from "../../server/node_modules/wrangler/wrangler-dist/cli.js";
import { fileURLToPath } from "node:url";

const worker = await unstable_dev(
  fileURLToPath(new URL("simulator-worker.mjs", import.meta.url)),
  { persist: false, ip: "127.0.0.1", port: 0, local: true, logLevel: "none", experimental: { disableExperimentalWarning: true } },
);
console.log(`http://${worker.address}:${worker.port}`);
process.stdin.resume();
process.stdin.on("end", () => worker.stop());
