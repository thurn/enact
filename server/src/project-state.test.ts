import {env, exports} from "cloudflare:workers";
import {runDurableObjectAlarm, runInDurableObject} from "cloudflare:test";
import {expect, test} from "vitest";
import type {Ticket} from "./ticket-types";

async function fixture(project: string) {
  const stub = env.TICKETS.getByName(project);
  const token = "project-state-test-token";
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  const hash = [...new Uint8Array(bytes)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  const sql = <T>(action: (sql: SqlStorage) => T) => runInDurableObject(stub, ({store}) => action(store.sql));
  await sql((db) => { db.exec("INSERT INTO meta VALUES ('token_hashes', ?)", JSON.stringify({[hash]: "test-machine"})); });
  const get = (path: string) => exports.default.fetch(`https://example.com/${project}/api${path}`);
  const post = (path: string, body: unknown, authorization = `Bearer ${token}`) =>
    exports.default.fetch(`https://example.com/${project}/api${path}`, {
      method: "POST", headers: {Authorization: authorization, "X-Actor": "worker"}, body: JSON.stringify(body),
    });
  const json = async <T>(response: Promise<Response>, status = 200): Promise<T> => {
    const reply = await response;
    expect(reply.status, await reply.clone().text()).toBe(status);
    return reply.json<T>();
  };
  return {stub, sql, get, post, json};
}

test("concurrent acquires have one winner and preserve the queue through expiry and release", async () => {
  const {get, post, json, sql} = await fixture("lease-queue");
  const lease = "/leases/vizier%2Fepic%2Floop";
  const take = (holder: string) => post(lease, {op: "acquire", holder, ttl_s: 180, payload: {host: holder}});
  const initial = await Promise.all(Array.from({length: 12}, (_, index) => take(`worker-${index}`)));
  expect(initial.map((response) => response.status).sort()).toEqual([200, ...Array(11).fill(409)]);
  const winner = await initial.find((response) => response.ok)!.json<{holder: string}>();
  const replies = await Promise.all(initial.filter((response) => !response.ok)
    .map((response) => response.json<{holder: string; position: number}>()));
  expect(replies.map(({position}) => position).sort((a, b) => a - b)).toEqual(Array.from({length: 11}, (_, index) => index + 1));
  expect(replies.every(({holder}) => holder === winner.holder)).toBe(true);
  const [listed] = await json<{name: string; waiters: {holder: string}[]}[]>(get("/leases"));
  expect(listed.name).toBe("vizier/epic/loop");
  const [first, second, third] = listed.waiters;
  expect(new Set(listed.waiters.map(({holder}) => holder)).size).toBe(11);
  expect(await json(take(first.holder), 409)).toMatchObject({position: 1});

  for (const op of ["renew", "release"]) {
    expect(await json(post(lease, {op, holder: first.holder, ttl_s: 100}), 409))
      .toMatchObject({holder: winner.holder});
  }
  expect(await json(post(lease, {op: "renew", holder: winner.holder, ttl_s: 180})))
    .toMatchObject({holder: winner.holder, machine: "test-machine", payload: {host: winner.holder}});

  await sql((db) => { db.exec("UPDATE leases SET expires_at = '2000-01-01T00:00:00.000Z'"); });
  expect(await json(post(lease, {op: "renew", holder: winner.holder, ttl_s: 180}), 409))
    .toEqual({holder: null, expires_at: null});
  expect(await json(take(second.holder), 409)).toMatchObject({position: 2});
  expect(await json(take(first.holder))).toMatchObject({holder: first.holder});
  expect(await json(post(lease, {op: "release", holder: first.holder}))).toMatchObject({holder: first.holder});
  expect(await json(take(third.holder), 409)).toMatchObject({position: 2});
  expect(await json(take(second.holder))).toMatchObject({holder: second.holder});
});

test("expired and abandoned waiters do not block an acquire", async () => {
  const {get, post, json, sql} = await fixture("stale-waiters");
  const lease = "/leases/staging";
  const take = (holder: string) => post(lease, {op: "acquire", holder, ttl_s: 180});
  await json(take("owner"));
  await json(take("gone"), 409);
  await json(take("next"), 409);
  await sql((db) => {
    const past = "2000-01-01T00:00:00.000Z";
    db.exec("UPDATE leases SET expires_at = ?, waiters = json_set(waiters, '$[0].since', ?)", past, past);
  });
  const [listed] = await json<{waiters: {holder: string}[]}[]>(get("/leases"));
  expect(listed.waiters.map(({holder}) => holder)).toEqual(["next"]);
  expect(await json(take("next"))).toMatchObject({holder: "next", waiters: []});
  await json(post(lease, {op: "release", holder: "next"}));
  expect(await json(get("/leases"))).toEqual([]);
});

test("cutover follows the pause and commit transitions and advances the shared sequence", async () => {
  const {get, post, json} = await fixture("cutover-transitions");
  const set = (cutover: string) => post("/state", {cutover});
  expect(await json(get("/state"))).toMatchObject({cutover: "before", cutover_at: null, seq: 0});
  expect((await set("after")).status).toBe(400);
  const pause = await json<{cutover: string; cutover_at: string; seq: number}>(set("paused"));
  expect(pause).toMatchObject({cutover: "paused", seq: 1});
  expect(Date.parse(pause.cutover_at)).not.toBeNaN();
  expect(await json(set("paused"))).toMatchObject({cutover: "paused", cutover_at: pause.cutover_at, seq: 1});
  expect(await json(set("before"))).toMatchObject({cutover: "before", cutover_at: null, seq: 2});
  const retry = await json<{cutover_at: string}>(set("paused"));
  expect(await json(set("after"))).toMatchObject({cutover: "after", cutover_at: retry.cutover_at, seq: 4});
  for (const cutover of ["before", "paused", "invalid"]) expect((await set(cutover)).status).toBe(400);
  expect(await json(get("/graph"))).toMatchObject({seq: 4});
  expect((await post("/state", {cutover: "after"}, "Bearer unknown")).status).toBe(401);
});

test("events use the authenticated identity, support filtered cursors, and expire on the daily alarm", async () => {
  const {stub, get, post, json, sql} = await fixture("events");
  const first = await json(post("/events", {kind: "machine", data: {drained: true}, machine: "forged"}));
  expect(first).toMatchObject({seq: 1, kind: "machine", data: {drained: true}, machine: "test-machine", actor: "worker"});
  const second = await json(post("/events", {kind: "session", data: null}));
  const third = await json(post("/events", {kind: "machine", data: ["ready"]}));
  expect(await json(get("/events?kind=machine&limit=1"))).toEqual([third]);
  expect(await json(get("/events?since=1&limit=1"))).toEqual([second]);
  expect(await json(get("/events?since=1&kind=machine"))).toEqual([third]);
  expect((await get("/events?since=-1")).status).toBe(400);
  expect((await post("/events", {kind: "usage"})).status).toBe(400);
  expect((await post("/events", {kind: "unknown", data: 0})).status).toBe(400);
  await sql((db) => { db.exec("UPDATE events SET at = '2000-01-01T00:00:00.000Z' WHERE seq = 1"); });
  expect(await runDurableObjectAlarm(stub)).toBe(true);
  expect(await json(get("/events?since=0"))).toEqual([second, third]);
  expect(await runInDurableObject(stub, (_object, state) => state.storage.getAlarm())).not.toBeNull();
});

test("counts committed rows, rejects quota-breaking batches atomically, and resets by UTC day", async () => {
  const {get, post, json, sql} = await fixture("daily-quota");
  const batch = (title: string) => post("/batch", {ops: [{op: "create", fields: {title}, request_key: title}]});
  expect(await json(get("/state"))).toMatchObject({requests: 1, rows_written: 1, limits: {requests: 100_000, rows_written: 100_000}});
  await json(batch("first"));
  await json(post("/leases/staging", {op: "acquire", holder: "worker", ttl_s: 180}));
  // The create writes five data rows including the sequence. The lease
  // writes two. Both changes write one feed row, and every request writes
  // one usage row.
  expect(await json(get("/state"))).toMatchObject({requests: 4, rows_written: 13, seq: 2});
  const seed = (rows: number, requests: number, day: string) => sql((db) => {
    db.exec("UPDATE meta SET value = ? WHERE key = 'daily_usage'", JSON.stringify({rows_written: rows, requests, day}));
  });
  const today = new Date().toISOString().slice(0, 10);
  await seed(89_994, 4, today);
  expect(await json(batch("rolled back"), 429)).toEqual({limit: "rows_written"});
  expect(await json<Ticket[]>(get("/tickets"))).toHaveLength(1);
  expect(await json(get("/state"))).toMatchObject({requests: 7, rows_written: 89_997, seq: 2});
  await seed(89_996, 7, today);
  // A lease release can use the last four rows, including the feed row.
  await json(post("/leases/staging", {op: "release", holder: "worker"}));
  expect(await json(get("/state"))).toMatchObject({rows_written: 90_000, seq: 3});
  expect(await json(batch("at limit"), 429)).toEqual({limit: "rows_written"});
  await seed(100, 90_000, today);
  expect(await json(batch("requests exhausted"), 429)).toEqual({limit: "requests"});
  await seed(90_000, 90_000, "2000-01-01");
  expect(await json(get("/state"))).toMatchObject({day: today, requests: 1, rows_written: 1, seq: 3});
  await json(batch("new day"));
});

async function readNdjson(response: Response): Promise<string> {
  return new TextDecoder().decode(await response.arrayBuffer());
}

function imported(id: string, overrides: object = {}) {
  return {
    id, parent: null, next_child: 1, title: id, body: "", design: "", acceptance: "", notes: "",
    type: "task", status: "open", priority: 2, assignee: null, last: 0, labels: [], metadata: {},
    defer_until: null, close_reason: null, created_at: "2026-01-01T00:00:00Z", created_by: "legacy",
    updated_at: "2026-01-01T00:00:00Z", closed_at: null, edges: [], comments: [], history: [], aliases: [], ...overrides,
  };
}

test("whole ticket snapshots export, replay idempotently, and reconcile changed source data", async () => {
  const {get, post, json} = await fixture("transfer");
  const at = "2026-01-01T00:00:00Z";
  const parent = imported("parent", {type: "epic", next_child: 4, aliases: ["transfer-old"]});
  const child = imported("parent.3", {
    parent: "parent", edges: [{src: "parent.3", dst: "other", kind: "related"}],
    comments: [{ticket: "parent.3", n: 1, author: "legacy", at, text: "A legacy comment", legacy_id: "27e9a640-1e3d-5519-abfd-16080ed75930"}],
    history: [{ticket: "parent.3", seq: 0, actor: "legacy", at, change: [{migration: "legacy history"}]}],
  });
  const other = imported("other", {status: "closed", closed_at: at});
  const send = (...tickets: object[]) => post("/import", {tickets});
  const first = await json<{seq: number; rows_written: number; imported: number}>(send(child, parent, other));
  expect(first).toMatchObject({seq: 1, imported: 3});
  expect(first.rows_written).toBeGreaterThan(3);
  expect(await json(send(child, parent, other))).toEqual({seq: 1, imported: 0, rows_written: 0});
  expect(await json(get("/state"))).toMatchObject({seq: 1, requests: 3, rows_written: first.rows_written + 4});
  expect(await json(get("/tickets/transfer-old"))).toMatchObject({id: "parent", children: ["parent.3"]});
  expect(await json(get("/tickets/parent.3?comments=1&history=1"))).toMatchObject({
    id: child.id, edges: child.edges, comments: child.comments, history: child.history,
  });

  const exported = await get("/export?after=other");
  expect(exported.headers.get("Content-Type")).toBe("application/x-ndjson");
  const lines = (await readNdjson(exported)).trim().split("\n").map((line) => JSON.parse(line));
  expect(lines).toEqual([{...parent, version: 1}, {...child, version: 1}]);
  expect(await json(send(...lines))).toEqual({seq: 1, imported: 0, rows_written: 0});
  expect(await readNdjson(await get("/export?after=parent.3"))).toBe("");

  const originalEtag = (await get("/graph")).headers.get("ETag");
  const delta = {...child, title: "Reconciled", edges: [], updated_at: "2026-01-02T00:00:00Z"};
  expect(await json(send(delta))).toMatchObject({seq: 2, imported: 1});
  expect(await json(get("/tickets/parent.3"))).toMatchObject({title: "Reconciled", edges: [], version: 2});
  expect((await get("/graph")).headers.get("ETag")).not.toBe(originalEtag);
  expect(await json(send(child))).toEqual({seq: 2, imported: 0, rows_written: 0});
  await json(send({...parent, next_child: 2}));
  expect(await json(post("/batch", {ops: [{op: "create", parent: "parent", fields: {title: "New child"}}]})))
    .toMatchObject({results: [{id: "parent.4"}]});
  expect((await send(imported("good"), imported("cycle", {parent: "cycle"}))).status).toBe(400);
  expect((await get("/tickets/good")).status).toBe(404);
  for (const alias of ["parent", "transfer-parent"]) {
    expect((await send(imported("bad", {aliases: [alias]}))).status).toBe(400);
  }
  expect((await send(...Array.from({length: 201}, (_, n) => imported(`row${n}`)))).status).toBe(400);
});

test("honors an import ceiling atomically and resumes on a later UTC day", async () => {
  const {get, post, json, stub} = await fixture("migration-budget");
  const archived = imported("ci-deadbeef", {status: "closed", aliases: ["by-ci-deadbeef"]});
  const active = imported("active");
  const send = (tickets: object[], daily_row_ceiling: number) => post("/import", {tickets, daily_row_ceiling});
  expect(await json(send([archived], 6))).toMatchObject({seq: 1, imported: 1});
  const limited = await send([active], 6);
  expect(limited.status).toBe(429);
  expect(await limited.json()).toEqual({limit: "rows_written"});
  expect((await get("/tickets/active")).status).toBe(404);
  await runInDurableObject(stub, ({store}) => {
    store.sql.exec("UPDATE meta SET value = json_set(value, '$.day', '2000-01-01') WHERE key = 'daily_usage'");
  });
  expect(await json(send([archived, active], 6))).toMatchObject({seq: 2, imported: 1});
  expect(await readNdjson(await get("/export?after=ci-deadbeef"))).toBe("");
  expect(await json(get("/tickets/by-ci-deadbeef"))).toMatchObject({id: "ci-deadbeef", status: "closed"});
});

test("exports stop at the page boundary and resume after its final canonical ID", async () => {
  const {post, get, json} = await fixture("export-pages");
  const rows = Array.from({length: 201}, (_, n) => imported(`row${String(n).padStart(3, "0")}`));
  await json(post("/import", {tickets: rows.slice(0, 200)}));
  await json(post("/import", {tickets: rows.slice(200)}));
  const page = async (cursor = "") => (await readNdjson(await get(`/export${cursor}`)))
    .split("\n").filter(Boolean).map((line) => JSON.parse(line).id);
  expect(await page()).toEqual(rows.slice(0, 200).map(({id}) => id));
  expect(await page("?after=row199")).toEqual(["row200"]);
  expect(await page("?after=row200")).toEqual([]);
});

test("restoring an exported native history reserves its sequence before the next update", async () => {
  const {post, get, json} = await fixture("restore-seq");
  const at = "2026-01-01T00:00:00Z";
  await json(post("/import", {tickets: [imported("restored", {
    history: [{ticket: "restored", seq: 100, actor: "worker", at, change: {title: ["old", "restored"]}}],
  })]}));
  expect(await json(post("/batch", {ops: [{op: "update", id: "restored", set: {title: "New title"}}]})))
    .toMatchObject({seq: 102});
  expect(await json(get("/tickets/restored?history=1"))).toMatchObject({
    history: [{seq: 100}, {seq: 102}],
  });
});

test("late import cannot overwrite native comments or dependency removals", async () => {
  const {post, json, get, sql} = await fixture("late-import");
  const src = imported("source", {edges: [{src: "source", dst: "target", kind: "blocks"}]});
  const target = imported("target");
  const send = (...tickets: object[]) => post("/import", {tickets});
  await json(send(src, target));
  await json(post("/state", {cutover: "paused"}));
  await json(post("/state", {cutover: "after"}));
  await json(post("/batch", {ops: [
    {op: "comment", id: "source", text: "Native comment"},
    {op: "dep_remove", id: "source", on: "target"},
  ]}));

  const late = {...src, updated_at: "2040-01-01T00:00:00Z", title: "Late title"};
  expect(await json(send(late, imported("newticket")), 409)).toMatchObject({conflicts: ["source"]});
  expect(await json(get("/tickets/source?comments=1"))).toMatchObject({
    title: "source", edges: [], comments: [{text: "Native comment"}],
  });
  expect((await get("/tickets/newticket")).status).toBe(404);
  expect(await json(send({...target, title: "Late update"}, imported("newticket"))))
    .toMatchObject({imported: 2});

  await sql((db) => { db.exec("DELETE FROM feed_changes"); });
  expect(await json(send({...target, title: "Unverified replacement"}), 409))
    .toMatchObject({conflicts: ["target"]});
  expect(await json(get("/tickets/target"))).toMatchObject({title: "Late update"});
  expect(await json(send(imported("anothernew")))).toMatchObject({imported: 1});
});

test("coordination changes invalidate a cached graph's batch precondition", async () => {
  const {get, post, json} = await fixture("graph-coordination-seq");
  const {results: [{id}]} = await json<{results: {id: string}[]}>(post("/batch", {
    ops: [{op: "create", fields: {title: "Available"}}],
  }));
  const first = await json<{seq: number}>(get("/graph?all=1"));
  const event = await json<{seq: number}>(post("/events", {kind: "session", data: {state: "started"}}));
  expect(event.seq).toBeGreaterThan(first.seq);
  const changed = await json<{seq: number}>(get("/graph?all=1"));
  expect(changed.seq).toBe(event.seq);
  await json(post("/batch", {if: [{seq: changed.seq}], ops: [{op: "claim", id}]}));
});

test("lease, event and cutover operations stay within their SQLite row budgets", async () => {
  const {get, post, json, sql} = await fixture("coordination-row-budget");
  const countedRows = () => sql((db) => db.exec<{value: string}>(
    "SELECT value FROM meta WHERE key = 'daily_usage'",
  ).toArray().map(({value}) => JSON.parse(value).rows_written as number)[0] ?? 0);
  const check = async (request: () => Promise<Response>, maximum: number, status = 200) => {
    const before = await countedRows();
    await json(request(), status);
    expect(await countedRows() - before).toBeLessThanOrEqual(maximum);
  };
  const lease = (op: string, holder: string) =>
    post("/leases/staging", {op, holder, ttl_s: 180});
  await check(() => get("/state"), 1);
  await check(() => lease("acquire", "owner"), 4);
  // A new staging waiter adds a sequence and a journal row for the dashboard.
  await check(() => lease("acquire", "waiter"), 4, 409);
  await check(() => lease("acquire", "waiter"), 2, 409);
  await check(() => lease("renew", "owner"), 4);
  await check(() => lease("release", "owner"), 4);
  await check(() => post("/events", {kind: "ci", data: {result: "passed"}}), 5);
  await check(() => post("/state", {cutover: "paused"}), 5);
  await check(() => post("/state", {cutover: "after"}), 5);
});

test("machines with the same holder name have separate queue positions", async () => {
  const {post, json, sql} = await fixture("lease-machine");
  const token = "another-machine";
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  const hash = [...new Uint8Array(bytes)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  await sql((db) => {
    db.exec("UPDATE meta SET value = json_patch(value, ?) WHERE key = 'token_hashes'",
      JSON.stringify({[hash]: token}));
  });
  const acquire = (holder: string, authorization?: string) =>
    post("/leases/staging", {op: "acquire", holder, ttl_s: 300}, authorization);
  await json(acquire("owner"));
  expect(await json(acquire("integrator"), 409)).toMatchObject({position: 1});
  expect(await json(acquire("integrator", `Bearer ${token}`), 409)).toMatchObject({position: 2});
  await json(post("/leases/staging", {op: "release", holder: "owner"}));
  expect(await json(acquire("integrator", `Bearer ${token}`), 409)).toMatchObject({position: 2});
  expect(await json(acquire("integrator"))).toMatchObject({machine: "test-machine"});
});
