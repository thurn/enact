import type {SqlRow, TicketSql} from "./ticket-runtime";
import type {TicketStore} from "./ticket-store";

export const migrationKey = "sqlite_migration";
export const migrationTables = {
  meta: ["key"],
  tickets: ["id"],
  edges: ["src", "dst"],
  comments: ["ticket", "n"],
  history: ["ticket", "seq"],
  leases: ["name"],
  events: ["seq"],
  aliases: ["alias"],
  feed_changes: ["slot"],
} as const;

export type MigrationTable = keyof typeof migrationTables;
export type Cursor = (string | number)[];

export function migrationPage(sql: TicketSql, table: MigrationTable, after: Cursor | null) {
  const keys = migrationTables[table];
  const where = table === "meta"
    ? "WHERE key NOT IN ('sqlite_migration', 'daily_usage', 'token_fetched_at')"
    : "";
  const clause = after ? `${where ? " AND" : "WHERE"} (${keys.join(",")}) > (${keys.map(() => "?").join(",")})` : "";
  const rows = sql.exec(`SELECT * FROM ${table} ${where} ${clause} ORDER BY ${keys.join(",")} LIMIT 129`, ...(after ?? []));
  const page: SqlRow[] = [];
  let bytes = 0;
  for (const row of rows) {
    // Account conservatively for UTF-8, with space for JSON framing.
    bytes += JSON.stringify(row).length * 3;
    if (page.length && (bytes > 1024 * 1024 || page.length === 128)) {
      return {rows: page, next: keys.map(key => page.at(-1)![key]) as Cursor};
    }
    page.push(row);
  }
  return {rows: page, next: null};
}

async function authorized(header: string | null, secret: string) {
  const utf8 = new TextEncoder();
  const [actual, expected] = await Promise.all([header ?? "", `Bearer ${secret}`].map(async (value) =>
    new Uint8Array(await crypto.subtle.digest("SHA-256", utf8.encode(value)))));
  return actual.reduce((diff, byte, index) => diff | (byte ^ expected[index]), 0) === 0;
}

export async function migrationRequest(request: Request, secret: string | undefined, store: TicketStore,
  frozen: () => string | undefined, setFrozen: (fence: string | undefined) => Promise<void>): Promise<Response> {
  if (!secret) return Response.json({error: "Unknown route"}, {status: 404});
  if (!await authorized(request.headers.get("Authorization"), secret)) {
    return Response.json({error: "Unknown route"}, {status: 404});
  }

  const query = new URL(request.url).searchParams;
  const id = query.get("id");
  if (!id || !/^[a-zA-Z0-9_-]{16,128}$/.test(id)) {
    return Response.json({error: "Invalid migration ID"}, {status: 400});
  }
  if (request.method === "POST") {
    const action = query.get("action");
    const current = frozen();
    if (action === "freeze" && !current) await setFrozen(id);
    else if (action === "thaw" && current === id) await setFrozen(undefined);
    else if ((action !== "freeze" || current !== id) && (action !== "thaw" || current !== undefined)) {
      return Response.json({error: "Invalid migration transition"}, {status: 409});
    }
    return Response.json({id, frozen: frozen() === id, seq: Number(store.meta("seq") ?? 0)});
  }
  if (request.method !== "GET" || frozen() !== id) {
    return Response.json({error: "Project is not frozen under this migration ID"}, {status: 409});
  }
  const info = {id, seq: Number(store.meta("seq") ?? 0)};
  const table = query.get("table") as MigrationTable | null;
  if (table === null) return Response.json({...info, tables: Object.keys(migrationTables)});
  if (!Object.hasOwn(migrationTables, table)) return Response.json({error: "Unknown table"}, {status: 400});

  let after: Cursor | null = null;
  try {
    const cursor = JSON.parse(query.get("after") ?? "null");
    if (cursor !== null) {
      if (!Array.isArray(cursor) || cursor.length !== migrationTables[table].length
          || cursor.some(value => typeof value !== "string" && !Number.isSafeInteger(value))) {
        throw new Error("Invalid cursor");
      }
      after = cursor;
    }
  } catch {
    return Response.json({error: "Invalid cursor"}, {status: 400});
  }
  const page = migrationPage(store.sql, table, after);
  return Response.json({...info, ...page}, {headers: {"Cache-Control": "no-store"}});
}
