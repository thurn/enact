/// <reference types="vite/client" />

import {env, exports} from "cloudflare:workers";
import {evictDurableObject, runDurableObjectAlarm, runInDurableObject} from "cloudflare:test";
import {expect, test} from "vitest";
import {ChangeFeed} from "./change-feed";
import {ApiError, type Blocker, type Edge, type Ticket} from "./ticket-types";

type ShownTicket = Ticket & {
  ready: boolean;
  blocked_by: Blocker[];
  children: string[];
  edges: Edge[];
  comments?: {n: number; author: string; text: string}[];
  history?: {seq: number; change: Record<string, unknown>}[];
};
type BatchReply = {seq: number; results: {id: string; closed?: string[]; ready?: string[]}[]};

const bearer = "ticket-store-test-token";

async function fixture(project: string) {
  const stub = env.TICKETS.getByName(project);
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(bearer));
  const hash = [...new Uint8Array(bytes)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  await runInDurableObject(stub, ({store}) => {
    store.sql.exec("INSERT INTO meta VALUES ('token_hashes', ?)", JSON.stringify([hash]));
  });
  const request = (path: string, init?: RequestInit) => exports.default.fetch(`https://example.com/${project}/api${path}`, init);
  const write = (path: string, body: object, actor = "worker") => request(path, {
    method: "POST",
    headers: {Authorization: `Bearer ${bearer}`, "X-Actor": actor},
    body: JSON.stringify(body),
  });
  const post = (ops: object[], conditions: object[] = [], actor = "worker") =>
    write("/batch", {if: conditions, ops}, actor);
  const checked = async <T>(pending: Promise<Response>): Promise<T> => {
    const response = await pending;
    expect(response.status, await response.clone().text()).toBe(200);
    return response.json<T>();
  };
  const batch = (ops: object[], conditions?: object[]) => checked<BatchReply>(post(ops, conditions));
  const show = (id: string, query = "") => checked<ShownTicket>(request(`/tickets/${id}${query}`));
  const list = (query = "") => checked<ShownTicket[]>(request(`/tickets${query}`));
  return {stub, request, write, post, batch, show, list, checked};
}

function create(title: string, parent?: string, fields: object = {}, extra: object = {}) {
  return {op: "create", fields: {title, ...fields}, parent, ...extra};
}

test("projects expose public reads and reject writes without a registered token", async () => {
  const {request, batch} = await fixture("public-project");
  const second = await fixture("second-project");
  expect((await request("/graph")).status).toBe(200);
  expect((await request("/tickets/missing")).status).toBe(404);
  expect((await request("/tickets/%ff")).status).toBe(400);
  expect((await request("/tickets?limit=-1")).status).toBe(400);
  expect((await request("/tickets?status=nope")).status).toBe(400);
  expect((await request("/unknown")).status).toBe(404);
  for (const authorization of ["", "Bearer unknown"]) {
    const response = await request("/batch", {method: "POST", headers: {authorization}, body: '{"ops":[]}'});
    expect(response.status).toBe(401);
  }
  const response = await request("/batch", {
    method: "POST", headers: {Authorization: `Bearer ${bearer}`, "X-Actor": "worker"}, body: "{",
  });
  expect(response.status).toBe(400);
  const {results: [{id}]} = await batch([create("Only here")]);
  expect((await second.request(`/tickets/${id}`)).status).toBe(404);
});

test("creates nested IDs, resolves aliases, and lists using every filter", async () => {
  const {stub, batch, show, list, request, checked} = await fixture("lookup");
  const {seq, results} = await batch([
    create("Epic", undefined, {type: "epic", body: "long body", design: "proposal", notes: "notes"}),
    create("Milestone", "$0", {type: "milestone"}),
    create("Assigned bug", "$1", {type: "bug", assignee: "dev", labels: ["engine", "urgent"]}, {deps: [{on: "$0", kind: "related"}]}),
    create("Closed task", "$0", {status: "closed"}),
  ]);
  const [epic, milestone, bug, archived] = results.map(({id}) => id);
  expect(epic).toMatch(/^[abcdefghjkmnpqrstvwxyz][0-9abcdefghjkmnpqrstvwxyz]{3}$/);
  expect(milestone).toBe(`${epic}.1`);
  expect(bug).toBe(`${milestone}.1`);
  expect(archived).toBe(`${epic}.2`);

  await runInDurableObject(stub, ({store}) => {
    store.sql.exec("INSERT INTO aliases VALUES ('old', ?), ('lookup-legacy', ?)", epic, bug);
    store.invalidateIndex();
  });
  const shown = await show("lookup-old");
  expect(shown).toMatchObject({id: epic, children: [milestone, archived], body: "long body"});
  expect(shown.edges).toEqual([{src: bug, dst: epic, kind: "related"}]);
  expect(shown).not.toHaveProperty("comments");
  expect(shown).not.toHaveProperty("history");
  expect((await show("lookup-legacy")).id).toBe(bug);
  expect((await show(`lookup-${bug}`)).id).toBe(bug);
  expect((await list()).map(({id}) => id)).toEqual([epic, milestone, bug]);
  expect((await list("?ancestor=lookup-old")).map(({id}) => id)).toEqual([milestone, bug]);
  expect((await list("?all=1&ancestor=lookup-old")).map(({id}) => id)).toEqual([milestone, bug, archived]);
  expect((await list("?parent=lookup-old")).map(({id}) => id)).toEqual([milestone]);
  expect((await list("?status=closed")).map(({id}) => id)).toEqual([archived]);
  expect((await list("?all=1&limit=1")).map(({id}) => id)).toEqual([epic]);
  expect(await list("?limit=0")).toEqual([]);
  expect(await list("?label=engine&label=missing")).toEqual([]);
  const filtered = await list("?ready=1&type=bug&status=open&label=engine&label=urgent&assignee=dev");
  expect(filtered.map(({id}) => id)).toEqual([bug]);

  const response = await request("/graph");
  const etag = response.headers.get("ETag");
  expect(etag).toBeTruthy();
  const graph = await checked<{seq: number; tickets: Partial<Ticket>[]; edges: Edge[]}>(Promise.resolve(response));
  expect(graph.seq).toBe(seq);
  expect(graph.tickets.map(({id}) => id)).toEqual([epic, milestone, bug]);
  for (const field of ["body", "design", "notes"]) expect(graph.tickets[0]).not.toHaveProperty(field);
  expect(graph.edges).toEqual(shown.edges);
  const unchanged = await request("/graph", {headers: {"If-None-Match": etag!}});
  expect(unchanged.status).toBe(304);
  expect(await unchanged.text()).toBe("");
  await batch([{op: "update", id: bug, set: {title: "Changed"}}]);
  expect((await request("/graph", {headers: {"If-None-Match": etag!}})).status).toBe(200);
});

