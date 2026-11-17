import {mkdtemp, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {startServer} from "../src/local/http";

export async function startTestServer(projects = {en: "", by: ""}) {
  const data_dir = await mkdtemp(join(tmpdir(), "enact-tickets-test-"));
  const remove = () => rm(data_dir, {recursive: true, force: true});
  try {
    const server = await startServer({data_dir, projects, port: 0}, {
      assetRoot: new URL("../.cirrus/public/", import.meta.url),
    });
    return {
      url: server.url, data_dir,
      async close() {
        try {
          await server.close();
        } finally {
          await remove();
        }
      },
    };
  } catch (error) {
    await remove();
    throw error;
  }
}
