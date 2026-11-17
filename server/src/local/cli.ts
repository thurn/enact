import {realpathSync} from "node:fs";
import {readFile} from "node:fs/promises";
import {resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {backupProjects, verifyRecentBackup, verifySnapshot} from "./backup";
import {startServer, validateConfig, type Config} from "./http";

export {backupProjects, startServer, verifyRecentBackup, verifySnapshot};

async function readConfig(file: string): Promise<Config> {
  const config = JSON.parse(await readFile(file, "utf8"));
  validateConfig(config);
  return config;
}

async function main(args: string[]) {
  process.umask(0o077);
  const [command, ...parameters] = args;
  if (command === "serve" && parameters.length === 1) {
    const server = await startServer(await readConfig(parameters[0]));
    console.log(server.url);
    const stop = () => { void server.close().catch(error => {
      console.error(error);
      process.exitCode = 1;
    }); };
    process.once("SIGINT", stop);
    process.once("SIGTERM", stop);
    return;
  }
  if (command === "backup" && parameters.length === 2) {
    return backupProjects(await readConfig(parameters[0]), resolve(parameters[1]));
  }
  if (command === "verify-nas" && parameters.length === 2) {
    return verifyRecentBackup(await readConfig(parameters[0]), resolve(parameters[1]));
  }
  if (command === "verify-backup" && parameters.length === 1) return verifySnapshot(resolve(parameters[0]));
  throw new Error(`Usage:
  tickets serve CONFIG
  tickets backup CONFIG SNAPSHOT_DIRECTORY
  tickets verify-nas CONFIG NAS_DIRECTORY
  tickets verify-backup SNAPSHOT`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === realpathSync(process.argv[1])) {
  void main(process.argv.slice(2)).then(result => {
    if (result) console.log(JSON.stringify(result));
  }).catch(error => {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  });
}
