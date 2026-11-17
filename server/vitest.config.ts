import {readFile} from "node:fs/promises";
import {defineConfig} from "vitest/config";

export default defineConfig({
  plugins: [{
    name: "ticket-schema",
    async load(id) {
      if (id.endsWith(".sql")) return `export default ${JSON.stringify(await readFile(id, "utf8"))}`;
    },
  }],
});