test("reports each readiness rule across ancestors, late findings and parallel audits", async () => {
  const {stub, batch, show, list, request, checked} = await fixture("ready");
  const {results} = await batch([
    create("Milestone", undefined, {type: "milestone"}),
    create("Work", "$0"),
    create("Audit A", "$0", {last: true}),
    create("Audit B", "$0", {last: 1}),
    create("Release", undefined, {}, {deps: [{on: "$0", kind: "waits-for"}]}),
    create("External blocker"),
    {op: "dep_add", id: "$0", on: "$5", kind: "conditional-blocks"},
    {op: "dep_add", id: "$1", on: "$5"},
  ]);
  const [milestone, work, first, second, release, external] = results.map(({id}) => id);
  expect((await show(work)).blocked_by).toEqual([{id: external, rule: 2}, {id: external, rule: 3}]);
  expect((await show(first)).blocked_by).toEqual([{id: external, rule: 3}, {id: work, rule: 4}]);
  expect((await show(milestone)).blocked_by).toContainEqual({id: work, rule: 5});
  expect((await list()).map(({id}) => id)).toContain(release);
  expect((await list("?ready=1")).map(({id}) => id)).toEqual([external]);

  await batch([{op: "close", id: external, reason: "Fixed"}, {op: "close", id: work, reason: "Done"}]);
  expect((await show(first)).ready).toBe(true);
  expect((await show(second)).ready).toBe(true);
  await batch([{op: "claim", id: first}]);
  expect((await show(first)).blocked_by).toEqual([{id: first, rule: 1}]);
  expect((await show(second)).ready).toBe(true);
  const {results: [{id: finding}]} = await batch([create("Late finding", milestone)]);
  expect((await show(second)).blocked_by).toEqual([{id: finding, rule: 4}]);

  await batch([{op: "defer", id: finding, until: "2999-03-01"}, {op: "reopen", id: finding}]);
  expect((await show(finding)).blocked_by).toEqual([{id: finding, rule: 1}]);
  await batch([{op: "update", id: finding, set: {defer_until: "2000-03-01"}}]);
  expect((await show(finding)).ready).toBe(true);
  await batch([{op: "close", id: milestone, reason: "Closed explicitly"}]);
  await runInDurableObject(stub, ({store}) => store.invalidateIndex());
  expect((await show(release)).blocked_by).toEqual([{id: milestone, rule: 2}]);
  const graph = await checked<{tickets: ShownTicket[]; edges: Edge[]}>(request("/graph"));
  expect(graph.edges.some(({src, dst}) => src === release && dst === milestone)).toBe(false);
  expect(graph.tickets.find(({id}) => id === release)?.blocked_by).toEqual([{id: milestone, rule: 2}]);
  await batch([{op: "reopen", id: external}]);
  const inherited = await checked<{tickets: ShownTicket[]}>(request("/graph"));
  expect(inherited.tickets.find(({id}) => id === second)?.blocked_by).toEqual([
    {id: external, rule: 3}, {id: finding, rule: 4},
  ]);
  await batch([{op: "close", id: external, reason: "Fixed"}]);

  await batch([{op: "close", id: finding, reason: "Fixed"}, {op: "close", id: first, reason: "Done"}]);
  expect((await show(second)).ready).toBe(true);
  await batch([{op: "close", id: second, reason: "Done"}]);
  expect((await show(release)).ready).toBe(true);
  const {results: [{id: lateFinding}]} = await batch([create("Another finding", milestone)]);
  expect((await show(release)).blocked_by).toEqual([{id: milestone, rule: 2}]);
  await batch([{op: "close", id: lateFinding, reason: "Fixed"}]);
  expect((await show(release)).ready).toBe(true);
});

