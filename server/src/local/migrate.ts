import {createHash} from "node:crypto";
import {existsSync} from "node:fs";
import {link, mkdir, unlink} from "node:fs/promises";
import {dirname} from "node:path";
import {ensureFeedJournal} from "../change-feed";
import {migrationPage, migrationTables, type Cursor, type MigrationTable} from "../ticket-migration";
import type {SqlRow} from "../ticket-runtime";
import {TicketStore} from "../ticket-store";
import {SqliteStorage} from "./sqlite";

const tables = Object.keys(migrationTables) as MigrationTable[];

type Receipt = {id: string; seq: number};
type Page = Receipt & {next: Cursor | null; rows: SqlRow[]};
type Progress = {
  name: MigrationTable;
  cursor: string;
  rows: number;
  digest: string;
  done: number;
};

function digestRows(start: string, rows: SqlRow[]): string {
  let digest = start;
  for (const row of rows) {
    digest = createHash("sha256").update(digest)
      .update(JSON.stringify(Object.keys(row).sort().map(key => [key, row[key]]))).digest("hex");
  }
  return digest;
}

function endpoint(base: string, project: string, id: string): URL {
  const url = new URL(base);
  if (url.origin !== base || url.protocol !== "https:" && !(url.protocol === "http:" && url.hostname === "127.0.0.1")) {
    throw new Error("Migration source must be an HTTPS origin or a loopback test server");
  }
  if (!/^[a-z0-9][a-z0-9-]*$/.test(project) || !/^[a-zA-Z0-9_-]{16,128}$/.test(id)) {
    throw new Error("Invalid project or migration ID");
  }
  url.pathname = `/${project}/api/_sqlite-migration`;
  url.searchParams.set("id", id);
  return url;
}

async function request<T>(url: URL, secret: string, method = "GET"): Promise<T> {
  const response = await fetch(url, {
    method, headers: {authorization: `Bearer ${secret}`}, signal: AbortSignal.timeout(30_000), redirect: "error",
  });
  if (!response.ok) throw new Error(`Migration request failed with HTTP ${response.status}`);
  return await response.json() as T;
}

export async function setSourceFreeze(base: string, project: string, id: string, secret: string, freeze: boolean) {
  const url = endpoint(base, project, id);
  url.searchParams.set("action", freeze ? "freeze" : "thaw");
  return request<Receipt & {frozen: boolean}>(url, secret, "POST");
}

export function checkDatabase(db: SqliteStorage, thorough = false) {
  const check = thorough ? "integrity_check" : "quick_check";
  const rows = db.sql.exec(`PRAGMA ${check}`).toArray();
  if (rows.length !== 1 || rows[0][check] !== "ok") {
    throw new Error(`SQLite ${check} failed: ${JSON.stringify(rows)}`);
  }
}

export async function migrate(base: string, project: string, id: string, secret: string, destination: string) {
  const endpointUrl = endpoint(base, project, id);
  const source = await request<Receipt & {tables: string[]}>(endpointUrl, secret);
  if (source.id !== id || !Number.isSafeInteger(source.seq) || source.seq < 0
      || JSON.stringify(source.tables) !== JSON.stringify(tables)) throw new Error("Unsupported source snapshot");
  if (existsSync(destination)) throw new Error("Destination database already exists");
  await mkdir(dirname(destination), {mode: 0o700, recursive: true});

  const temporary = `${destination}.${id}.partial`;
  const db = new SqliteStorage(temporary, "offline");
  const {sql} = db;
  const marker = JSON.stringify([base, project, id, source.seq]);
  const progress = () => sql.exec<Progress>("SELECT * FROM migration_progress").toArray();
  try {
    const store = new TicketStore(db);
    ensureFeedJournal(sql);
    db.transactionSync(() => {
      sql.exec("CREATE TABLE IF NOT EXISTS migration_source (marker TEXT PRIMARY KEY)");
      sql.exec(`CREATE TABLE IF NOT EXISTS migration_progress (
        name TEXT PRIMARY KEY, cursor TEXT NOT NULL, rows INTEGER NOT NULL, digest TEXT NOT NULL, done INTEGER NOT NULL
      )`);
      sql.exec("INSERT INTO migration_source SELECT ? WHERE NOT EXISTS (SELECT 1 FROM migration_source)", marker);
    });
    if (sql.exec<{marker: string}>("SELECT marker FROM migration_source").one().marker !== marker) {
      throw new Error("Partial database belongs to another source snapshot");
    }

    for (const table of tables) {
      const keys = migrationTables[table];
      const columns = sql.exec<{name: string}>(`PRAGMA table_info(${table})`).toArray().map(({name}) => name);
      const insert = `INSERT INTO ${table} (${columns.join(",")}) VALUES (${columns.map(() => "?").join(",")})`;
      let current = progress().find(entry => entry.name === table);
      const url = new URL(endpointUrl);
      url.searchParams.set("table", table);
      while (!current?.done) {
        const cursor = current?.cursor ?? "null";
        url.searchParams.set("after", cursor);
        const page = await request<Page>(url, secret);
        if (page.id !== id || page.seq !== source.seq || !Array.isArray(page.rows)
            || page.rows.length > 128 || page.next !== null &&
            (!page.rows.length || JSON.stringify(page.next) === cursor
              || JSON.stringify(page.next) !== JSON.stringify(keys.map(key => page.rows.at(-1)![key])))) {
          throw new Error(`Invalid or changed source page for ${table}`);
        }
        const next: Progress = {name: table, cursor: JSON.stringify(page.next),
          rows: (current?.rows ?? 0) + page.rows.length,
          digest: digestRows(current?.digest ?? "", page.rows), done: page.next === null ? 1 : 0};
        db.transactionSync(() => {
          for (const row of page.rows) {
            if (Object.keys(row).length !== columns.length
                || columns.some(column => !Object.hasOwn(row, column))) throw new Error(`Schema mismatch for ${table}`);
            sql.exec(insert, ...columns.map(column => row[column]));
          }
          sql.exec("INSERT OR REPLACE INTO migration_progress VALUES (?, ?, ?, ?, ?)",
            table, next.cursor, next.rows, next.digest, next.done);
        });
        current = next;
      }
    }

    const receipts = progress();
    for (const receipt of receipts) {
      let cursor: Cursor | null = null;
      let rows = 0;
      let digest = "";
      do {
        const page = migrationPage(sql, receipt.name, cursor);
        rows += page.rows.length;
        digest = digestRows(digest, page.rows);
        cursor = page.next;
      } while (cursor !== null);
      if (!receipt.done || rows !== receipt.rows || digest !== receipt.digest) {
        throw new Error(`Import verification failed for ${receipt.name}`);
      }
    }
    const confirmed = await request<Receipt>(endpointUrl, secret);
    if (confirmed.id !== id || confirmed.seq !== source.seq || Number(store.meta("seq") ?? 0) !== source.seq) {
      throw new Error("Source or imported sequence changed");
    }
    checkDatabase(db, true);
    // Leave progress in the staging file until publication succeeds. The service
    // only sees the published file, and startup has no migration dependencies.
    db.close();
    await link(temporary, destination);
    await unlink(temporary);
    return {...source, rows: Object.fromEntries(receipts.map(entry => [entry.name, entry.rows]))};
  } catch (error) {
    if (db.database.isOpen) db.close();
    throw error;
  }
}
