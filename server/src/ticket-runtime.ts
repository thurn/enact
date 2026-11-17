export type SqlValue = string | number | null | ArrayBuffer;
export type SqlRow = Record<string, SqlValue>;

export interface TicketSql {
  exec<T extends SqlRow = SqlRow>(query: string, ...values: SqlValue[]): Iterable<T> & {
    toArray(): T[];
    one(): T;
    readonly rowsWritten: number;
  };
}

export interface TicketStorage {
  sql: TicketSql;
  transactionSync<T>(operation: () => T): T;
}

export interface FeedSocket {
  send(message: string): void;
  close(code: number, reason: string): void;
  serializeAttachment(sequence: number): void;
  deserializeAttachment(): unknown;
}

export interface FeedTransport {
  sockets(): Iterable<FeedSocket>;
  upgrade(request: Request): Response;
  waitUntil(promise: Promise<void>): void;
}
