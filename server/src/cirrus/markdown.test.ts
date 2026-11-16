import { expect, test } from "vitest";
import { markdown } from "./fixtures.json";
import { inline, render } from "./markdown";

for (const { name, kind, source, html } of markdown) {
  test(`${name} matches the Python renderer`, () => {
    const autolink =
      kind === "autolink"
        ? (text: string) => text.replaceAll("by-x", "<a>by-x</a>")
        : undefined;
    const renderer = kind === "render" ? render : inline;
    expect(renderer(source, autolink)).toBe(html);
  });
}

test("literal private-use characters survive alongside code spans", () => {
  expect(inline("\uE0000\uE000 and `code`")).toBe(
    "\uE0000\uE000 and <code>code</code>",
  );
});
