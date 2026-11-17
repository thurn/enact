// Cirrus renders the Markdown subset used in ticket descriptions and comments.
// Source text is escaped. Only the renderer and its trusted autolink hook
// supply HTML.
import { esc } from "./components";

export type Autolink = (escapedText: string) => string;

const fence = /^(\s*)(```|~~~)([^\n]*)$/;
const heading = /^(#{1,6})\s+([^\n]*?)\s*#*\s*$/;
const bullet = /^(\s*)[-*+]\s+([^\n]*)$/;
const numbered = /^(\s*)\p{Decimal_Number}+[.)]\s+([^\n]*)$/u;
const quote = /^\s*>\s?([^\n]*)$/;
const tableRule = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const codeSpan = /(`+)([^\n]+?)\1/g;
const link = /\[([^\]]+)\]\(([^)\s]+)\)/g;
const url = /(?<!["'=\p{L}\p{N}_])https?:\/\/[^\s<]+[^\s<.,;:!?)\]'"]/gu;
const bold = /\*\*([^\n]+?)\*\*|__([^\n]+?)__/g;
const italic =
  /(?<![\p{L}\p{N}_*])\*(?!\s)([^\n]+?)(?<!\s)\*(?![\p{L}\p{N}_*])|(?<![\p{L}\p{N}_])_(?!\s)([^\n]+?)(?<!\s)_(?![\p{L}\p{N}_])/gu;
const safeTarget = /^(https?:\/\/|\/|\.|#|[\p{L}\p{N}_-]+(\/|$))/u;
const mark = "\uE000";

const noLinks: Autolink = (text) => text;

/** Render inline Markdown, leaving the autolink hook out of code and links. */
export function inline(source: string, autolink: Autolink = noLinks): string {
  const kept: string[] = [];
  // A delimiter absent from the source keeps literal text out of the lookup.
  let delimiter = mark;
  while (source.includes(delimiter)) delimiter += mark;
  const keep = (html: string) => {
    kept.push(html);
    return `${delimiter}${kept.length - 1}${delimiter}`;
  };

  let text = source.replace(codeSpan, (_match, _ticks: string, code: string) =>
    keep(`<code>${esc(code)}</code>`),
  );
  text = text.replace(link, (match, label: string, target: string) => {
    if (!safeTarget.test(target)) return keep(esc(match));
    return keep(`<a href="${esc(target)}">${esc(label)}</a>`);
  });
  text = text.replace(url, (target) => {
    const escaped = esc(target);
    return keep(`<a href="${escaped}">${escaped}</a>`);
  });
  text = esc(text, false);
  text = text.replace(
    bold,
    (_match, a: string, b: string) => `<strong>${a || b}</strong>`,
  );
  text = text.replace(
    italic,
    (_match, a: string, b: string) => `<em>${a || b}</em>`,
  );
  return autolink(text).replace(
    new RegExp(`${delimiter}(\\d+)${delimiter}`, "g"),
    (match, index: string) => kept[Number(index)] ?? match,
  );
}

function cells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function listItem(line: string): RegExpExecArray | null {
  return bullet.exec(line) ?? numbered.exec(line);
}

function paragraphEnds(line: string): boolean {
  return (
    !line.trim() ||
    fence.test(line) ||
    heading.test(line) ||
    listItem(line) !== null ||
    quote.test(line)
  );
}

/** Render blocks, including fenced code, tables, lists and quotes. */
export function render(
  source: string, autolink: Autolink = noLinks, prepareInline: (source: string) => string = noLinks,
): string {
  const lines = source.replaceAll("\r\n", "\n").split("\n");
  const out: string[] = [];
  const inlines = (text: string) => inline(prepareInline(text), autolink);
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index++;
      continue;
    }

    const fenced = fence.exec(line);
    if (fenced) {
      const marker = fenced[2];
      const language = fenced[3].trim().split(" ")[0];
      const body: string[] = [];
      index++;
      while (index < lines.length && !lines[index].trim().startsWith(marker)) {
        body.push(lines[index++]);
      }
      index++;
      const cls = language ? ` class="language-${esc(language)}"` : "";
      out.push(`<pre><code${cls}>${esc(body.join("\n"))}</code></pre>`);
      continue;
    }

    const header = heading.exec(line);
    if (header) {
      // A description's headings sit under the page's own section titles.
      const level = Math.min(header[1].length + 2, 6);
      out.push(`<h${level}>${inlines(header[2])}</h${level}>`);
      index++;
      continue;
    }

    if (quote.test(line)) {
      const quoted: string[] = [];
      while (index < lines.length) {
        const match = quote.exec(lines[index]);
        if (!match) break;
        quoted.push(match[1]);
        index++;
      }
      out.push(
        `<blockquote>${render(quoted.join("\n"), autolink, prepareInline)}</blockquote>`,
      );
      continue;
    }

    const next = lines[index + 1];
    if (line.includes("|") && next?.includes("|") && tableRule.test(next)) {
      const headers = cells(line)
        .map((cell) => `<th>${inlines(cell)}</th>`)
        .join("");
      const rows: string[] = [];
      index += 2;
      while (index < lines.length && lines[index].includes("|")) {
        const data = cells(lines[index++])
          .map((cell) => `<td>${inlines(cell)}</td>`)
          .join("");
        rows.push(`<tr>${data}</tr>`);
      }
      out.push(
        `<div class="c-table"><table><thead><tr>${headers}</tr></thead>` +
          `<tbody>${rows.join("")}</tbody></table></div>`,
      );
      continue;
    }

    if (listItem(line)) {
      const [html, after] = listBlock(lines, index, inlines);
      out.push(html);
      index = after;
      continue;
    }

    const paragraph = [line.trim()];
    index++;
    while (index < lines.length && !paragraphEnds(lines[index])) {
      paragraph.push(lines[index++].trim());
    }
    out.push(`<p>${inlines(paragraph.join(" "))}</p>`);
  }
  return out.join("\n");
}

/** Return the list and the index of the next line. */
function listBlock(
  lines: string[],
  index: number,
  inlines: (source: string) => string,
): [string, number] {
  const first = listItem(lines[index])!;
  const indent = first[1].length;
  const sameKind = bullet.test(lines[index]) ? bullet : numbered;
  const items: string[] = [];

  while (index < lines.length) {
    const match = listItem(lines[index]);
    if (!match || match[1].length !== indent) break;
    const text = [match[2]];
    let nested = "";
    index++;
    while (index < lines.length && lines[index].trim()) {
      const line = lines[index];
      const deeper = listItem(line);
      if (deeper && deeper[1].length > indent) {
        [nested, index] = listBlock(lines, index, inlines);
        continue;
      }
      if (deeper || fence.test(line) || heading.test(line)) break;
      text.push(line.trim());
      index++;
    }
    items.push(`<li>${inlines(text.join(" "))}${nested}</li>`);
    // A blank line between items of the same kind keeps the list going.
    if (index + 1 < lines.length && !lines[index].trim()) {
      const after = sameKind.exec(lines[index + 1]);
      if (after?.[1].length === indent) index++;
    }
  }
  const tag = sameKind === numbered ? "ol" : "ul";
  return [`<${tag}>${items.join("")}</${tag}>`, index];
}
