import {dashboardRoot, storedProject} from "./project-paths";

export function mentions(project: string, text: string, replace: (id: string) => string): string {
  return text.replace(/\bT?[a-z0-9-]+(?:\.[0-9]+)*\b/g, (word) => {
    // Bare roots can be ordinary words. In prose, link explicit prefixes and dotted IDs.
    return word.startsWith("T") || word.startsWith(project + "-") || /^[a-z0-9-]+\.[0-9]/.test(word)
      ? replace(word) : word;
  });
}

export function ticketPaths(
  project: string, text: string, replace: (id: string, url: string) => string,
): string {
  return text.replace(
    /(?<![\w./-])(?:https?:\/\/tickets\.thurn\.workers\.dev)?\/([a-z0-9-]+)\/(tickets|beads)\/(T?[a-z0-9-]+(?:\.[0-9]+)*(?:\.html)?)\b/g,
    (url, owner: string, route: string, id: string) =>
      storedProject(owner) === project ? replace(route === "beads" ? id.replace(/\.html$/, "") : id, url) : url,
  );
}

export function displayTicketId(id: string): string {
  return `T${id}`;
}

export function ticketUrl(root: string, id: string): string {
  return root + "tickets/" + encodeURIComponent(displayTicketId(id));
}

type Resolve = (name: string) => string | undefined;

export function rewriteTicketPaths(project: string, text: string, resolve: Resolve): string {
  return ticketPaths(project, text, (name, url) => {
    const id = resolve(name);
    if (!id) return url;
    const origin = url.startsWith("http") ? new URL(url).origin : "";
    return origin + ticketUrl(dashboardRoot(project), id);
  });
}

/** Format Markdown for text output, preserving fenced commands and unrelated URLs. */
export function readableTicketText(project: string, source: string, resolve: Resolve): string {
  const literals = /^(?:[ \t]*)(`{3,}|~{3,})[^\n]*\n[\s\S]*?^[ \t]*\1[^\n]*$|https?:\/\/[^\s<>()\]]+|(?<![\w./-])\/[^\s<>()\]]+/gm;
  let offset = 0;
  let result = "";
  function text(value: string) {
    return mentions(project, value, (name) => {
      if (name.startsWith("T")) return name;
      const id = resolve(name);
      return id ? displayTicketId(id) : name;
    });
  }
  for (const match of source.matchAll(literals)) {
    result += text(source.slice(offset, match.index));
    result += match[1] ? match[0] : rewriteTicketPaths(project, match[0], resolve);
    offset = match.index + match[0].length;
  }
  return result + text(source.slice(offset));
}