test("runs milestone audits and the Archivist as last tickets", async () => {
  const {batch, list, show} = await fixture("milestone-plan");
  const {results} = await batch([
    create("Run", undefined, {type: "epic"}),
    create("Milestone 1", "$0", {type: "epic"}),
    create("Milestone 2", "$0", {type: "epic"}, {deps: ["$1"]}),
    create("Build", "$1"),
    create("Mason", "$1", {last: true, labels: ["role:mason"]}),
    create("Ship", "$2"),
    create("Archivist", "$0", {last: true, labels: ["role:archivist"]}),
  ]);
  const [run, first, second, build, mason, ship, archivist] = results.map(({id}) => id);
  const ready = async () => (await list(`?ready=1&ancestor=${run}`)).map(({id}) => id);

  expect(await ready()).toEqual([build]);
  await batch([{op: "close", id: build, reason: "Built"}]);
  expect(await ready()).toEqual([mason]);
  await batch([{op: "claim", id: mason}]);
  const {results: [{id: finding}]} = await batch([create("Fix audit finding", first)]);
  await batch([{op: "close", id: mason, reason: "Finding filed"}]);
  expect(await ready()).toEqual([finding]);
  expect((await show(first)).status).toBe("open");

  const {results: [fixed]} = await batch([{op: "close", id: finding, reason: "Fixed"}]);
  expect(fixed.closed).toEqual([first]);
  expect(await ready()).toEqual([ship]);
  const {results: [shipped]} = await batch([{op: "close", id: ship, reason: "Shipped"}]);
  expect(shipped.closed).toEqual([second]);
  expect(await ready()).toEqual([archivist]);
  const {results: [documented]} = await batch([{op: "close", id: archivist, reason: "Documented"}]);
  expect(documented.closed).toEqual([run]);
  expect(await ready()).toEqual([]);
});

test("updates fields and history and closes every now-empty ancestor", async () => {
  const {batch, show} = await fixture("operations");
  const {results} = await batch([create("Epic"), create("Milestone", "$0"), create("Task", "$1"), create("Peer")]);
  const [epic, milestone, task, peer] = results.map(({id}) => id);
  await batch([
    {op: "update", id: task, set: {title: "Revised", labels: ["old"], metadata: {discard: 1}}, add_labels: ["new"],
      remove_labels: ["old"], set_meta: {kept: {score: 3}}, unset_meta: ["discard"]},
    {op: "comment", id: task, text: "first"},
    {op: "comment", id: task, text: "second"},
    {op: "dep_add", id: task, on: peer},
  ]);
  expect((await show(task)).ready).toBe(false);
  await batch([{op: "dep_remove", id: task, on: peer, kind: "related"}]);
  expect((await show(task)).ready).toBe(false);
  await batch([{op: "dep_remove", id: task, on: peer}]);
  const shown = await show(task, "?comments=1&history=1");
  expect(shown).toMatchObject({title: "Revised", labels: ["new"], metadata: {kept: {score: 3}}, ready: true, edges: []});
  expect(shown.comments).toMatchObject([{n: 1, author: "worker", text: "first"}, {n: 2, author: "worker", text: "second"}]);
  expect(shown.history?.[1].change.title).toEqual(["Task", "Revised"]);
  await batch([{op: "defer", id: task, until: "2999-03-01T00:00:00Z"}]);
  expect(await show(task)).toMatchObject({status: "open", ready: false});
  await batch([{op: "reopen", id: task}, {op: "update", id: task, set: {defer_until: null}}]);
  expect((await show(task)).ready).toBe(true);
  const {results: [, closed]} = await batch([
    {op: "dep_add", id: peer, on: task},
    {op: "close", id: task, reason: "Delivered", include_ready: true},
    {op: "claim", id: peer},
  ]);
  expect(closed).toEqual({id: task, closed: [milestone, epic], ready: [peer]});
  expect(await show(epic)).toMatchObject({status: "closed", close_reason: "Delivered"});
  await batch([{op: "reopen", id: task}]);
  expect(await show(task)).toMatchObject({status: "open", closed_at: null, close_reason: null});
});

test("the all-ticket graph keeps closed assignments and updates the archive on writes", async () => {
  const {request, checked, batch} = await fixture("archive");
  const {results: [{id: live}, {id: archived}]} = await batch([
    create("Live"),
    create("Closed", undefined, {status: "closed", assignee: "old-worker", notes: "long notes"}, {deps: ["$0"]}),
    {op: "dep_add", id: "$0", on: "$1"},
  ]);
  const all = () => checked<{tickets: ShownTicket[]; edges: Edge[]}>(request("/graph?all=1"));
  const first = await all();
  const saved = first.tickets.find(({id}) => id === archived);
  expect(saved).toMatchObject({status: "closed", assignee: "old-worker", ready: false});
  expect(saved).not.toHaveProperty("notes");
  expect(first.tickets.find(({id}) => id === live)?.ready).toBe(true);
  expect(first.edges).toEqual(expect.arrayContaining([
    {src: archived, dst: live, kind: "blocks"}, {src: live, dst: archived, kind: "blocks"},
  ]));
  const etag = (await request("/graph?all=1")).headers.get("ETag")!;
  expect((await request("/graph?all=1", {headers: {"If-None-Match": etag}})).status).toBe(304);
  expect((await request("/graph", {headers: {"If-None-Match": etag}})).status).toBe(200);
  const other = await fixture("other-archive");
  await other.batch([create("Live"), {op: "close", id: "$0", reason: "Done"}, create("Another")]);
  expect((await other.request("/graph?all=1", {headers: {"If-None-Match": etag}})).status).toBe(200);

  await batch([{op: "update", id: archived, set: {assignee: "new-worker"}}, {op: "dep_remove", id: archived, on: live}]);
  const updated = await all();
  expect(updated.tickets.find(({id}) => id === archived)?.assignee).toBe("new-worker");
  expect(updated.edges).toEqual([{src: live, dst: archived, kind: "blocks"}]);
  await batch([{op: "reopen", id: archived}]);
  const reopened = await all();
  expect(reopened.tickets.filter(({id}) => id === archived)).toHaveLength(1);
  expect(reopened.tickets.find(({id}) => id === live)?.ready).toBe(false);
  await batch([{op: "close", id: live, reason: "Done"}]);
  expect((await all()).edges).toEqual([{src: live, dst: archived, kind: "blocks"}]);
});

