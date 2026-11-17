import * as c from "../cirrus/components";
import type {Ticket} from "../ticket-types";
import type {Card, Progress} from "./data";

const statusDisplay: Record<Ticket["status"], [c.Tone, string]> = {
  in_progress: ["blue", "In progress"],
  blocked: ["amber", "Blocked"],
  open: ["neutral", "Open"],
  deferred: ["muted", "Deferred"],
  closed: ["green", "Closed"],
};

export const priorityNames = ["Critical", "High", "Medium", "Low", "Backlog"];
const priorityTones: c.Tone[] = ["red", "orange", "neutral"];

export function statusDot(status: Ticket["status"], label = true): string {
  const [tone, name] = statusDisplay[status];
  return c.dot(tone, label ? name : "", label ? undefined : name);
}

export function priorityChip(priority: number): string {
  return c.chip(`P${priority}`, priorityTones[priority] ?? "muted", priorityNames[priority]);
}

export function person(name: string | null): string {
  if (!name) return c.muted("Nobody");
  const session = /^local_([0-9a-f]{6})[0-9a-f-]*$/.exec(name)?.[1];
  const host = /^vizier:([^:]+):/.exec(name)?.[1];
  const uuid = /^([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.exec(name)?.[1];
  const short = session ? `session ${session}` : host ? `vizier on ${host}` : uuid;
  return short ? c.tip(c.esc(short), name) : c.esc(name);
}

export function childrenProgress(count: Progress | undefined): string {
  if (!count) return "";
  const {closed, working, total} = count;
  return c.progress(closed, total, `${closed} closed, ${working} in progress, ${total - closed - working} to do`);
}

export function displayTicketId(id: string): string {
  return `T${id}`;
}

export function ticketUrl(root: string, id: string): string {
  return root + "tickets/" + encodeURIComponent(id);
}

export function brand(root: string): string {
  const project = root.slice(1, -1);
  if (project === "by") return "Beryl";
  if (project === "en") return "Enact";
  return project;
}

export function ticketHeading(ticket: Card): string {
  return `<div class="c-card-top">${c.mono(displayTicketId(ticket.id))}${priorityChip(ticket.priority)}</div>`;
}
