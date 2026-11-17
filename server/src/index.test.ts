import {join} from "node:path";
import {access} from "node:fs/promises";
import {afterAll, beforeAll, expect, test} from "vitest";
import {WebSocket} from "ws";
import {version} from "../package.json";
import {startTestServer} from "../tests/test-server";

let server: Awaited<ReturnType<typeof startTestServer>>;
beforeAll(async () => { server = await startTestServer(); });
afterAll(async () => { await server?.close(); });

test("the service serves its package version", async () => {
  const response = await fetch(server.url + "/version");
  expect(response.status).toBe(200);
  expect(await response.text()).toBe(version);
});

test.each(["typo-project", "constructor"])("rejects %s without opening ticket storage", async (project) => {
  const root = `${server.url}/${project}`;
  expect((await fetch(`${root}/api/state`)).status).toBe(404);
  expect((await fetch(`${root}/`)).status).toBe(404);
  expect((await fetch(`${root}/`, {method: "HEAD"})).status).toBe(404);
  await expect(access(join(server.data_dir, `${project}.db`))).rejects.toMatchObject({code: "ENOENT"});
});

test.each(["beryl", "en"])("serves API and dashboard reads for %s", async (project) => {
  const root = `${server.url}/${project}`;
  const state = await fetch(`${root}/api/state`);
  expect(state.status).toBe(200);
  expect(await state.json()).toMatchObject({cutover: "before", quota_enforced: false});
  const dashboard = await fetch(`${root}/`);
  expect(dashboard.status).toBe(200);
  expect(dashboard.headers.get("Content-Type")).toContain("text/html");
  expect(await dashboard.text()).toContain(`<meta name="cirrus-root" content="/${project}/">`);
});

test.each(["by", "beryl"])("opens the Beryl change feed at /%s", async (path) => {
  const socket = new WebSocket(server.url.replace(/^http/, "ws") + `/${path}/api/feed`);
  try {
    await new Promise<void>((resolve, reject) => {
      socket.once("open", resolve);
      socket.once("error", reject);
    });
  } finally {
    socket.terminate();
  }
});
