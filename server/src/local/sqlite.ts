import {DatabaseSync, type StatementSync} from "node:sqlite";
import type {SqlRow, SqlValue, TicketStorage} from "../ticket-runtime";

const maxStatements = 256;
type Prepared = {statement: StatementSync; returnsRows: boolean};

export class SqliteStorage implements TicketStorage {
  readonly database: DatabaseSync;
  private readonly statements = new Map<string, Prepared>();

  constructor(path: string, mode: "service" | "offline" = "service") {
    this.database = new DatabaseSync(path, {timeout: 1000, enableDoubleQuotedStringLiterals: false});
    try {
      const version = String(this.database.prepare("SELECT sqlite_version() AS version").get()?.version);
      const [major, minor, patch] = version.split(".").map(Number);
      if (major < 3 || major === 3 && (minor < 51 || minor === 51 && patch < 3)) {
        throw new Error(`SQLite 3.51.3 or later is required, found ${version}`);
      }
      this.database.exec(`PRAGMA journal_mode = ${mode === "service" ? "WAL" : "DELETE"}; PRAGMA synchronous = FULL;`);
    } catch (error) {
      this.database.close();
      throw error;
    }
  }

  readonly sql = {
    exec: <T extends SqlRow>(query: string, ...values: SqlValue[]) => {
      let rows: T[] = [];
      let rowsWritten = 0;
      if (!values.length && /^\s*(?:CREATE|ALTER)\b/i.test(query)) {
        this.database.exec(query);
        // A schema change can invalidate cached column metadata.
        this.statements.clear();
      } else {
        const {statement, returnsRows} = this.prepare(query);
        const bindings = values.map(value => value instanceof ArrayBuffer ? new Uint8Array(value) : value);
        if (returnsRows) rows = statement.all(...bindings) as T[];
        else rowsWritten = Number(statement.run(...bindings).changes);
      }
      return {
        [Symbol.iterator]: () => rows[Symbol.iterator](),
        toArray: () => rows,
        one: () => {
          if (rows.length !== 1) throw new Error(`Expected one row, got ${rows.length}`);
          return rows[0];
        },
        rowsWritten,
      };
    },
  };

  private prepare(query: string): Prepared {
    const cached = this.statements.get(query);
    if (cached) return cached;
    const statement = this.database.prepare(query);
    if (this.statements.size >= maxStatements) this.statements.delete(this.statements.keys().next().value!);
    const prepared = {statement, returnsRows: statement.columns().length !== 0};
    this.statements.set(query, prepared);
    return prepared;
  }

  transactionSync<T>(operation: () => T): T {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const result = operation();
      this.database.exec("COMMIT");
      return result;
    } catch (error) {
      if (this.database.isTransaction) this.database.exec("ROLLBACK");
      throw error;
    }
  }

  close(): void { this.database.close(); }
}
