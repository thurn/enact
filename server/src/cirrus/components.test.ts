import { expect, test } from "vitest";
import * as c from "./components";
import { assets, components } from "./fixtures.json";

const text = `A & <B> "C" 'D'`;
const body = "<strong>Example</strong>";
const url = '/ticket?x="one"&y=two';

const outputs = {
  esc: c.esc(text),
  escNull: c.esc(null),
  page: c.page({ title: text, body, root: "", assets }),
  pageWithOptions: c.page({
    title: text,
    body,
    root: "../",
    assets,
    crumbs: [
      [text, url],
      ["Here", null],
    ],
    brand: "Enact & Co.",
    description: text,
    base: "https://example.com/enact/",
  }),
  title: c.title(text, body, body),
  titlePlain: c.title(text),
  chip: c.chip(text),
  chipWithTip: c.chip(text, "green", text),
  dot: c.dot("red", text, ""),
  mono: c.mono(text),
  muted: c.muted(body),
  tip: c.tip(body, text),
  time: c.time("2026-11-16T12:35:00Z"),
  timeFallback: c.time(null, text),
  link: c.link(url, body, text, "c-extra"),
  template: c.template(text, body),
  part: c.part(text, body, "active", 0),
  section: c.section(text, body, 2, "open", 3),
  sectionUntitled: c.section("", body),
  subsection: c.subsection(text, body, 0, 4),
  grid: c.grid([body, body].values()),
  card: c.card(url, body, text),
  rows: c.rows([body, body].values()),
  row: c.row(body, body, url),
  rowPlain: c.row(body),
  meta: c.meta([
    [text, body],
    ["Next", body],
  ]),
  progress: c.progress(1, 8, text),
  progressEmpty: c.progress(0, 0),
  disclosure: c.disclosure(body, body),
  disclosureOpen: c.disclosure(body, body, true),
  skeleton: c.skeleton(8),
  include: c.include(url, text, 2),
  empty: c.empty(text),
  prose: c.prose(body),
  layout: c.layout(body, body),
  timeline: c.timeline([body, body].values()),
  timelineEntry: c.timelineEntry(body, "today", body, true),
  timelineEntryPlain: c.timelineEntry(body, "today"),
  comment: c.comment("  𐐨 writer", body, "today", body),
  commentAnonymous: c.comment("", body, "today", body),
  toc: c.toc([
    [text, "active", 0, 0],
    ["Next", text, null, 1],
  ]),
  withToc: c.withToc(body, body),
} satisfies Record<keyof typeof components, string>;

for (const name of Object.keys(outputs) as (keyof typeof outputs)[]) {
  test(`${name} matches the Python renderer`, () => {
    expect(outputs[name]).toBe(components[name]);
  });
}
