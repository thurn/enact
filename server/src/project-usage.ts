export type Counts = {day: string; requests: number; rows_written: number};
export type UsageState = Counts & {limits: {requests: number; rows_written: number}; quota_enforced: false};

export interface UsageMeter {
  record(rows?: number, requests?: number): void;
  state(): UsageState;
}

export function localUsage(): (now: number) => UsageMeter {
  let counts: Counts = {day: "", requests: 0, rows_written: 0};
  const unbounded = Number.MAX_SAFE_INTEGER;
  return (now) => {
    const day = new Date(now).toISOString().slice(0, 10);
    if (day !== counts.day) counts = {day, requests: 0, rows_written: 0};
    return {
      record(rows = 0, requests = 1) {
        counts.requests += requests;
        counts.rows_written += rows;
      },
      state: () => ({...counts, quota_enforced: false, limits: {requests: unbounded, rows_written: unbounded}}),
    };
  };
}
