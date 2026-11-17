import { env, exports } from "cloudflare:workers";
import { listDurableObjectIds, runInDurableObject } from "cloudflare:test";
import { expect, test } from "vitest";
import { version } from "../package.json";

test("the Worker serves its package version", async () => {
  const response = await exports.default.fetch("https://example.com/version");

  expect(response.status).toBe(200);
  expect(await response.text()).toBe(version);
});

test.each(["typo-project", "constructor"])("rejects %s without opening ticket storage", async (project) => {
  const root = `https://example.com/${project}`;
  expect((await exports.default.fetch(`${root}/api/state`)).status).toBe(404);
  expect((await exports.default.fetch(`${root}/`)).status).toBe(404);
  expect((await exports.default.fetch(`${root}/`, {method: "HEAD"})).status).toBe(404);

  const id = env.TICKETS.idFromName(project);
  const opened = await listDurableObjectIds(env.TICKETS);
  expect(opened.some((object) => object.equals(id))).toBe(false);
});

test.each(["by", "en"])("serves API and dashboard reads for %s", async (project) => {
  const root = `https://example.com/${project}`;
  const state = await exports.default.fetch(`${root}/api/state`);
  expect(state.status).toBe(200);
  expect(await state.json()).toMatchObject({cutover: "before"});

  const dashboard = await exports.default.fetch(`${root}/`);
  expect(dashboard.status).toBe(200);
  expect(dashboard.headers.get("Content-Type")).toContain("text/html");
});

test("a project's Durable Object has SQLite storage", async () => {
  const project = env.TICKETS.getByName("test");
  const result = await runInDurableObject(project, (_instance, state) =>
    state.storage.sql.exec<{ value: number }>("SELECT 1 AS value").one(),
  );

  expect(result.value).toBe(1);
});