test("a defer time passing invalidates graph readiness at the same sequence", async () => {
  const {stub, batch} = await fixture("timed-readiness");
  const deadline = "2999-01-01T00:00:00Z";
  const {results: [{id}]} = await batch([create("Later")]);
  const {seq} = await batch([{op: "defer", id, until: deadline}]);
  const at = Date.parse(deadline);
  const result = await runInDurableObject(stub, async ({store}) => {
    const first = store.tickets.graph("timed-readiness", null, false, at);
    const tag = first.headers.get("ETag");
    const unchanged = store.tickets.graph("timed-readiness", tag, false, at);
    const after = store.tickets.graph("timed-readiness", tag, false, at + 1);
    return {
      before: await first.json(), after: await after.json(),
      stable: unchanged.status, tags: [tag, after.headers.get("ETag")],
    };
  });
  expect(result.before).toMatchObject({seq, tickets: [{ready: false}]});
  expect(result.after).toMatchObject({seq, tickets: [{ready: true, blocked_by: []}]});
  expect(result.stable).toBe(304);
  expect(result.tags[0]).not.toBe(result.tags[1]);
});

test("reparenting a live subtree updates readiness and rejects ancestry cycles", async () => {
  const {batch, post, show, list} = await fixture("parent-update");
  const {results} = await batch([
    create("Former parent"), create("Current parent"), create("Container", "$0"), create("Task", "$2"),
    create("External"), {op: "dep_add", id: "$1", on: "$4"},
  ]);
  const [old, current, container, task, external] = results.map(({id}) => id);
  expect((await show(task)).ready).toBe(true);
  await batch([{op: "update", id: container, set: {parent: current}}]);
  expect((await list(`?ancestor=${current}`)).map(({id}) => id)).toEqual([container, task]);
  expect((await show(task)).blocked_by).toEqual([{id: external, rule: 3}]);
  expect((await show(old)).children).toEqual([]);
  expect((await post([{op: "update", id: current, set: {parent: task}}])).status).toBe(400);
  expect((await show(current)).parent).toBeNull();
});

test("checks each precondition and lets only one simultaneous claimant win", async () => {
  const {batch, post, show} = await fixture("claims");
  const {seq, results: [{id}]} = await batch([create("Claimable")]);
  const ticket = await show(id);
  const valid = [{seq}, {id, version: ticket.version}, {id, assignee: null}, {id, status: "open"}];
  for (const [index, invalid] of [{seq: seq + 1}, {id, version: seq + 1}, {id, assignee: "other"}, {id, status: "blocked"}].entries()) {
    const conditions = [...valid];
    conditions[index] = invalid as typeof conditions[number];
    const failure = await post([{op: "claim", id}], conditions);
    expect(failure.status).toBe(409);
    expect(await failure.json()).toMatchObject({failed: index, current: index === 0 ? null : {id, status: "open"}});
  }
  expect((await post([], [{id, status: "open", version: 1}])).status).toBe(400);
  const results = await Promise.all(["first", "second"].map((actor) => post([{op: "claim", id}], valid, actor)));
  expect(results.map(({status}) => status).sort()).toEqual([200, 409]);
  const winner = results[0].ok ? "first" : "second";
  expect(await show(id)).toMatchObject({assignee: winner, status: "in_progress"});
  const lost = await post([{op: "claim", id}]);
  expect(lost.status).toBe(409);
  expect(await lost.json()).toMatchObject({failed: 0, current: {id, assignee: winner}});
});

test("rolls back SQL, allocated IDs and the cached graph, and deduplicates creates after a reload", async () => {
  const {stub, request, batch, post, show} = await fixture("rollback");
  const {seq, results: [{id: parent}, {id: claimed}]} = await batch([
    create("Parent"), create("Already claimed", undefined, {assignee: "other"}),
  ]);
  const before = await (await request("/graph")).text();
  const failed = await post([
    create("Child", parent, {}, {request_key: "child-1"}),
    {op: "comment", id: parent, text: "Rollback"},
    {op: "dep_add", id: parent, on: "$0"},
    {op: "claim", id: claimed},
  ]);
  expect(failed.status).toBe(409);
  expect(await failed.json()).toMatchObject({failed: 3, current: {id: claimed}});
  expect(await (await request("/graph")).text()).toBe(before);
  expect(await show(parent, "?comments=1&history=1")).toMatchObject({next_child: 1, children: [], edges: [], comments: []});
  expect((await post([create("Child", parent), {op: "comment", id: "missing", text: ""}])).status).toBe(404);
  expect((await post([create("Child", parent), {op: "update", id: "$8", set: {title: "Bad"}}])).status).toBe(400);
  expect((await show(parent)).next_child).toBe(1);
  const op = create("Child", parent, {}, {request_key: "child-1"});
  const accepted = await batch([op], [{seq}]);
  expect(accepted.results).toEqual([{id: `${parent}.1`}]);
  await runInDurableObject(stub, ({store}) => store.invalidateIndex());
  expect(await batch([op])).toEqual(accepted);
  expect((await show(parent)).children).toEqual([`${parent}.1`]);
});

