import {env, exports} from "cloudflare:workers";
import {runInDurableObject} from "cloudflare:test";
import {expect, test} from "vitest";
import {Dashboard, dashboardRoute} from "./index";

const request = (path: string, init?: RequestInit) => exports.default.fetch("https://example.com/by" + path, init);
const stub = env.TICKETS.getByName("by");

async function seed() {
  await runInDurableObject(stub, ({store}, {storage}) => storage.transactionSync(() => {
    store.sql.exec(`
      WITH RECURSIVE numbers(n) AS (VALUES (1) UNION ALL SELECT n + 1 FROM numbers WHERE n < 4992)
      INSERT INTO tickets (id, parent, title, status, body, created_at, updated_at, created_by, version)
      SELECT CASE WHEN n <= 200 THEN printf('root%04d', n) ELSE printf('q44s.%d', n + 20) END,
             CASE WHEN n > 200 THEN 'q44s' END, printf('Fixture ticket %d', n),
             CASE WHEN n <= 20 THEN 'in_progress' WHEN n <= 200 THEN 'open' ELSE 'closed' END,
             'Fixture issue body', '2026-11-15T19:30:00Z', '2026-11-15T19:30:00Z', 'Sim actor', n FROM numbers;
      INSERT INTO tickets (id, parent, title, type, status, labels, body, created_at, updated_at, created_by, version)
      VALUES
        ('q44s', NULL, 'Dashboard epic', 'epic', 'open', '[]', 'Collect all dashboard work',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('q44s.7', 'q44s', 'Render milestone', 'milestone', 'open', '[]', '',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('q44s.7.24', 'q44s.7', 'Render live tickets <safely>', 'task', 'in_progress', '["lane:ui"]',
         'Read by-q44s and q44s.7 for **context**.', '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('q44s.7.24.1', 'q44s.7.24', 'Render detail rows', 'task', 'open', '[]', '',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('erro', NULL, 'Simulator failure', 'bug', 'blocked', '["role:steward"]', '',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('brpt', NULL, 'Filed from the app', 'bug', 'open', '["bug-report"]', '',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('wait', NULL, 'Wait for deployment', 'task', 'deferred', '[]', '',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992),
        ('path', NULL, 'Validate the old path', 'task', 'open', '[]', '',
         '2026-11-16T19:30:00Z', '2026-11-16T19:30:00Z', 'Derek', 4992);
      UPDATE tickets SET assignee = 'local_ff743711' WHERE id = 'q44s.7.24';
      INSERT INTO edges VALUES ('q44s.7.24', 'erro', 'blocks'), ('path', 'q44s.7.24', 'waits-for');
      INSERT INTO aliases VALUES ('old', 'q44s.7.24');
      INSERT INTO comments VALUES ('q44s.7.24', 1, 'Derek', '2026-11-16T20:00:00Z', 'See by-path.');
      INSERT INTO history VALUES ('q44s.7.24', 4993, 'Derek', '2026-11-16T19:30:00Z',
                                  '{"title":["Render tickets","Render live tickets <safely>"]}');
      INSERT INTO history VALUES ('q44s.7.24', 0, 'Derek', '2026-11-16T19:30:00Z',
        '[{"actor":"Derek","created_at":"2026-11-15T19:30:00Z","event_type":"created"}]');
      INSERT INTO meta VALUES ('seq', '4993');
    `);
  }));
}

