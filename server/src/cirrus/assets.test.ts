import { exports } from "cloudflare:workers";
import { expect, test } from "vitest";
import { assets } from "./assets";
import { page } from "./components";

for (const [name, path] of Object.entries(assets)) {
  test(`${name} is served by the local Worker under an immutable content hash`, async () => {
    const response = await exports.default.fetch(`https://example.com/${path}`);
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
  const response = await exports.default.fetch("https://example.com/sw.js");
  expect(response.status).toBe(200);
  expect(response.headers.get("cache-control")).toBe(
    "public, max-age=0, must-revalidate",
  );
  expect(await response.text()).toContain('addEventListener("fetch"');
});

test("project pages link to the Worker assets and keep their own navigation root", () => {
  const html = page({
    title: "Tickets",
    body: "",
    root: "/by/",
    assetRoot: "/",
  });
  expect(html).toContain('<meta name="cirrus-root" content="/by/">');
  expect(html).toContain(`href="/${assets["cirrus.css"]}"`);
  expect(html).toContain(`src="/${assets["cirrus.js"]}"`);
});
