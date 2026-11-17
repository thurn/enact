import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {copyFile, mkdir, mkdtemp, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {test} from "node:test";
import {setTimeout as delay} from "node:timers/promises";
import WebSocket from "ws";
import {backupProjects, startServer, verifyRecentBackup, verifySnapshot} from "../.local/tickets.mjs";

test("restart the SQLite service and restore a live backup", {timeout: 30_000}, async () => {
  const directory = await mkdtemp(join(tmpdir(), "enact-sqlite-backup-"));
  const token = "dashboard-test-token";
  const bearer = {"Authorization": `Bearer ${token}`, "X-Actor": "Operator", "Content-Type": "application/json"};
  const data_dir = join(directory, "source");
  await mkdir(data_dir);
  const config = {data_dir, projects: {by: "thurn/beryl"}, port: 0};
  let local, socket;
  try {
    local = await startServer(config, {
      async fixture(_request, mounted) {
        const hash = createHash("sha256").update(token).digest("hex");
        mounted.get("by").storage.sql.exec("INSERT INTO meta VALUES ('token_hashes', ?)", JSON.stringify({[hash]: "test-mac"}));
        return new Response(null, {status: 204});
      },
    });
    const source = local.url;
    const api = (origin, path, body) => {
      const init = body === undefined ? undefined : {method: "POST", headers: bearer, body: JSON.stringify(body)};
      return fetch(origin + "/by/api/" + path, init);
    };
    const post = async (origin, path, body) => {
      const reply = await api(origin, path, body);
      assert.equal(reply.status, 200, await reply.clone().text());
      return reply.json();
    };
    assert.equal((await fetch(source + "/__coordination/seed", {method: "POST"})).status, 204);
    await post(source, "state", {cutover: "paused"});
    await post(source, "state", {cutover: "after"});
    const receipt = await post(source, "batch", {ops: [
      {op: "create", fields: {title: "Parent"}, request_key: "durable-key"},
      {op: "create", parent: "$0", fields: {title: "Child"}},
      {op: "comment", id: "$1", text: "Preserved comment"},
      {op: "event", id: "$1", kind: "ci", data: {os: "macos", passed: true}},
    ]});
    const [parent, child] = receipt.results.map(result => result.id);
    const lease = await post(source, "leases/staging", {op: "acquire", holder: "Holder", ttl_s: 180});
    const original = await (await api(source, `tickets/${child}?comments=1&history=1`)).json();
    const state = await (await api(source, "state")).json();
    await local.close();
    local = await startServer({...config, read_only: true}, {});
    assert.equal((await api(local.url, "batch", {ops: []})).status, 503);
    assert.equal((await (await api(local.url, "state")).json()).seq, state.seq);
    await local.close();
    local = await startServer(config, {});
    const actual = await (await api(local.url, `tickets/${child}?comments=1&history=1`)).json();
    assert.deepEqual(actual, original);
    assert.deepEqual((await (await api(local.url, "leases")).json()).find(entry => entry.name === "staging"), lease);
    assert.equal((await (await api(local.url, "state")).json()).quota_enforced, false);
    assert.equal((await post(local.url, "batch", {ops: [
      {op: "create", fields: {title: "Parent"}, request_key: "durable-key"},
    ]})).results[0].id, parent);

    function nextFeed(since) {
      socket = new WebSocket(local.url.replace(/^http/, "ws") + "/by/api/feed");
      return new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("No change feed notification")), 3000);
        socket.once("open", () => socket.send(JSON.stringify({since})));
        socket.once("message", data => { clearTimeout(timeout); resolve(JSON.parse(data)); });
        socket.once("error", error => { clearTimeout(timeout); reject(error); });
      });
    }
    assert.equal((await nextFeed(state.seq - 1)).seq, state.seq);

    const deadline = Date.now() + 500;
    const deferred = await post(local.url, "batch", {ops: [
      {op: "update", id: child, set: {status: "deferred", defer_until: new Date(deadline).toISOString()}},
    ]});
    await local.close();
    local = undefined;
    await delay(Math.max(0, deadline - Date.now() + 10));
    local = await startServer(config, {});
    const wake = await nextFeed(deferred.seq);
    assert.ok(wake.seq > deferred.seq);
    assert.ok(wake.tickets.includes(child));
    assert.equal((await (await api(local.url, `tickets/${child}`)).json()).status, "open");

    const backupDirectory = join(directory, "snapshots");
    const [snapshot] = await Promise.all([
      backupProjects(config, backupDirectory),
      post(local.url, "batch", {ops: [{op: "update", id: child, set: {notes: "Concurrent backup write"}}]}),
    ]);
    assert.deepEqual(await verifyRecentBackup(config, backupDirectory), await verifySnapshot(snapshot.snapshot));

    await local.close();
    local = undefined;
    const recovery = join(directory, "recovery");
    await mkdir(recovery);
    await copyFile(join(snapshot.snapshot, "by.db"), join(recovery, "by.db"));
    local = await startServer({...config, data_dir: recovery, read_only: true}, {});
    const restored = await (await api(local.url, `tickets/${child}?comments=1`)).json();
    assert.equal(restored.title, "Child");
    assert.equal(restored.comments[0].text, "Preserved comment");
    assert.equal((await (await api(local.url, "state")).json()).seq, snapshot.projects.by.seq);
  } finally {
    socket?.terminate();
    await local?.close();
    await rm(directory, {recursive: true, force: true});
  }
});
