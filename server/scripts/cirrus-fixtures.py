"""Generate the parity fixtures with Beryl's Python renderer.

Usage: python3 server/scripts/cirrus-fixtures.py /path/to/beryl
"""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(sys.argv[1]) / "status/src"))

from cirrus import components as c, markdown
from cirrus.assets import Assets

text = """A & <B> "C" 'D'"""
body = "<strong>Example</strong>"
url = '/ticket?x="one"&y=two'
paths = {
    "cirrus.css": "assets/cirrus.sample.css",
    "cirrus.js": "assets/cirrus.sample.js",
}
assets = Assets(paths)
components = {
    "esc": c.esc(text),
    "escNull": c.esc(None),
    "page": c.page(title=text, body=body, root="", assets=assets),
    "pageWithOptions": c.page(
        title=text,
        body=body,
        root="../",
        assets=assets,
        crumbs=[(text, url), ("Here", None)],
        brand="Enact & Co.",
        description=text,
        base="https://example.com/enact/",
    ),
    "title": c.title(text, body, body),
    "titlePlain": c.title(text),
    "chip": c.chip(text),
    "chipWithTip": c.chip(text, "green", text),
    "dot": c.dot("red", text, ""),
    "mono": c.mono(text),
    "muted": c.muted(body),
    "tip": c.tip(body, text),
    "time": c.time("2026-11-16T12:35:00Z"),
    "timeFallback": c.time(None, text),
    "link": c.link(url, body, text, "c-extra"),
    "template": c.template(text, body),
    "part": c.part(text, body, "active", 0),
    "section": c.section(text, body, 2, "open", 3),
    "sectionUntitled": c.section("", body),
    "subsection": c.subsection(text, body, 0, 4),
    "grid": c.grid(iter([body, body])),
    "card": c.card(url, body, text),
    "rows": c.rows(iter([body, body])),
    "row": c.row(body, body, url),
    "rowPlain": c.row(body),
    "meta": c.meta([(text, body), ("Next", body)]),
    "progress": c.progress(1, 8, text),
    "progressEmpty": c.progress(0, 0),
    "disclosure": c.disclosure(body, body),
    "disclosureOpen": c.disclosure(body, body, True),
    "skeleton": c.skeleton(8),
    "include": c.include(url, text, 2),
    "empty": c.empty(text),
    "prose": c.prose(body),
    "layout": c.layout(body, body),
    "timeline": c.timeline(iter([body, body])),
    "timelineEntry": c.timeline_entry(body, "today", body, True),
    "timelineEntryPlain": c.timeline_entry(body, "today"),
    "comment": c.comment("  𐐨 writer", body, "today", body),
    "commentAnonymous": c.comment("", body, "today", body),
    "toc": c.toc([(text, "active", 0, 0), ("Next", text, None, 1)]),
    "withToc": c.with_toc(body, body),
}


def link(text: str) -> str:
    return text.replace("by-x", "<a>by-x</a>")


samples = [
    ("HTML escaping", "render", "<script>x</script>"),
    ("marks and code", "inline", "**bold** and *it* and `a<b`"),
    ("unsafe explicit target", "inline", "[x](javascript:alert(1))"),
    ("safe explicit target", "inline", "[x](https://e.com)"),
    ("bare URL punctuation", "inline", "see https://e.com/a."),
    (
        "headings, nested lists, fences and quotes",
        "render",
        "# Head\n\n- a\n- b\n  - c\n\n1. one\n\n```py\nx < 1\n```\n\n> q",
    ),
    ("tables", "render", "| a | b |\n|---|---|\n| 1 | 2 |"),
    ("autolink skips code and links", "autolink", "by-x and `by-x` [by-x](./by-x)"),
    ("paragraph continuation", "render", " First  \r\n   second \r\n\r\nThird"),
    ("heading levels", "render", "## Two ##\n##### Five\n###### Six"),
    ("quotes and unclosed fence", "render", "> > nested\n> outer\n\n~~~js\n'a<b'"),
    (
        "blank lines within and between lists",
        "render",
        "- one\n  more\n\n- two\n\n2) next",
    ),
    ("Unicode lists and links", "render", "١. [字](字/file)\n٢. café_it_ and _em_"),
    ("quotes, escaping and entities", "inline", """' " &amp; [a&b](./a"b) `"'`"""),
]

markdown_cases = []
for name, kind, source in samples:
    if kind == "render":
        html = markdown.render(source)
    elif kind == "autolink":
        html = markdown.inline(source, link)
    else:
        html = markdown.inline(source)
    markdown_cases.append(dict(name=name, kind=kind, source=source, html=html))

target = Path(__file__).resolve().parents[1] / "src/cirrus/fixtures.json"
target.write_text(
    json.dumps(
        dict(assets=paths, components=components, markdown=markdown_cases),
        indent=2,
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
