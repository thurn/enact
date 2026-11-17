import * as c from "../cirrus/components";
import {brand, siteIcons, childrenProgress, person, statusDot, ticketHeading, ticketUrl} from "./common";
import type {HomeCard, homeData} from "./data";

const automatic = [
  ["Slow CI failures", ["role:steward"]],
  ["Incidents", ["role:justiciar"]],
  ["Flaky checks", ["flaky-check"]],
  ["Sweep and nightly failures", ["sweep-failure", "nightly-failure", "b3d-sweep"]],
  ["CI time budget", ["ci-cut", "check-budget"]],
  ["CI structure review", ["ci-structure", "ci-structure-log"]],
  ["Count gate", ["count-creep", "count-lower"]],
  ["Check verdicts", ["ci-check"]],
] as const;

export function home(root: string, {cards, counts}: ReturnType<typeof homeData>, seq: string): c.PageOptions {
  const progress = new Map(counts.map((count) => [count.id, count]));
  const active: HomeCard[] = [];
  const epics: HomeCard[] = [];
  const tickets: HomeCard[] = [];
  const deferred: HomeCard[] = [];
  const bugReports: HomeCard[] = [];
  const groups: HomeCard[][] = automatic.map(() => []);

  for (const ticket of cards) {
    const labels = JSON.parse(ticket.labels) as string[];
    const group = automatic.findIndex(([, keys]) => keys.some((key) => labels.includes(key)));
    if (ticket.status === "in_progress") active.push(ticket);
    if (ticket.status === "deferred") deferred.push(ticket);
    else if (labels.includes("bug-report")) bugReports.push(ticket);
    else if (group !== -1) groups[group].push(ticket);
    else if (ticket.parent === null) (ticket.type === "epic" ? epics : tickets).push(ticket);
  }

  function grid(items: HomeCard[], showKind = true, showState = true): string {
    return c.grid(items.map((ticket) => {
      const state = showState && ticket.status !== "open" ? statusDot(ticket.status) : "";
      const kind = showKind && ticket.type === "epic" ? c.chip("Epic", "violet") : "";
      const foot = state + kind + childrenProgress(progress.get(ticket.id)) + (ticket.assignee ? person(ticket.assignee) : "")
        + `<span class="c-card-time">${c.time(ticket.updated_at)}</span>`;
      return c.card(ticketUrl(root, ticket.id), ticketHeading(ticket)
        + `<h3 class="c-card-title">${c.esc(ticket.title)}</h3><div class="c-card-foot">${foot}</div>`);
    }));
  }

  const sections = [
    ["Epics", "epics", epics.length, grid(epics, false)],
    ["Tickets", "tickets", tickets.length, grid(tickets)],
    ["Filed automatically", "automatic", groups.reduce((sum, group) => sum + group.length, 0),
      groups.map((group, index) => group.length ? c.subsection(automatic[index][0], grid(group), group.length, 4) : "").join("")],
    ["Bug reports", "bug-reports", bugReports.length, grid(bugReports)],
    ["Deferred", "deferred", deferred.length, grid(deferred)],
  ] as const;
  const shown = sections.filter(([, , count]) => count > 0);
  const total = shown.reduce((sum, [, , count]) => sum + count, 0);
  const nav = c.toc([
    ["Active", "active", active.length, 0], ["All", "all", total, 0],
    ...shown.map(([heading, anchor, count]) => [heading, anchor, count, 1] as const),
    ["Project status", "project-status", null, 0],
  ]);
  const activePart = c.part("Active", active.length ? grid(active, true, false) : c.empty("Nothing is in progress."), "active", active.length);
  const allPart = c.part("All", shown.map(([heading, anchor, count, body]) => c.section(heading, body, count, anchor, 3)).join("")
    || c.empty("No open tickets."), "all", total);

  const status = c.part("Project status", c.include(root + "?fragment=status", "Project status is unavailable."), "project-status");
  return {
    title: `Tickets · ${brand(root)}`, body: c.title("Tickets", "", "What is in progress, and everything open.")
      + c.withToc(nav, activePart + allPart + status),
    feed: {seq},
    root, assetRoot: "/", icons: siteIcons, crumbs: [["Tickets", null]], brand: brand(root),
  };
}
