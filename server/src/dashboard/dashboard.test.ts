import {projectFixture} from "../project-fixture";
import {expect, test} from "vitest";

const token = "dashboard-test-token";

function fixture() {
  const stub = projectFixture("by");
  const request = (path: string, init?: RequestInit) => stub.fetch("https://example.com/beryl" + path, init);
  const batch = async (op: object) => {
    const response = await request("/api/batch", {
      method: "POST", headers: {Authorization: `Bearer ${token}`, "X-Actor": "Derek"},
      body: JSON.stringify({ops: [op]}),
    });
    expect(response.status, await response.text()).toBe(200);
  };
  return {stub, request, batch};
}

async function seed(stub: ReturnType<typeof projectFixture>) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  const hash = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  stub.inspect(({store}) => stub.storage.transactionSync(() => {
    store.sql.exec("INSERT INTO meta VALUES ('token_hashes', ?)", JSON.stringify([hash]));
    store.sql.exec(`
      WITH RECURSIVE numbers(n) AS (VALUES (1) UNION ALL SELECT n + 1 FROM numbers WHERE n < 4992)
      INSERT INTO tickets (id, parent, title, status, body, created_at, updated_at, created_by, version)
      SELECT CASE WHEN n <= 200 THEN printf('root%04d', n) ELSE printf('q44s.%d', n + 20) END,
             CASE WHEN n > 200 THEN 'q44s' END, printf('Fixture ticket %d', n),
             CASE WHEN n <= 20 THEN 'in_progress' WHEN n <= 200 THEN 'open' ELSE 'closed' END,
             'Fixture issue body', '2026-11-15T19:30:00Z', '2026-11-15T19:30:00Z', 'Sim actor', n FROM numbers`);
    store.sql.exec(`
      INSERT INTO tickets (id, parent, title, type, status, labels, body, created_at, updated_at, created_by, version)
      VALUES
        ('q44s', NULL, 'Dashboard epic', 'epic', 'open', '[]', 'Collect all dashboard work',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('q44s.7', 'q44s', 'Render milestone', 'milestone', 'open', '[]', '',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('q44s.7.24', 'q44s.7', 'Render live tickets <safely>', 'task', 'in_progress', '["lane:ui"]',
         'Read by-q44s, q44s.7, and Terro for **context**. See \`by-path\` and [by-path](/by/beads/by-path.html?ref=notes).', '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('q44s.7.24.1', 'q44s.7.24', 'Render detail rows', 'task', 'open', '[]', '',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('erro', NULL, 'Simulator failure', 'bug', 'blocked', '["role:steward"]', '',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('brpt', NULL, 'Filed from the app', 'bug', 'open', '["bug-report"]', '',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('wait', NULL, 'Wait for deployment', 'task', 'deferred', '[]', '',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('path', NULL, 'Validate the old path', 'task', 'open', '[]', '',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992)`);
    store.sql.exec(`
      UPDATE tickets SET assignee = 'local_ff743711' WHERE id = 'q44s.7.24'`);
    store.sql.exec(`
      UPDATE tickets SET metadata = '{"_enact_beads":{"owner":"Old owner","started_at":"2026-11-16T12:00:00Z",
        "dependencies":[{"type":"blocks"}]},"enact_host":"test-mac"}' WHERE id = 'wait'`);
    store.sql.exec(`
      INSERT INTO edges VALUES ('q44s.7.24', 'erro', 'blocks'), ('path', 'q44s.7.24', 'waits-for')`);
    store.sql.exec(`
      INSERT INTO aliases VALUES ('old', 'q44s.7.24')`);
    store.sql.exec(`
      INSERT INTO comments (ticket, n, author, at, text)
      VALUES ('q44s.7.24', 1, 'Derek', '2026-11-16T20:00:00Z', 'See by-path.')`);
    store.sql.exec(`
      INSERT INTO history VALUES ('q44s.7.24', 4993, 'Derek', '2026-11-16T19:30:00Z',
                                  '{"title":["Render tickets","Render live tickets <safely>"]}')`);
    store.sql.exec(`
      INSERT INTO history VALUES ('q44s.7.24', 0, 'Derek', '2026-11-16T19:30:00Z',
        '[{"actor":"Derek","created_at":"2026-11-15T19:30:00Z","event_type":"created"}]')`);
    store.sql.exec(`
      INSERT INTO meta VALUES ('seq', '4993');
    `);
  }));
}