test("one batch reports all rows to the transaction budget check", async () => {
  const {stub} = await fixture("rows");
  const rows = await runInDurableObject(stub, ({store}) => {
    const counts: Record<string, number> = {};
    const apply = (description: string, ...ops: object[]) => store.batch("rows", "worker", {ops}, (count) => {
      counts[description] = count;
    });
    const {results: [{id}]} = apply("create", create("Root", undefined, {}, {request_key: "a"}));
    apply("retry", create("Root", undefined, {}, {request_key: "a"}));
    const {results: [{id: child}]} = apply("child", create("Child", id, {}, {request_key: "b", deps: [{on: id, kind: "related"}]}));
    apply("update", {op: "update", id: child, set: {title: "Changed"}});
    apply("claim", {op: "claim", id: child});
    apply("defer", {op: "defer", id: child, until: "2999-03-01"});
    apply("reopen", {op: "reopen", id: child});
    apply("comment", {op: "comment", id: child, text: "comment"});
    apply("dep_remove", {op: "dep_remove", id: child, on: id});
    apply("dep_add", {op: "dep_add", id: child, on: id});
    expect(() => store.batch("rows", "worker", {ops: [{op: "close", id: child, reason: "Budget"}]}, () => {
      throw new ApiError(429, {limit: "rows_written"});
    })).toThrow(ApiError);
    expect(store.tickets.lookup(child)?.status).toBe("open");
    expect(store.sql.exec("SELECT status FROM tickets WHERE id = ?", child).one().status).toBe("open");
    apply("close", {op: "close", id: child, reason: "Done"});
    return counts;
  });
  expect(rows).toEqual({
    create: 5, retry: 0, child: 9, update: 3, claim: 4, defer: 4, reopen: 0,
    comment: 2, dep_remove: 2, dep_add: 3, close: 5,
  });
});

test("serves the live graph, listing and a conditional batch with 5,000 stored tickets", async () => {
  const {stub, checked, request, batch} = await fixture("volume");
  await runInDurableObject(stub, ({store}) => {
    store.sql.exec(`
      WITH RECURSIVE ids(n) AS (VALUES (1) UNION ALL SELECT n + 1 FROM ids WHERE n < 5000)
      INSERT INTO tickets (id, parent, title, status, created_at, created_by, updated_at, version)
      SELECT printf('seed%04d', n), CASE WHEN n > 10 THEN printf('seed%04d', 1 + n % 10) END,
        printf('Ticket %04d', n), CASE WHEN n > 4700 THEN 'open' ELSE 'closed' END,
        '2026-11-01', 'seed', '2026-11-01', 0 FROM ids
    `);
    store.sql.exec(`
      INSERT INTO edges SELECT id, 'seed4999', 'blocks'
      FROM tickets WHERE status = 'open' AND id != 'seed4999'
    `);
    store.sql.exec(`
      INSERT INTO edges SELECT id, 'seed0001', 'blocks'
      FROM tickets WHERE status = 'closed' AND parent IS NOT NULL
    `);
    store.invalidateIndex();
  });
  const graph = await checked<{tickets: Ticket[]; edges: Edge[]; seq: number}>(request("/graph"));
  expect(graph.tickets).toHaveLength(300);
  expect(graph.edges).toHaveLength(299);
  const all = await checked<{tickets: Ticket[]; edges: Edge[]}>(request("/graph?all=1"));
  expect(all.tickets).toHaveLength(5000);
  expect(all.edges).toHaveLength(4989);
  expect((await checked<ShownTicket[]>(request("/tickets")))).toHaveLength(300);
  expect((await checked<ShownTicket[]>(request("/tickets?ready=1"))).map(({id}) => id)).toEqual(["seed4999"]);
  await batch([{op: "claim", id: "seed4999"}], [{seq: graph.seq}, {id: "seed4999", assignee: null}]);
  expect((await checked<ShownTicket[]>(request("/tickets?ready=1")))).toEqual([]);
});

async function connectFeed(request: (path: string, init?: RequestInit) => Promise<Response>) {
  const response = await request("/feed", {headers: {Upgrade: "websocket"}});
  expect(response.status).toBe(101);
  const socket = response.webSocket!;
  socket.accept();
  return socket;
}

function nextMessage(socket: WebSocket): Promise<unknown> {
  return new Promise((resolve, reject) => {
    socket.addEventListener("message", ({data}) => resolve(JSON.parse(data)), {once: true});
    socket.addEventListener("close", ({code, reason}) => reject(new Error(`Feed closed: ${code} ${reason}`)), {once: true});
  });
}