test("renders project pages from 5,000 tickets and revalidates them", async () => {
  await seed();
  const home = await request("/");
  expect(home.status).toBe(200);
  const homeBody = await home.text();
  expect(homeBody.match(/class="c-card"/g)).toHaveLength(226);
  for (const id of ["active", "all", "epics", "tickets", "automatic", "bug-reports", "deferred"]) {
    expect(homeBody).toContain(`href="#${id}"`);
  }
  expect(homeBody).toContain("Slow CI failures");
  expect(homeBody).toContain('href="/by/tickets/q44s.7.24"');
  expect(homeBody).toContain("4792/4795");

  const detail = await request("/tickets/q44s.7.24");
  expect(detail.status).toBe(200);
  const detailBody = await detail.text();
  for (const value of ["Description", "Comments", "Changelog", "Subtasks", "Blocked by", "Blocking", "Assignee", "Epic",
    "In progress", "Render live tickets &lt;safely&gt;", "session ff7437", "Validate the old path", "Simulator failure",
    '<strong>context</strong>', 'href="/by/tickets/q44s"', 'href="/by/tickets/path"', "renamed it", "created this"]) {
    expect(detailBody).toContain(value);
  }

  const homeTag = home.headers.get("etag")!;
  const detailTag = detail.headers.get("etag")!;
  for (const [path, tag] of [["/", homeTag], ["/tickets/q44s.7.24", detailTag]]) {
    expect(tag).toMatch(/^W\/"[0-9a-f]+"$/);
    const unchanged = await request(path, {headers: {"If-None-Match": `"another", ${tag.slice(2)}`}});
    expect(unchanged.status).toBe(304);
    expect(await unchanged.text()).toBe("");
    expect(unchanged.headers.get("etag")).toBe(tag);
  }

  await runInDurableObject(stub, ({store}) => {
    store.batch("by", "Derek", {ops: [{op: "comment", id: "q44s.7.24", text: "New dashboard comment"}]});
  });
  expect((await request("/", {headers: {"If-None-Match": homeTag}})).status).toBe(304);
  const commented = await request("/tickets/q44s.7.24", {headers: {"If-None-Match": detailTag}});
  expect(commented.status).toBe(200);
  expect(await commented.text()).toContain("New dashboard comment");

  await runInDurableObject(stub, ({store}) => {
    store.batch("by", "Derek", {ops: [{op: "update", id: "erro", set: {title: "Renamed failure"}}]});
  });
  const changed = await request("/tickets/q44s.7.24", {headers: {"If-None-Match": commented.headers.get("etag")!}});
  expect(changed.status).toBe(200);
  expect(await changed.text()).toContain("Renamed failure");

  for (const path of ["/beads/by-q44s.7.24", "/beads/by-q44s.7.24.html", "/tickets/by-q44s.7.24", "/tickets/old", "/tickets/q44s.7.24/"]) {
    const redirect = await request(path + "?from=bookmark", {redirect: "manual"});
    expect(redirect.status).toBe(308);
    expect(redirect.headers.get("location")).toBe("https://example.com/by/tickets/q44s.7.24?from=bookmark");
  }
  const homeBeforeReopen = (await request("/")).headers.get("etag")!;
  await runInDurableObject(stub, ({store}) => {
    store.batch("by", "Derek", {ops: [{op: "reopen", id: "q44s.999"}]});
  });
  const progressed = await request("/", {headers: {"If-None-Match": homeBeforeReopen}});
  expect(progressed.status).toBe(200);
  expect((await progressed.text()).includes("4791/4795")).toBe(true);

  const head = await request("/tickets/q44s.7.24", {method: "HEAD"});
  expect(head.headers.get("etag")).toBe((await request("/tickets/q44s.7.24")).headers.get("etag"));
  expect(await head.text()).toBe("");
  expect((await request("/tickets/missing")).status).toBe(404);

  // Run explicitly because timings in the local simulator depend on host load.
  if (import.meta.env.VITE_DASHBOARD_BUDGET_CHECK === "1") {
    const results = await runInDurableObject(stub, async ({store}) => {
      const samples: Record<string, number[]> = {home: [], coldTicket: [], ticketAfterHome: [], revalidate: []};
      for (let index = 0; index < 11; index++) {
        const dashboard = new Dashboard(store.sql);
        for (const [name, path] of [["home", "/by/"], ["ticketAfterHome", "/by/tickets/q44s.7.24"],
          ["revalidate", "/by/"], ["coldTicket", "/by/tickets/q44s.7.24"]]) {
          const req = new Request("https://example.com" + path, name === "revalidate" ? {headers: {"If-None-Match": progressed.headers.get("etag")!}} : undefined);
          const renderer = name === "coldTicket" ? new Dashboard(store.sql) : dashboard;
          const start = performance.now();
          await renderer.fetch(req, dashboardRoute(path)!);
          samples[name].push(performance.now() - start);
        }
      }
      return samples;
    });
    for (const [name, samples] of Object.entries(results)) {
      const median = samples.sort((left, right) => left - right)[Math.floor(samples.length / 2)];
      console.log(`Dashboard ${name} median ${median.toFixed(2)} ms`);
      expect(median).toBeLessThan(10);
    }
    expect(results.home.some((duration) => duration > 0)).toBe(true);
  }
});
