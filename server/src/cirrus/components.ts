// Cirrus components return HTML. Text is escaped here; arguments such as body
// and trailing contain HTML already rendered by another component.
import { assets as defaultAssets, type Assets } from "./assets";

const escaped: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#x27;",
};

export function esc(
  value: string | number | null | undefined,
  quote = true,
): string {
  return String(value ?? "").replace(
    quote ? /[&<>"']/g : /[&<>]/g,
    (char) => escaped[char],
  );
}

function attrs(values: Record<string, string | null | undefined>): string {
  return Object.entries(values)
    .filter(([, value]) => value != null)
    .map(([name, value]) => ` ${name}="${esc(value)}"`)
    .join("");
}

const tones = [
  "neutral",
  "accent",
  "red",
  "orange",
  "amber",
  "green",
  "blue",
  "violet",
  "muted",
] as const;
export type Tone = (typeof tones)[number];

function tone(name: Tone): Tone {
  if (!tones.includes(name)) throw new Error(`Unknown cirrus tone: ${name}`);
  return name;
}

export interface PageOptions {
  title: string;
  body: string;
  root: string;
  assetRoot?: string;
  assets?: Assets;
  icons?: {svg: string; png: string; apple: string};
  crumbs?: ReadonlyArray<readonly [label: string, href: string | null]>;
  brand?: string;
  description?: string;
  base?: string | null;
  feed?: {seq: string; tickets?: readonly string[]};
}

/** The header and main ids identify the regions cirrus.js swaps on navigation.
 * Root points to the project home. Supply assetRoot when the assets live at
 * a different location, such as the Worker origin. Pass a base URL for a 404.
 */
export function pageRegions({crumbs = [], brand = "", root, body, feed}: PageOptions): string {
  const trail = crumbs.map(([label, href]) =>
    href === null
      ? `<span class="c-crumb" aria-current="page">${esc(label)}</span>`
      : `<a class="c-crumb" href="${esc(href)}">${esc(label)}</a>`,
  ).join("");
  const subscription = feed ? attrs({
    "data-feed-seq": feed.seq,
    "data-feed-tickets": feed.tickets?.join(" "),
  }) : "";
  return `<header class="c-header" id="c-header">
<nav class="c-crumbs" aria-label="Breadcrumbs"><a class="c-brand" href="${esc(root || "./")}">${esc(brand)}</a>${trail}</nav>
</header>
<main class="c-main" id="c-main"${subscription}>
${body}
</main>`;
}

export function page(options: PageOptions, regions = pageRegions(options)): string {
  const {root, assetRoot = root, assets = defaultAssets, title, description = "", base, icons} = options;
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
${base ? `<base href="${esc(base)}">` : ""}
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>${esc(title)}</title>
<meta name="description" content="${esc(description || title)}">
<meta name="cirrus-root" content="${esc(root)}">${icons ? `
<link rel="icon" type="image/png" sizes="32x32" href="${esc(icons.png)}">
<link rel="icon" type="image/svg+xml" sizes="any" href="${esc(icons.svg)}">
<link rel="apple-touch-icon" sizes="180x180" href="${esc(icons.apple)}">
<meta name="apple-mobile-web-app-title" content="${esc(options.brand || title)}">` : ""}
<link rel="stylesheet" href="${esc(assetRoot + assets["cirrus.css"])}">
<script src="${esc(assetRoot + assets["cirrus.js"])}" defer></script>
</head>
<body>
<div class="c-progress-bar" aria-hidden="true"></div>
${regions}
</body>
</html>
`;
}

export function title(text: string, eyebrow = "", sub = ""): string {
  const above = eyebrow ? `<div class="c-eyebrow">${eyebrow}</div>` : "";
  const below = sub ? `<p class="c-subtitle">${sub}</p>` : "";
  return `<div class="c-titleblock">${above}<h1 class="c-title">${esc(text)}</h1>${below}</div>`;
}

export function chip(
  text: string,
  color: Tone = "neutral",
  tip?: string | null,
): string {
  return `<span class="c-chip c-tone-${tone(color)}"${attrs({ "data-tip": tip, tabindex: tip ? "0" : null })}>${esc(text)}</span>`;
}

export function dot(color: Tone, label: string, tip?: string | null): string {
  return `<span class="c-dot c-tone-${tone(color)}"${attrs({ "data-tip": tip, tabindex: tip ? "0" : null })}>${esc(label)}</span>`;
}

export function mono(text: string): string {
  return `<span class="c-mono">${esc(text)}</span>`;
}

export function muted(body: string): string {
  return `<span class="c-muted">${body}</span>`;
}

/** Reveal a tooltip on hover, focus or tap. */
export function tip(body: string, text: string): string {
  return `<span class="c-tip-target" tabindex="0"${attrs({ "data-tip": text })}>${body}</span>`;
}

/** Cirrus shows relative time when script is available. */
export function time(iso: string | null, fallback = ""): string {
  if (!iso) return esc(fallback);
  const timestamp = esc(iso);
  return `<time class="c-time" datetime="${timestamp}" tabindex="0" data-tip="${timestamp}">${esc(iso.slice(0, 10))}</time>`;
}

/** The hovercard names a template on the page. */
export function link(
  href: string,
  body: string,
  hovercard?: string | null,
  cls = "",
): string {
  return `<a href="${esc(href)}"${attrs({ class: cls || null, "data-hovercard": hovercard })}>${body}</a>`;
}

export function template(id: string, body: string): string {
  return `<template id="${esc(id)}">${body}</template>`;
}

function countLabel(count?: number | null): string {
  return count == null ? "" : `<span class="c-count">${count}</span>`;
}

type HeadingLevel = 1 | 2 | 3 | 4 | 5 | 6;

/** A top-level division containing sections that use heading level 3. */
export function part(
  heading: string,
  body: string,
  id: string,
  count?: number | null,
): string {
  return `<section class="c-part"${attrs({ id })}><h2 class="c-part-title">${esc(heading)}${countLabel(count)}</h2>${body}</section>`;
}

export function section(
  heading: string,
  body: string,
  count?: number | null,
  id?: string | null,
  level: HeadingLevel = 2,
): string {
  const title = heading
    ? `<h${level} class="c-section-title">${esc(heading)}${countLabel(count)}</h${level}>`
    : "";
  return `<section class="c-section"${attrs({ id })}>${title}${body}</section>`;
}

export function subsection(
  heading: string,
  body: string,
  count?: number | null,
  level: HeadingLevel = 3,
): string {
  return `<div class="c-subsection"><h${level} class="c-subsection-title">${esc(heading)}${countLabel(count)}</h${level}>${body}</div>`;
}

export function grid(cards: Iterable<string>): string {
  return `<div class="c-grid">${Array.from(cards).join("")}</div>`;
}

/** A whole-card link. Cirrus prefetches the target as the card scrolls into view. */
export function card(
  href: string,
  body: string,
  hovercard?: string | null,
): string {
  return `<a class="c-card" href="${esc(href)}" data-prefetch${attrs({ "data-hovercard": hovercard })}>${body}</a>`;
}

export function rows(items: Iterable<string>): string {
  return `<ul class="c-rows">${Array.from(items, (item) => `<li>${item}</li>`).join("")}</ul>`;
}

export function row(main: string, trailing = "", href?: string | null): string {
  const inner = `<span class="c-row-main">${main}</span><span class="c-row-trailing">${trailing}</span>`;
  return href == null
    ? `<div class="c-row">${inner}</div>`
    : `<a class="c-row" href="${esc(href)}" data-prefetch>${inner}</a>`;
}

/** Label and HTML value pairs for a property list. */
export function meta(
  items: ReadonlyArray<readonly [label: string, value: string]>,
): string {
  const body = items
    .map(
      ([label, value]) =>
        `<div class="c-meta-item"><dt>${esc(label)}</dt><dd>${value}</dd></div>`,
    )
    .join("");
  return `<dl class="c-meta">${body}</dl>`;
}

export function progress(
  done: number,
  total: number,
  tipText?: string | null,
): string {
  const percentage = total === 0 ? 0 : (100 * done) / total;
  const lower = Math.floor(percentage);
  // Python rounds exact halves to the even integer.
  const share =
    percentage - lower === 0.5 && lower % 2 === 0
      ? lower
      : Math.round(percentage);
  return (
    `<span class="c-progress"${attrs({ "data-tip": tipText, tabindex: tipText ? "0" : null })}>` +
    `<span class="c-progress-track"><span class="c-progress-fill" style="width:${share}%">` +
    `</span></span><span class="c-progress-label">${done}/${total}</span></span>`
  );
}

export function disclosure(
  summary: string,
  body: string,
  open = false,
): string {
  return (
    `<details class="c-disclosure"${open ? " open" : ""}><summary>${summary}</summary>` +
    `<div class="c-disclosure-body">${body}</div></details>`
  );
}

export function skeleton(lines = 3): string {
  const widths = [92, 78, 85, 64, 88, 70];
  const bars = Array.from(
    { length: lines },
    (_, index) =>
      `<span class="c-skeleton-line" style="width:${widths[index % widths.length]}%"></span>`,
  ).join("");
  return `<div class="c-skeleton" aria-busy="true" aria-label="Loading">${bars}</div>`;
}

/** Fill a fragment after the page shows, with a skeleton in the meantime. */
export function include(url: string, missing: string, lines = 3): string {
  return `<div class="c-include"${attrs({ "data-include": url, "data-missing": missing })}>${skeleton(lines)}</div>`;
}

export function empty(text: string): string {
  return `<p class="c-empty">${esc(text)}</p>`;
}

export function prose(html: string): string {
  return `<div class="c-prose">${html}</div>`;
}

/** A reading column with a property rail that moves above it on narrow screens. */
export function layout(main: string, aside: string): string {
  return (
    `<div class="c-layout"><div class="c-layout-main">${main}</div>` +
    `<aside class="c-layout-aside">${aside}</aside></div>`
  );
}

export function timeline(entries: Iterable<string>): string {
  return `<ol class="c-timeline">${Array.from(entries).join("")}</ol>`;
}

export function timelineEntry(
  body: string,
  when: string,
  who = "",
  quiet = false,
): string {
  const cls = quiet ? "c-timeline-entry c-quiet" : "c-timeline-entry";
  const actor = who ? `<span class="c-timeline-actor">${who}</span>` : "";
  return (
    `<li class="${cls}"><div class="c-timeline-body">${actor}${body}</div>` +
    `<div class="c-timeline-when">${when}</div></li>`
  );
}

/** The first character of the full author name is the avatar. Shown is HTML. */
export function comment(
  author: string,
  shown: string,
  when: string,
  body: string,
): string {
  const initial = author.trim()[Symbol.iterator]().next().value ?? "?";
  return (
    `<article class="c-comment"><div class="c-avatar" aria-hidden="true">${esc(initial.toUpperCase())}</div>` +
    `<div class="c-comment-main"><header class="c-comment-head">` +
    `<span class="c-comment-author">${shown}</span>${when}</header>` +
    `<div class="c-prose">${body}</div></div></article>`
  );
}

type TocEntry = readonly [
  label: string,
  anchor: string,
  count: number | null,
  depth: number,
];

/** Depth 0 identifies a part and depth 1 a section within it. */
export function toc(entries: readonly TocEntry[]): string {
  const items = entries
    .map(
      ([label, anchor, count, depth]) =>
        `<li class="c-toc-item c-toc-depth-${depth}"><a class="c-toc-link" ` +
        `href="#${esc(anchor)}"><span class="c-toc-label">${esc(label)}</span>` +
        (count === null ? "" : `<span class="c-toc-count">${count}</span>`) +
        "</a></li>",
    )
    .join("");
  return `<nav class="c-toc" aria-label="On this page"><ul>${items}</ul></nav>`;
}

/** The sticky contents sit beside the page, or above it on a narrow screen. */
export function withToc(nav: string, body: string): string {
  return (
    `<div class="c-toc-layout"><div class="c-toc-rail">${nav}</div>` +
    `<div class="c-toc-main">${body}</div></div>`
  );
}