test("renders project pages from 5,000 tickets and revalidates them", async () => {
  const {stub, request, batch} = fixture();
  await seed(stub);
  const home = await request("/");
  expect(home.status).toBe(200);
  const homeBody = await home.text();
  expect(homeBody.match(/class="c-card"/g)).toHaveLength(226);
  for (const id of ["active", "all", "epics", "tickets", "automatic", "bug-reports", "deferred"]) {
    expect(homeBody).toContain(`href="#${id}"`);
  }
  expect(homeBody).toContain("Slow CI failures");
  expect(homeBody).toContain('href="/beryl/tickets/Tq44s.7.24"');
  expect(homeBody).toContain('class="c-mono">Tq44s</');
  expect(homeBody).toContain("4792/4795");

  const detail = await request("/tickets/Tq44s.7.24");
  expect(detail.status).toBe(200);
  const detailBody = await detail.text();
  for (const value of ["Description", "Comments", "Changelog", "Subtasks", "Blocked by", "Blocking", "Assignee", "Epic",
    "In progress", "Render live tickets &lt;safely&gt;", "session ff7437", "Validate the old path", "Simulator failure",
    '<strong>context</strong>', 'href="/beryl/tickets/Tq44s"', 'href="/beryl/tickets/Tpath"', "renamed it", "created this",
    'class="c-mono">Tq44s.7.24</', 'href="/beryl/tickets/Tq44s.7">Tq44s.7</',
    'class="c-row-id c-mono c-hide-narrow">Tq44s.7.24.1</']) {
    expect(detailBody).toContain(value);
  }

  expect(detailBody).toMatch(/href="\/beryl\/tickets\/Terro"[^>]*><span class="c-mono">Terro<\/span><\/a>/);
  expect(detailBody).toContain("<code>Tpath</code>");
  expect(detailBody).toContain('href="/beryl/tickets/Tpath?ref=notes">Tpath</a>');
  expect(detailBody).not.toContain("by-path");
  expect(detailBody).not.toContain("by-q44s");

  const homeTag = home.headers.get("etag")!;
  const detailTag = detail.headers.get("etag")!;
  for (const [path, tag] of [["/", homeTag], ["/tickets/Tq44s.7.24", detailTag]]) {
    expect(tag).toMatch(/^W\/"[0-9a-f]+"$/);
    const unchanged = await request(path, {headers: {"If-None-Match": `"another", ${tag.slice(2)}`}});
    expect(unchanged.status).toBe(304);
    expect(await unchanged.text()).toBe("");
    expect(unchanged.headers.get("etag")).toBe(tag);
  }

  await batch({op: "comment", id: "q44s.7.24", text: "New dashboard comment"});
  expect((await request("/", {headers: {"If-None-Match": homeTag}})).status).toBe(304);
  const commented = await request("/tickets/Tq44s.7.24", {headers: {"If-None-Match": detailTag}});
  expect(commented.status).toBe(200);
  expect(await commented.text()).toContain("New dashboard comment");

  await batch({op: "update", id: "erro", set: {title: "Renamed failure"}});
  const changed = await request("/tickets/Tq44s.7.24", {headers: {"If-None-Match": commented.headers.get("etag")!}});
  expect(changed.status).toBe(200);
  expect(await changed.text()).toContain("Renamed failure");

  for (const path of ["/beads/by-q44s.7.24", "/beads/by-q44s.7.24.html", "/tickets/by-q44s.7.24", "/tickets/old", "/tickets/Tq44s.7.24/", "/tickets/q44s.7.24"]) {
    const redirect = await request(path + "?from=bookmark", {redirect: "manual"});
    expect(redirect.status).toBe(308);
    expect(redirect.headers.get("location")).toBe("https://example.com/beryl/tickets/Tq44s.7.24?from=bookmark");
  }
  const homeBeforeReopen = (await request("/")).headers.get("etag")!;
  await batch({op: "reopen", id: "q44s.999"});
  const progressed = await request("/", {headers: {"If-None-Match": homeBeforeReopen}});
  expect(progressed.status).toBe(200);
  expect((await progressed.text()).includes("4791/4795")).toBe(true);

  const head = await request("/tickets/Tq44s.7.24", {method: "HEAD"});
  expect(head.headers.get("etag")).toBe((await request("/tickets/Tq44s.7.24")).headers.get("etag"));
  expect(await head.text()).toBe("");
  expect((await request("/tickets/missing")).status).toBe(404);

  const migrated = await (await request("/tickets/Twait")).text();
  expect(migrated).toContain("<dt>Owner</dt><dd>Old owner</dd>");
  expect(migrated).toContain("<dt>Started</dt><dd><time");
  expect(migrated).toContain("started work");
  expect(migrated).toContain("enact_host");
  expect(migrated).not.toContain("_enact_beads");

  await batch({op: "update", id: "q44s.7.24", set: {parent: null}});
  expect(await (await request("/")).text()).toContain("4791/4793");
});

