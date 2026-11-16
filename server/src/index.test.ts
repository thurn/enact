import { env, exports } from "cloudflare:workers";
import { runInDurableObject } from "cloudflare:test";
import { expect, test } from "vitest";
import { version } from "../package.json";

test("the Worker serves its package version", async () => {
  const response = await exports.default.fetch("https://example.com/version");

  expect(response.status).toBe(200);
  expect(await response.text()).toBe(version);
});

test("a project's Durable Object has SQLite storage", async () => {
  const project = env.TICKETS.getByName("test");
  const result = await runInDurableObject(project, (_instance, state) =>
    state.storage.sql.exec<{ value: number }>("SELECT 1 AS value").one(),
  );

  expect(result.value).toBe(1);
});
