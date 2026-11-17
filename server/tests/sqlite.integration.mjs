import assert from "node:assert/strict";
import {copyFile, mkdir, mkdtemp, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {test} from "node:test";
import {setTimeout as delay} from "node:timers/promises";
import {fileURLToPath} from "node:url";
import WebSocket from "ws";
import {unstable_dev} from "wrangler";
import {backupProjects, migrate, setSourceFreeze, startServer, verifyRecentBackup, verifySnapshot} from "../.local/tickets.mjs";

test("freeze and resume a Worker export, then restore a live SQLite backup", {timeout: 30_000}, async () => {
  const directory = await mkdtemp(join(tmpdir(), "enact-sqlite-migration-"));
  const secret = "fixture-migration-token";
  const id = "sqlite-migration-0001";
  const bearer = {"Authorization": "Bearer dashboard-test-token", "X-Actor": "Operator", "Content-Type": "application/json"};
  const serverPath = fileURLToPath(new URL("../", import.meta.url));
  let worker, local, socket;
  try {
    worker = await unstable_dev(join(serverPath, "tests/dashboard-worker.ts"), {
      config: join(serverPath, "wrangler.toml"), ip: "127.0.0.1", port: 0, logLevel: "error",
      vars: {TICKET_MIGRATION_TOKEN: secret}, experimental: {disableExperimentalWarning: true}, persist: false,
    });
    const source = `http://${worker.address}:${worker.port}`;
    const api = (origin, path, body) => {
      const init = body === undefined ? undefined : {method: "POST", headers: bearer, body: JSON.stringify(body)};
      return fetch(origin + "/by/api/" + path, init);
    };
    const post = async (origin, path, body) => {
      const reply = await api(origin, path, body);
      assert.equal(reply.status, 200, await reply.clone().text());
      return reply.json();
    };
    assert.equal((await api(source, "test-token", {})).status, 204);
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
    await assert.rejects(setSourceFreeze(source, "by", id, "wrong", true), /HTTP 404/);
    const freeze = await setSourceFreeze(source, "by", id, secret, true);
    assert.equal(freeze.frozen, true);
    assert.equal((await api(source, "leases/staging", {op: "release", holder: "Holder"})).status, 503);
    assert.equal((await api(source, "tickets?ready=1")).status, 503);
    await assert.rejects(setSourceFreeze(source, "by", "another-migration-id", secret, false), /HTTP 409/);

    // Fail a later table request after earlier tables committed. Reuse the
    // same partially populated database on the next invocation.
    const destination = join(directory, "by.db");
    const fetchSource = globalThis.fetch;
    globalThis.fetch = (url, init) => url.searchParams.get("table") === "events"
      ? Promise.reject(new Error("Interrupted export")) : fetchSource(url, init);
    try {
      await assert.rejects(migrate(source, "by", id, secret, destination), /Interrupted export/);
    } finally {
      globalThis.fetch = fetchSource;
    }
    const migrated = await migrate(source, "by", id, secret, destination);
    assert.equal(migrated.seq, freeze.seq);
    assert.equal(migrated.rows.tickets, 2);
    assert.equal(migrated.rows.comments, 1);
    assert.equal(migrated.rows.leases, 1);

    const config = {data_dir: directory, projects: {by: ""}, port: 0};
    local = await startServer({...config, read_only: true}, {});
    assert.equal((await api(local.url, "batch", {ops: []})).status, 503);
    assert.equal((await (await api(local.url, "state")).json()).seq, freeze.seq);
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
    assert.equal((await nextFeed(freeze.seq - 1)).seq, freeze.seq);

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
    assert.equal((await setSourceFreeze(source, "by", id, secret, false)).frozen, false);
    assert.equal((await api(source, "state")).status, 200);
  } finally {
    socket?.terminate();
    await local?.close();
    await worker?.stop();
    await rm(directory, {recursive: true, force: true});
  }
});
