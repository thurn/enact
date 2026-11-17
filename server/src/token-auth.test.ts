import {env, exports} from "cloudflare:workers";
import {runInDurableObject} from "cloudflare:test";
import {expect, test, vi} from "vitest";

async function tokenRef(machine: string, token: string) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  const hex = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `refs/enact/tokens/${machine}/${hex}`;
}

function packet(text: string): string {
  return (new TextEncoder().encode(text).length + 4).toString(16).padStart(4, "0") + text;
}

function advertisement(refs: string[]): string {
  const commit = "a".repeat(40);
  return packet("# service=git-upload-pack\n") + "0000"
    + packet(`${commit} HEAD\0symref=HEAD:refs/heads/master\n`)
    + packet(`${commit} refs/heads/café\n`)
    + refs.map((ref) => packet(`${commit} ${ref}\n`)).join("") + "0000";
}

test.each([
  {project: "by", repository: "thurn/beryl"},
  {project: "en", repository: "thurn/enact"},
])("$project discovers Git token refs and keeps a durable cooldown for failed lookups", async ({project, repository}) => {
  const original = "original-bearer";
  const rotated = "rotated-bearer";
  const machine = "test-machine";
  const url = `https://github.com/${repository}.git/info/refs?service=git-upload-pack`;
  const stub = env.TICKETS.getByName(project);
  let body = advertisement([await tokenRef(machine, original)]);
  let status = 200;
  const realFetch = globalThis.fetch;
  const github = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    // Let the Worker runtime validate the outbound options before returning fixture refs.
    new Request(input, init);
    return new Response(body, {status, headers: {"Content-Type": "application/x-git-upload-pack-advertisement"}});
  });
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const address = input instanceof Request ? input.url : String(input);
    return address === url ? github(input, init) : realFetch(input, init);
  });

  const write = (token?: string) => exports.default.fetch(`https://example.com/${project}/api/events`, {
    method: "POST",
    headers: token ? {Authorization: `Bearer ${token}`, "X-Actor": "test-actor"} : {},
    body: JSON.stringify({kind: "machine", data: {enrolled: true}}),
  });
  const allowRefresh = () => runInDurableObject(stub, ({store}) => {
    store.sql.exec("UPDATE meta SET value = ? WHERE key = 'token_fetched_at'", String(Date.now() - 60_000));
  });

  try {
    expect((await write()).status).toBe(401);
    expect(github).toHaveBeenCalledTimes(0);
    const writes = await Promise.all([write(original), write("unknown"), write("also-unknown")]);
    expect(writes.map((write) => write.status)).toEqual([200, 401, 401]);
    expect(await writes[0].json()).toMatchObject({kind: "machine", machine, data: {enrolled: true}});
    await runInDurableObject(stub, ({store}) => {
      expect(Object.values(JSON.parse(store.meta("token_hashes")!))).toEqual([machine]);
      expect(Number(store.meta("token_fetched_at"))).toBeGreaterThan(0);
    });
    expect(github).toHaveBeenCalledExactlyOnceWith(url, expect.objectContaining({redirect: "manual"}));

    expect((await write("still-unknown")).status).toBe(401);
    expect((await write(original)).status).toBe(200);
    expect(github).toHaveBeenCalledTimes(1);

    await allowRefresh();
    status = 302;
    body = advertisement([await tokenRef(machine, rotated)]);
    expect((await write(rotated)).status).toBe(401);
    expect((await write(rotated)).status).toBe(401);
    expect((await write(original)).status).toBe(200);
    expect(github).toHaveBeenCalledTimes(2);

    await allowRefresh();
    status = 200;
    body = body.slice(0, -4);
    expect((await write(rotated)).status).toBe(401);
    expect((await write(original)).status).toBe(200);

    await allowRefresh();
    body = advertisement([await tokenRef(machine, rotated), `${await tokenRef(machine, original)}/extra`]);
    expect((await write(rotated)).status).toBe(200);
    expect((await write(original)).status).toBe(401);
    expect(github).toHaveBeenCalledTimes(4);
  } finally {
    vi.unstubAllGlobals();
  }
});
