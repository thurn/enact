import {build} from "esbuild";
import {fileURLToPath} from "node:url";

await build({
  absWorkingDir: fileURLToPath(new URL("../", import.meta.url)),
  entryPoints: ["src/local/cli.ts"],
  bundle: true,
  platform: "node",
  format: "esm",
  packages: "external",
  target: "node24",
  loader: {".sql": "text"},
  outfile: ".local/tickets.mjs",
});