test("a hibernated subscriber wakes to recheck readiness after a committed change", async () => {
  const {stub, request, batch, post, list} = await fixture("wait-ready");
  const {seq, results: [{id: blocker}, {id: waiting}]} = await batch([
    create("Prerequisite", undefined, {status: "in_progress"}),
    create("Waiting", undefined, {}, {deps: ["$0"]}),
  ]);
  expect(await list("?ready=1")).toEqual([]);

  const socket = await connectFeed(request);
  try {
    const catchup = nextMessage(socket);
    socket.send('{"since":0}');
    expect(await catchup).toEqual({seq, tickets: [blocker, waiting], leases: [], events: [], cutover: "before"});
    await evictDurableObject(stub);

    const wake = nextMessage(socket);
    const rejected = await post([{op: "comment", id: waiting, text: "This must roll back"}, {op: "claim", id: "missing"}]);
    expect(rejected.status).toBe(404);
    const closed = await batch([{op: "close", id: blocker, reason: "Done"}]);
    expect(await wake).toEqual({seq: closed.seq, tickets: [blocker], leases: [], events: [], cutover: "before"});
    expect((await list("?ready=1")).map(({id}) => id)).toEqual([waiting]);
  } finally {
    socket.close();
  }
});

test("catch-up includes comment and dependency changes with unchanged ticket versions", async () => {
  const {stub, request, batch, show} = await fixture("feed-catchup");
  const {seq, results: [{id: first}, {id: second}]} = await batch([create("First"), create("Second")]);
  const version = (await show(first)).version;
  const closed = await batch([
    {op: "comment", id: first, text: "An update"},
    {op: "dep_add", id: first, on: second},
  ]);
  expect((await show(first)).version).toBe(version);
  await evictDurableObject(stub);

  const socket = await connectFeed(request);
  try {
    const catchup = nextMessage(socket);
    socket.send(JSON.stringify({since: seq}));
    expect(await catchup).toEqual({
      seq: closed.seq, tickets: [first, second], leases: [], events: [], cutover: "before",
    });
  } finally {
    socket.close();
  }
});

test("a reconnect with more than 500 changed tickets requests a resync", async () => {
  const {request, batch} = await fixture("feed-resync");
  await batch(Array.from({length: 501}, (_, index) => create(`Imported ticket ${index}`, undefined, {status: "closed"})));
  const socket = await connectFeed(request);
  try {
    const catchup = nextMessage(socket);
    socket.send('{"since":0}');
    expect(await catchup).toEqual({resync: true});
  } finally {
    socket.close();
  }
});

test("a public feed stays inside its project and requires a WebSocket upgrade", async () => {
  const first = await fixture("feed-first");
  const second = await fixture("feed-second");
  expect((await first.request("/feed")).status).toBe(400);
  const socket = await connectFeed(first.request);
  try {
    const change = nextMessage(socket);
    socket.send('{"since":0}');
    await second.batch([create("Out of scope")]);
    const {seq, results: [{id}]} = await first.batch([create("Visible")]);
    expect(await change).toEqual({seq, tickets: [id], leases: [], events: [], cutover: "before"});
  } finally {
    socket.close();
  }
});

test("two commits in one turn share a frame with unique ticket IDs", async () => {
  const {stub, request, batch} = await fixture("coalesce-feed");
  const {seq, results: [{id}]} = await batch([create("Initial")]);
  const socket = await connectFeed(request);
  try {
    const catchup = nextMessage(socket);
    socket.send('{"since":0}');
    await catchup;
    const message = nextMessage(socket);
    const nextSeq = await runInDurableObject(stub, ({store}, state) => {
      // Both commits happen in the same turn, regardless of simulator speed.
      const feed = new ChangeFeed(state, store);
      for (const offset of [1, 2]) {
        const change = {previous: seq + offset - 1, seq: seq + offset, tickets: [id]};
        state.storage.transactionSync(() => {
          store.sql.exec("UPDATE meta SET value = ? WHERE key = 'seq'", String(change.seq));
          feed.record(change);
        });
        feed.publish(change);
      }
      return seq + 2;
    });
    expect(await message).toEqual({seq: nextSeq, tickets: [id], leases: [], events: [], cutover: "before"});
  } finally {
    socket.close();
  }
});

test("replay crosses the journal wrap in sequence order", async () => {
  const {stub, request, batch} = await fixture("feed-journal-wrap");
  await runInDurableObject(stub, ({store}) => {
    store.sql.exec("INSERT INTO meta VALUES ('seq', '4094')");
  });
  const first = await batch([create("Before wrap")]);
  const second = await batch([create("After wrap")]);
  const socket = await connectFeed(request);
  try {
    const message = nextMessage(socket);
    socket.send('{"since":4094}');
    expect(await message).toEqual({
      seq: second.seq, tickets: [first.results[0].id, second.results[0].id],
      leases: [], events: [], cutover: "before",
    });
  } finally {
    socket.close();
  }
});


test("feed replay includes lease names, event kinds and cutover state", async () => {
  const {request, write, checked} = await fixture("feed-project-changes");
  const lease = "vizier/epic/loop";
  await checked(write("/leases/vizier%2Fepic%2Floop", {op: "acquire", holder: "worker", ttl_s: 180}));
  await checked(write("/events", {kind: "machine", data: {drained: true}}));
  const {seq} = await checked<{seq: number}>(write("/state", {cutover: "paused"}));

  const socket = await connectFeed(request);
  try {
    const catchup = nextMessage(socket);
    socket.send('{"since":0}');
    expect(await catchup).toEqual({seq, tickets: [], leases: [lease], events: ["machine"], cutover: "paused"});
  } finally {
    socket.close();
  }
});

