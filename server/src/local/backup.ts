import {createHash} from "node:crypto";
import {createReadStream} from "node:fs";
import {mkdir, mkdtemp, readFile, readdir, rename, rm, writeFile} from "node:fs/promises";
import {join} from "node:path";
import {backup, DatabaseSync} from "node:sqlite";
import {projectFile, type Config} from "./http";
import {checkDatabase, SqliteStorage} from "./sqlite";

const snapshotPattern = /^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.\d{3}Z$/;
const hour = 60 * 60 * 1000;
type Manifest = {
  timestamp: string;
  projects: Record<string, {seq: number; sha256: string}>;
};

async function sha256(file: string) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest("hex");
}

function sequence(db: SqliteStorage): number {
  const seq = Number(db.sql.exec<{value: string}>("SELECT value FROM meta WHERE key = 'seq'").toArray()[0]?.value ?? 0);
  if (!Number.isSafeInteger(seq) || seq < 0) throw new Error("Invalid snapshot sequence");
  return seq;
}

async function snapshots(directory: string) {
  return (await readdir(directory, {withFileTypes: true}))
    .filter(entry => entry.isDirectory() && snapshotPattern.test(entry.name)).map(entry => entry.name).sort().reverse();
}

function snapshotTime(name: string) {
  return Date.parse(name.slice(0, 10) + "T" + name.slice(11).replaceAll("-", ":"));
}

export async function backupProjects(config: Config, directory: string) {
  await mkdir(directory, {recursive: true, mode: 0o700});
  const time = new Date();
  const manifest: Manifest = {timestamp: time.toISOString(), projects: Object.create(null)};
  const name = manifest.timestamp.replaceAll(":", "-");
  const staging = await mkdtemp(join(directory, ".snapshot-"));
  try {
    for (const project of Object.keys(config.projects)) {
      const db = new DatabaseSync(projectFile(config.data_dir, project), {readOnly: true, timeout: 1000});
      const destination = projectFile(staging, project);
      try {
        await backup(db, destination, {rate: 128});
      } finally {
        db.close();
      }
      const snapshot = new SqliteStorage(destination, "offline");
      let seq: number;
      try {
        checkDatabase(snapshot);
        seq = sequence(snapshot);
      } finally {
        snapshot.close();
      }
      manifest.projects[project] = {seq, sha256: await sha256(destination)};
    }
    await writeFile(join(staging, "manifest.json"), JSON.stringify(manifest) + "\n", {mode: 0o600});
    await rename(staging, join(directory, name));
  } finally {
    await rm(staging, {recursive: true, force: true});
  }

  const days = new Set<string>();
  for (const [index, entry] of (await snapshots(directory)).entries()) {
    const day = entry.slice(0, 10);
    const daily = !days.has(day) && time.getTime() - snapshotTime(entry) < 30 * 24 * hour;
    days.add(day);
    if (index >= 48 && !daily) await rm(join(directory, entry), {recursive: true});
  }
  return {snapshot: join(directory, name), ...manifest};
}

export async function verifySnapshot(directory: string, expectedProjects?: string[]): Promise<Manifest> {
  const manifest = JSON.parse(await readFile(join(directory, "manifest.json"), "utf8")) as Manifest;
  const projects = Object.keys(manifest.projects ?? {});
  if (!Number.isFinite(Date.parse(manifest.timestamp)) || !projects.length
      || expectedProjects && expectedProjects.some(project => !projects.includes(project))) {
    throw new Error("Incomplete snapshot manifest");
  }
  for (const [project, expected] of Object.entries(manifest.projects)) {
    const file = projectFile(directory, project);
    if (await sha256(file) !== expected.sha256) throw new Error(`Backup checksum mismatch for ${project}`);
    const db = new DatabaseSync(file, {readOnly: true, timeout: 1000});
    try {
      const integrity = db.prepare("PRAGMA integrity_check").all();
      const seq = Number(db.prepare("SELECT value FROM meta WHERE key = 'seq'").get()?.value ?? 0);
      if (integrity.length !== 1 || integrity[0].integrity_check !== "ok" || seq !== expected.seq) {
        throw new Error(`Invalid backup for ${project}`);
      }
    } finally {
      db.close();
    }
  }
  return manifest;
}

export async function verifyRecentBackup(config: Config, directory: string) {
  const [name] = await snapshots(directory);
  if (!name) throw new Error("No completed snapshot on the NAS");
  const manifest = await verifySnapshot(join(directory, name), Object.keys(config.projects));
  if (Date.now() - Date.parse(manifest.timestamp) > 2 * hour) throw new Error("Latest verified NAS snapshot is over two hours old");
  return manifest;
}
