import {afterAll, beforeAll} from "vitest";
import {startTestServer} from "../../tests/test-server";
import { expect, test } from "vitest";
import { assets } from "./assets";
import { siteIcons } from "../dashboard/common";
import { page } from "./components";

let server: Awaited<ReturnType<typeof startTestServer>>;
beforeAll(async () => { server = await startTestServer(); });
afterAll(async () => { await server?.close(); });

for (const [name, path] of Object.entries(assets)) {
  test(`${name} is served by the local ticket service under an immutable content hash`, async () => {
    const response = await fetch(`${server.url}/${path}`);
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe(
      "public, max-age=31536000, immutable",
    );
    const hash = await crypto.subtle.digest(
      "SHA-256",
      await response.arrayBuffer(),
    );
    const prefix = Array.from(new Uint8Array(hash).subarray(0, 5), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join("");
    expect(path).toBe(`assets/${name.replace(".", `.${prefix}.`)}`);
  });
}

test("the service worker keeps a stable URL and revalidates", async () => {
  const response = await fetch(server.url + "/sw.js");
  expect(response.status).toBe(200);
  expect(response.headers.get("cache-control")).toBe(
    "no-cache",
  );
  expect(await response.text()).toContain('addEventListener("fetch"');
});

test("project pages link to service assets and keep their own navigation root", () => {
  const html = page({
    title: "Tickets",
    body: "",
    root: "/beryl/",
    icons: siteIcons,
    brand: "Beryl",
    assetRoot: "/",
  });
  expect(html).toContain('<meta name="cirrus-root" content="/beryl/">');
  expect(html).toContain(`href="/${assets["cirrus.css"]}"`);
  expect(html).toContain(`src="/${assets["cirrus.js"]}"`);
  expect(html).toContain('<link rel="icon" type="image/svg+xml" sizes="any" href="/favicon.svg">');
  expect(html).toContain('<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">');
  expect(html).toContain('<meta name="apple-mobile-web-app-title" content="Beryl">');
});


for (const [path, size] of [[siteIcons.png, 32], [siteIcons.apple, 180]] as const) {
  test(`${path} serves a square PNG at the required size`, async () => {
    const response = await fetch(`${server.url}${path}`);
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("image/png");
    const bytes = await response.arrayBuffer();
    expect([...new Uint8Array(bytes, 0, 8)]).toEqual([137, 80, 78, 71, 13, 10, 26, 10]);
    const header = new DataView(bytes);
    expect(header.getUint32(16)).toBe(size);
    expect(header.getUint32(20)).toBe(size);
  });
}

test("the scalable purple gem favicon is served as SVG", async () => {
  const response = await fetch(`${server.url}${siteIcons.svg}`);
  expect(response.status).toBe(200);
  expect(response.headers.get("content-type")).toContain("image/svg+xml");
  expect(await response.text()).toContain('viewBox="0 0 256 256"');
});