test("the earliest lease expiry wakes a waiter and the alarm advances to the next deadline", async () => {
  const {stub, request, write, checked} = await fixture("lease-feed-expiry");
  const acquire = (name: string, ttl_s: number) =>
    checked<{expires_at: string}>(write(`/leases/${name}`, {op: "acquire", holder: "owner", ttl_s}));
  const late = await acquire("late", 600);
  const early = await acquire("early", 300);
  expect((await write("/leases/early", {op: "acquire", holder: "waiter", ttl_s: 300}, "waiter")).status).toBe(409);
  const alarm = () => runInDurableObject(stub, (_instance, state) => state.storage.getAlarm());
  const day = 86_400_000;
  const midnight = (Math.floor((Date.parse(early.expires_at) - 300_000) / day) + 1) * day;
  expect(await alarm()).toBe(Math.min(midnight, Date.parse(early.expires_at)));

  const socket = await connectFeed(request);
  try {
    const catchup = nextMessage(socket);
    socket.send('{"since":0}');
    const {seq} = await catchup as {seq: number};
    await runInDurableObject(stub, ({store}) => {
      const expired = new Date(Date.now() - 1).toISOString();
      store.sql.exec("UPDATE leases SET expires_at = ? WHERE name = 'early'", expired);
    });

    const wake = nextMessage(socket);
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(await wake).toEqual({seq: seq + 1, tickets: [], leases: ["early"], events: [], cutover: "before"});
    expect(await alarm()).toBe(Math.min(midnight, Date.parse(late.expires_at)));
    const reply = await checked<{holder: string}>(write("/leases/early", {op: "acquire", holder: "waiter", ttl_s: 300}, "waiter"));
    expect(reply.holder).toBe("waiter");
  } finally {
    socket.close();
  }
});

test("the feed names the staging lease when a waiter joins and expires", async () => {
  const {stub, request, write, checked} = await fixture("staging-waiter-feed");
  await checked(write("/leases/staging", {op: "acquire", holder: "owner", ttl_s: 180}));
  const socket = await connectFeed(request);
  try {
    const catchup = nextMessage(socket);
    socket.send('{"since":0}');
    const {seq} = await catchup as {seq: number};
    const joined = nextMessage(socket);
    expect((await write("/leases/staging", {op: "acquire", holder: "waiter", ttl_s: 180}, "waiter")).status).toBe(409);
    expect(await joined).toMatchObject({seq: seq + 1, leases: ["staging"]});
    await runInDurableObject(stub, ({store}) => {
      const since = new Date(Date.now() - 61_000).toISOString();
      store.sql.exec("UPDATE leases SET waiters = json_set(waiters, '$[0].since', ?) WHERE name = 'staging'", since);
    });
    const expired = nextMessage(socket);
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(await expired).toMatchObject({seq: seq + 2, leases: ["staging"]});
    expect(await checked(request("/leases"))).toMatchObject([{holder: "owner", waiters: []}]);
  } finally {
    socket.close();
  }
});

test("an alarm wakes a ready waiter when an open ticket's defer time passes", async () => {
  const {stub, request, batch, list} = await fixture("defer-feed-expiry");
  const future = "2999-01-01T00:00:00.000Z";
  const {seq, results: [{id}]} = await batch([create("Timed task", undefined, {defer_until: future})]);
  expect(await list("?ready=1")).toEqual([]);

  const socket = await connectFeed(request);
  try {
    const catchup = nextMessage(socket);
    socket.send('{"since":0}');
    await catchup;
    await runInDurableObject(stub, ({store}) => {
      const passed = new Date(Date.now() - 2).toISOString();
      store.sql.exec("UPDATE tickets SET defer_until = ? WHERE id = ?", passed, id);
      store.invalidateIndex();
    });
    const wake = nextMessage(socket);
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(await wake).toEqual({seq: seq + 1, tickets: [id], leases: [], events: [], cutover: "before"});
    expect((await list("?ready=1")).map((ticket) => ticket.id)).toEqual([id]);
  } finally {
    socket.close();
  }
});

