import * as c from "../cirrus/components";
import {render} from "../cirrus/markdown";
import {brand, childrenProgress, displayTicketId, person, priorityChip, priorityNames, statusDot, ticketHeading, ticketUrl} from "./common";
import {byAttention, mentions, type Card, type ticketData} from "./data";
import {timeline} from "./history";

export function ticketPage(root: string, project: string, data: ReturnType<typeof ticketData>, seq: string): c.PageOptions {
  const {ticket, children, chain, comments, history, groups, counts} = data;
  const displayId = displayTicketId(ticket.id);
  const progress = new Map(counts.map((count) => [count.id, count]));
  const references = new Map(data.references.map((card) => [card.id, card]));
  const links = new Map(data.links);
  const templates = new Map<string, string>();

  function hoverLink(card: Card, body: string): string {
    const key = "hc-" + card.id;
    if (!templates.has(key)) {
      const count = progress.get(card.id);
      templates.set(key, c.template(key, ticketHeading(card)
        + `<div class="c-hovercard-title">${c.esc(card.title)}</div>`
        + `<div class="c-hovercard-meta">${statusDot(card.status)}<span>${c.esc(card.type)}</span>`
        + (count ? c.progress(count.closed, count.total) : "") + "</div>"));
    }
    return c.link(ticketUrl(root, card.id), body, key);
  }

  function linkText(text: string): string {
    return mentions(project, text, (name) => {
      const card = references.get(links.get(name) ?? "");
      return card ? hoverLink(card, c.mono(name)) : name;
    });
  }

  const entries: [string, string, number | null, number][] = [];
  const main: string[] = [];
  function add(heading: string, html: string, count: number | null = null): void {
    const anchor = heading.toLowerCase().replaceAll(" ", "-");
    entries.push([heading, anchor, count, 0]);
    main.push(c.section(heading, html, count, anchor));
  }

  const eyebrow = c.mono(displayId) + (ticket.type === "epic" ? c.chip("Epic", "violet") : "") + statusDot(ticket.status);
  for (const [heading, text] of [
    ["Close reason", ticket.status === "closed" ? ticket.close_reason : ""],
    ["Description", ticket.body], ["Acceptance criteria", ticket.acceptance],
    ["Design", ticket.design], ["Notes", ticket.notes],
  ] as const) {
    if (text?.trim()) add(heading, c.prose(render(text, linkText)));
  }

  function row(card: Card): string {
    const label = statusDot(card.status, false)
      + `<span class="c-row-id c-mono c-hide-narrow">${c.esc(displayTicketId(card.id))}</span>`
      + `<span class="c-row-text">${c.esc(card.title)}</span>`;
    return c.row(label, childrenProgress(progress.get(card.id)) + priorityChip(card.priority), ticketUrl(root, card.id));
  }

  for (const [heading, cards] of [
    ["Milestones", children.filter((child) => child.type === "epic" || child.type === "milestone")],
    ["Subtasks", children.filter((child) => child.type !== "epic" && child.type !== "milestone")],
    ...groups.map(([name, ids]) => [name, ids.flatMap((id) => references.get(id) ?? []).sort(byAttention)] as const),
  ] as const) {
    if (!cards.length) continue;
    const open = cards.filter((card) => card.status !== "closed");
    const closed = cards.filter((card) => card.status === "closed");
    const archived = closed.length ? c.rows(closed.map(row)) : "";
    add(heading, open.length ? c.rows(open.map(row)) + (archived ? c.disclosure(`${closed.length} closed`, archived) : "") : archived, cards.length);
  }

  if (comments.length) add("Comments", comments.map(({author, at, text}) =>
    c.comment(author, person(author), c.time(at), render(text, linkText))).join(""), comments.length);
  const {_enact_beads: imported, ...metadata} = ticket.metadata;
  const legacy = imported as {owner?: unknown; started_at?: unknown} | undefined;
  const started = typeof legacy?.started_at === "string" ? legacy.started_at : "";
  add("Changelog", timeline(ticket, history, linkText, started));
  const details: [string, string][] = ticket.created_by ? [["Created by", c.esc(ticket.created_by)]] : [];
  if (typeof legacy?.owner === "string" && legacy.owner) details.push(["Owner", c.esc(legacy.owner)]);
  if (started) details.push(["Started", c.time(started)]);
  for (const [key, value] of Object.entries(metadata).sort(([left], [right]) => left.localeCompare(right))) {
    details.push([key, c.mono(typeof value === "string" ? value : JSON.stringify(value))]);
  }
  if (details.length) main.push(c.section("", c.disclosure("Details", c.meta(details))));

  const properties: [string, string][] = [
    ["Status", statusDot(ticket.status)],
    ["Priority", priorityChip(ticket.priority) + c.muted(priorityNames[ticket.priority] ?? "")],
    ["Assignee", person(ticket.assignee)],
  ];
  const parent = ticket.parent && references.get(ticket.parent);
  if (parent) properties.push(["Epic", hoverLink(parent, c.esc(parent.title))]);
  if (ticket.labels.length) properties.push(["Labels", ticket.labels.map((label) => c.chip(label, "muted")).join("")]);
  properties.push(["Type", c.esc(ticket.type[0].toUpperCase() + ticket.type.slice(1))],
    ["Created", c.time(ticket.created_at)], ["Updated", c.time(ticket.updated_at)]);
  if (ticket.closed_at) properties.push(["Closed", c.time(ticket.closed_at)]);

  const layout = c.layout(main.join(""), c.meta(properties));
  const crumbs = chain.filter((id) => references.has(id)).map((id) => [displayTicketId(id), ticketUrl(root, id)] as const);
  return {
    feed: {seq, tickets: [ticket.id, ...data.watched]},
    title: `${ticket.title} · ${displayId}`,
    body: c.title(ticket.title, eyebrow) + (entries.length >= 3 ? c.withToc(c.toc(entries), layout) : layout)
      + [...templates.values()].join(""),
    root, assetRoot: "/", brand: brand(root), description: ticket.title,
    crumbs: [["Tickets", root], ...crumbs, [displayId, null]],
  };
}
