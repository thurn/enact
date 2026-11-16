export const statuses = ["open", "in_progress", "blocked", "deferred", "closed"] as const;
export const ticketTypes = ["task", "epic", "bug", "feature", "chore", "decision", "spike", "story", "milestone"] as const;
export const edgeKinds = ["blocks", "conditional-blocks", "waits-for", "related", "discovered-from"] as const;

export type Ticket = {
  id: string;
  parent: string | null;
  next_child: number;
  title: string;
  body: string;
  acceptance: string;
  design: string;
  notes: string;
  type: typeof ticketTypes[number];
  status: typeof statuses[number];
  priority: number;
  assignee: string | null;
  last: 0 | 1;
  labels: string[];
  metadata: Record<string, unknown>;
  defer_until: string | null;
  close_reason: string | null;
  created_at: string;
  created_by: string;
  updated_at: string;
  closed_at: string | null;
  version: number;
};

export type TicketRow = Omit<Ticket, "labels" | "metadata"> & {labels: string; metadata: string};
export type Edge = {src: string; dst: string; kind: typeof edgeKinds[number]};
export type Blocker = {id: string; rule: 1 | 2 | 3 | 4 | 5};

export class ApiError extends Error {
  constructor(readonly status: number, readonly body: object) {
    super(JSON.stringify(body));
  }
}

export function bad(error: string): never {
  throw new ApiError(400, {error});
}

export function object(value: unknown, field: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    bad(`${field} must be an object`);
  }
  return value as Record<string, unknown>;
}

export function array(value: unknown, field: string): unknown[] {
  if (!Array.isArray(value)) bad(`${field} must be an array`);
  return value;
}

export function string(value: unknown, field: string): string {
  if (typeof value !== "string") bad(`${field} must be a string`);
  return value;
}

export function name(value: unknown, field: string): string {
  const result = string(value, field);
  if (!result.length) bad(`${field} must have a value`);
  return result;
}

export function integer(value: unknown, field: string, maximum = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0 || value > maximum) {
    bad(`${field} must be a nonnegative integer up to ${maximum}`);
  }
  return value;
}

export function choice<T extends string>(value: unknown, field: string, allowed: readonly T[]): T {
  if (typeof value !== "string" || !allowed.includes(value as T)) {
    bad(`${field} must be one of ${allowed.join(", ")}`);
  }
  return value as T;
}

export function timestamp(value: unknown, field: string): string {
  const result = string(value, field);
  if (!Number.isFinite(Date.parse(result))) bad(`${field} must be a timestamp`);
  return result;
}

export function strings(value: unknown, field: string): string[] {
  return array(value, field).map((item) => string(item, field));
}

export function decodeTicket(row: TicketRow): Ticket {
  return {...row, labels: JSON.parse(row.labels), metadata: JSON.parse(row.metadata)};
}

export function byId<T extends {id: string}>(left: T, right: T): number {
  return left.id < right.id ? -1 : Number(left.id > right.id);
}