test("cutover arms imported snoozes and the alarm reopens them exactly once", async () => {
  const {stub, request, write, checked, list, show} = await fixture("legacy-snooze");
  const now = Date.now();
  const until = new Date(now + 60_000).toISOString();
  const ticket = {
    id: "imported", parent: null, next_child: 1, title: "Imported snooze", body: "", design: "",
    acceptance: "", notes: "", type: "task", status: "deferred", priority: 2, assignee: null,
    last: 0, labels: [], metadata: {}, defer_until: until, close_reason: null, closed_at: null,
    created_at: until, updated_at: until, created_by: "legacy", edges: [], comments: [], history: [],
  };
  await checked(write("/import", {tickets: [ticket, {...ticket, id: "undated", defer_until: null}]}));
  await checked(write("/state", {cutover: "paused"}));
  expect(await runDurableObjectAlarm(stub)).toBe(true);
  expect(await show("imported")).toMatchObject({status: "deferred", defer_until: until, ready: false});

  const {seq} = await checked<{seq: number}>(write("/state", {cutover: "after"}));
  const alarm = await runInDurableObject(stub, (_object, state) => state.storage.getAlarm());
  const midnight = (Math.floor(now / 86_400_000) + 1) * 86_400_000;
  expect(alarm).toBe(Math.min(midnight, Date.parse(until) + 1));
  const graph = await request("/graph");
  const tag = graph.headers.get("ETag")!;
  expect(await list("?ready=1&beads=1")).toEqual([]);

  const socket = await connectFeed(request);
  try {
    socket.send(JSON.stringify({since: seq}));
    await runInDurableObject(stub, ({store}) => {
      const expired = new Date(now - 60_000).toISOString();
      store.sql.exec("UPDATE tickets SET defer_until = ? WHERE id = 'imported'", expired);
      store.invalidateIndex();
    });
    const wake = nextMessage(socket);
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(await wake).toMatchObject({seq: seq + 1, tickets: ["imported"], cutover: "after"});
    expect(await list("?ready=1&beads=1")).toMatchObject([{id: "imported", status: "open"}]);
    const current = await checked<{seq: number; tickets: ShownTicket[]}>(request(
      "/graph", {headers: {"If-None-Match": tag}},
    ));
    expect(current).toMatchObject({seq: seq + 1, tickets: [
      {id: "imported", status: "open", ready: true, defer_until: null},
      {id: "undated", status: "deferred", ready: false},
    ]});
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(await checked(request("/state"))).toMatchObject({seq: seq + 1});
  } finally {
    socket.close();
  }
});

test("the next waiter wakes when an abandoned head stops reserving an expired lease", async () => {
  const {stub, request, write, checked} = await fixture("lease-feed-abandoned");
  const acquire = (holder: string) =>
    write("/leases/staging", {op: "acquire", holder, ttl_s: 300}, holder);
  await checked(acquire("owner"));
  expect((await acquire("abandoned")).status).toBe(409);
  expect((await acquire("waiting")).status).toBe(409);

  const socket = await connectFeed(request);
  try {
    const catchup = nextMessage(socket);
    socket.send('{"since":0}');
    const {seq} = await catchup as {seq: number};
    const headDeadline = await runInDurableObject(stub, ({store}) => {
      const now = Date.now();
      const expired = new Date(now - 100).toISOString();
      const since = new Date(now - 30_000).toISOString();
      store.sql.exec(
        "UPDATE leases SET expires_at = ?, waiters = json_set(waiters, '$[0].since', ?)", expired, since,
      );
      return now + 30_000;
    });
    const expiration = nextMessage(socket);
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(await expiration).toMatchObject({seq: seq + 1, leases: ["staging"]});
    const nextAlarm = await runInDurableObject(stub, (_object, state) => state.storage.getAlarm());
    const day = 86_400_000;
    expect(nextAlarm).toBe(Math.min((Math.floor((headDeadline - 30_000) / day) + 1) * day, headDeadline));

    expect(await (await acquire("waiting")).json()).toMatchObject({position: 2});
    await runInDurableObject(stub, ({store}) => {
      const now = Date.now();
      // Simulate the elapsed time since the first wake without waiting 30 seconds.
      store.sql.exec("UPDATE meta SET value = ? WHERE key = 'feed_deadline_cursor'", now - 30_000);
      store.sql.exec(
        "UPDATE leases SET expires_at = ?, waiters = json_set(waiters, '$[0].since', ?)",
        new Date(now - 30_001).toISOString(), new Date(now - 60_001).toISOString(),
      );
    });
    const available = nextMessage(socket);
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(await available).toMatchObject({seq: seq + 2, leases: ["staging"]});
    expect(await checked(acquire("waiting"))).toMatchObject({holder: "waiting"});
  } finally {
    socket.close();
  }
});

test("a dependent stays blocked when child creation races the closing of its prerequisite", async () => {
  const {batch, show} = await fixture("close-and-file");
  const {results: [{id: parent}, {id: original}, {id: dependent}]} = await batch([
    create("Prerequisite"), create("Original child", "$0"),
    create("Dependent", undefined, {}, {deps: ["$0"]}),
  ]);
  const [, created] = await Promise.all([
    batch([{op: "close", id: original, reason: "Complete"}]),
    batch([create("New child", parent)]),
  ]);
  const child = created.results[0].id;
  expect((await show(parent)).children).toContain(child);
  expect((await show(dependent)).ready).toBe(false);
  await batch([{op: "close", id: child, reason: "Complete"}]);
  expect(await show(parent)).toMatchObject({status: "closed"});
  expect((await show(dependent)).ready).toBe(true);
});

test("a queued acquirer can replay a release that happened before it subscribed", async () => {
  const {request, write, checked} = await fixture("lease-feed-gap");
  const path = "/leases/staging";
  await checked(write(path, {op: "acquire", holder: "owner", ttl_s: 300}));
  const conflict = await write(path, {op: "acquire", holder: "waiter", ttl_s: 300}, "waiter");
  expect(conflict.status).toBe(409);
  const {seq} = await conflict.json<{seq: number}>();
  expect(Number.isInteger(seq)).toBe(true);
  await checked(write(path, {op: "release", holder: "owner"}));

  const socket = await connectFeed(request);
  try {
    const released = nextMessage(socket);
    socket.send(JSON.stringify({since: seq}));
    expect(await released).toMatchObject({leases: ["staging"]});
    expect(await checked(write(path, {op: "acquire", holder: "waiter", ttl_s: 300}, "waiter")))
      .toMatchObject({holder: "waiter"});
  } finally {
    socket.close();
  }
});
