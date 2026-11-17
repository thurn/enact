import type {TicketSql} from "./ticket-runtime";
import {ApiError} from "./ticket-types";

const limits = {requests: 100_000, rows_written: 100_000};
const threshold = {requests: limits.requests * 0.9, rows_written: limits.rows_written * 0.9};

export type Counts = {day: string; requests: number; rows_written: number};

// Reserve request counts in groups so a run of reads does not write once per
// request. A restart treats the unspent part of its previous group as used.
const requestsPerReservation = 32;
export type RequestReservation = {day: string; used: number; ceiling: number};

export type UsageState = Counts & {limits: typeof limits; quota_enforced?: false};

export interface UsageMeter {
  check(rows: number, rowCeiling?: number): void;
  record(rows?: number, requests?: number): void;
  state(): UsageState;
}

export class ProjectUsage implements UsageMeter {
  private counts: Counts;

  constructor(
    private readonly sql: TicketSql,
    now: number,
    private readonly requests: RequestReservation = {day: "", used: 0, ceiling: 0},
  ) {
    const day = new Date(now).toISOString().slice(0, 10);
    const row = sql.exec<{value: string}>("SELECT value FROM meta WHERE key = 'daily_usage'").toArray()[0];
    const saved: Counts | undefined = row && JSON.parse(row.value);
    this.counts = saved?.day === day ? saved : {day, requests: 0, rows_written: 0};
    if (requests.day !== day || requests.ceiling !== this.counts.requests) {
      Object.assign(requests, {day, used: this.counts.requests, ceiling: this.counts.requests});
    }
  }

  check(rows: number, rowCeiling = threshold.rows_written): void {
    if (this.requests.used + 1 > threshold.requests) {
      throw new ApiError(429, {limit: "requests"});
    }
    // Updating the usage row is itself a SQLite write.
    if (this.counts.rows_written + rows + 1 > Math.min(rowCeiling, threshold.rows_written)) {
      throw new ApiError(429, {limit: "rows_written"});
    }
  }

  record(rows = 0, requests = 1): void {
    // Keep serving reads after the write cutoff without consuming its reserve.
    if (this.counts.rows_written >= threshold.rows_written) return;
    const used = this.requests.used + requests;
    if (rows === 0 && used <= this.requests.ceiling) {
      this.requests.used = used;
      return;
    }
    const ceiling = used > this.requests.ceiling
      ? used + requestsPerReservation - 1 : this.requests.ceiling;
    const next = {
      day: this.counts.day,
      requests: ceiling,
      rows_written: this.counts.rows_written + rows + 1,
    };
    this.sql.exec(
      "INSERT INTO meta VALUES ('daily_usage', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
      JSON.stringify(next),
    );
    this.counts = next;
    Object.assign(this.requests, {used, ceiling});
  }

  state() {
    return {...this.counts, requests: this.requests.used, limits};
  }
}