test("home fragments show current project status independently of the ticket board", async () => {
  const {stub, request} = fixture();
  const board = await request("/");
  const tag = board.headers.get("etag")!;
  stub.inspect(({store}) => {
    const sql = store.sql;
    const at = new Date().toISOString();
    sql.exec(`
      INSERT INTO events VALUES
        (1, ?, 'session', 'mac', 'Casey', '{"session":"Casey","ticket":"by-live","action":"start"}'),
        (2, ?, 'session', 'mac', 'Pat', '{"session":"Pat","ticket":"old","action":"start"}'),
        (3, ?, 'session', 'mac', 'Pat', '{"session":"Pat","ticket":"old","action":"archive"}'),
        (4, ?, 'ci', 'Mac runner', 'ci', '{"os":"macos","passed":false,"tip":"obsolete"}'),
        (5, ?, 'ci', 'Windows runner', 'ci', '{"os":"windows","passed":false,"tip":"0123456789"}'),
        (6, ?, 'ci', 'Mac runner', 'ci', '{"os":"macos","passed":true,"tip":"abcdef1234"}')
    `, at, at, at, at, at, at);
    sql.exec("INSERT INTO leases VALUES ('staging', 'Merge holder', 'mac', ?, '{}', ?)",
      new Date(Date.now() + 180_000).toISOString(), JSON.stringify([{holder: "Queued merger", since: at}]));
    sql.exec("INSERT OR REPLACE INTO meta VALUES ('seq', '6'), ('cutover', 'paused')");
  });
  expect((await request("/", {headers: {"If-None-Match": tag}})).status).toBe(304);

  const status = await request("/?fragment=status");
  expect(status.status).toBe(200);
  expect(status.headers.get("x-cirrus-sequence")).toBe("6");
  const html = await status.text();
  for (const value of [
    "Casey", 'href="/beryl/tickets/Tlive">Tlive</a>', "Windows", "Failed", "01234567", "macOS", "Passed", "abcdef12",
    "Merge holder", "Queued merger", "Writes paused", "UTC day", "Requests", "Rows written",
  ]) expect(html).toContain(value);
  expect(html).not.toMatch(/>Pat<|obsolete|<main|<script/);

  stub.inspect(({store}) => {
    store.sql.exec(
      "INSERT INTO events VALUES (7, ?, 'ci', 'Mac runner', 'ci', ?)",
      new Date().toISOString(), '{"os":"MACOS","passed":false,"tip":"a1b2c3d4e5"}',
    );
  });
  const updated = await (await request("/?fragment=status")).text();
  expect(updated).toContain("a1b2c3d4");
  expect(updated).toContain("01234567");
  expect(updated).not.toContain("abcdef12");

  const live = await (await request("/?fragment=all")).text();
  expect(live).toContain('<main class="c-main"');
  expect(live).toContain('<template id="c-status-fragment">');
  expect(live).toContain("Queued merger");
  expect(live).not.toContain("<script");
});

test("Beryl bookmarks redirect while both API paths reach the existing project", async () => {
  const {stub, request} = fixture();
  await seed(stub);
  for (const prefix of ["by", "beryl"]) {
    const get = (path: string, method = "GET") => stub.fetch(`https://example.com/${prefix}${path}`, {method, redirect: "manual"});
    for (const path of ["", "/"]) {
      if (prefix === "beryl" && path === "/") continue;
      for (const method of ["GET", "HEAD"]) {
        const response = await get(path + "?from=bookmark", method);
        expect(response.status).toBe(308);
        expect(response.headers.get("location")).toBe("https://example.com/beryl/?from=bookmark");
      }
    }
    for (const path of ["/tickets/Tq44s.7.24", "/tickets/by-q44s.7.24", "/beads/by-q44s.7.24.html"]) {
      if (prefix === "beryl" && path === "/tickets/Tq44s.7.24") continue;
      const response = await get(path + "?from=bookmark");
      expect(response.status).toBe(308);
      expect(response.headers.get("location")).toBe("https://example.com/beryl/tickets/Tq44s.7.24?from=bookmark");
    }
    const ticket = await get("/api/tickets/Tq44s.7.24");
    expect(ticket.status).toBe(200);
    expect(await ticket.text()).toContain("Render live tickets <safely>");
  }
  const home = await request("/");
  const html = await home.text();
  expect(html).toContain('<title>Tickets · Beryl</title>');
  expect(html).toContain('<meta name="cirrus-root" content="/beryl/">');
  expect(html).not.toContain('href="/by/');

});
