import {ApiError} from "./ticket-types";

const limits = {requests: 100_000, rows_written: 100_000};
const threshold = {requests: limits.requests * 0.9, rows_written: limits.rows_written * 0.9};

type Counts = {day: string; requests: number; rows_written: number};

export class ProjectUsage {
  private counts: Counts;

  constructor(private readonly sql: SqlStorage, now: number) {
    const day = new Date(now).toISOString().slice(0, 10);
    const row = sql.exec<{value: string}>("SELECT value FROM meta WHERE key = 'daily_usage'").toArray()[0];
    const saved: Counts | undefined = row && JSON.parse(row.value);
    this.counts = saved?.day === day ? saved : {day, requests: 0, rows_written: 0};
  }

  check(rows: number): void {
    if (this.counts.requests + 1 > threshold.requests) {
      throw new ApiError(429, {limit: "requests"});
    }
    // Updating the usage row is itself a SQLite write.
    if (this.counts.rows_written + rows + 1 > threshold.rows_written) {
      throw new ApiError(429, {limit: "rows_written"});
    }
  }

  record(rows = 0): void {
    // Keep serving reads after the write cutoff without consuming its reserve.
    if (this.counts.rows_written >= threshold.rows_written) return;
    const next = {
      day: this.counts.day,
      requests: this.counts.requests + 1,
      rows_written: this.counts.rows_written + rows + 1,
    };
    this.sql.exec(
      "INSERT INTO meta VALUES ('daily_usage', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
      JSON.stringify(next),
    );
    this.counts = next;
  }

  state() {
    return {...this.counts, limits};
  }
}
