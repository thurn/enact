// Run against an isolated SQLite service. Build tk first with
// cargo build --manifest-path ../cli/Cargo.toml --locked --bin tk.
// Set PLAYWRIGHT_MODULE to the installed Playwright entry point if needed.
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {execFile} from "node:child_process";
import {mkdir, mkdtemp, rm, writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {fileURLToPath} from "node:url";
import {promisify} from "node:util";
import {startServer} from "../.local/tickets.mjs";

const {chromium} = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const serverPath = fileURLToPath(new URL("../", import.meta.url));
const tkPath = process.env.TK_PATH ?? join(serverPath, "../cli/target/debug", process.platform === "win32" ? "tk.exe" : "tk");
const scratch = await mkdtemp(join(tmpdir(), "enact-dashboard-live-"));
const token = "dashboard-test-token";
let server, browser;

try {
  server = await startServer({data_dir: scratch, projects: {by: ""}, port: 0}, {
    async fixture(_request, mounted) {
      const hash = createHash("sha256").update(token).digest("hex");
      mounted.get("by").storage.sql.exec("INSERT INTO meta VALUES ('token_hashes', ?)", JSON.stringify({[hash]: "test-mac"}));
      return new Response(null, {status: 204});
    },
  });
  const origin = server.url;
  assert.equal((await fetch(origin + "/__coordination/seed", {method: "POST"})).status, 204);
  const tokenDirectory = process.platform === "win32" ? ["appdata", "enact"] : ["home", ".config", "enact"];
  await mkdir(join(scratch, ...tokenDirectory), {recursive: true});
  await writeFile(join(scratch, ...tokenDirectory, "token"), token + "\n");
  await writeFile(join(scratch, ".enact.toml"), `[tickets]\nproject = "by"\nurl = "${origin}"\n`);
  const run = promisify(execFile);
  async function tkOutput(...args) {
    const {stdout} = await run(tkPath, ["--retry-for", "0", "--actor", "Dashboard test", ...args], {
      cwd: scratch, env: {...process.env, ENACT_TICKETS_URL: "", HOME: join(scratch, "home"), APPDATA: join(scratch, "appdata")},
    });
    return stdout.trim();
  }
  async function tk(...args) {
    return JSON.parse(await tkOutput("--json", ...args));
  }
  async function api(path, data, actor = "Dashboard test") {
    return fetch(origin + "/by/api/" + path, {
      method: "POST", body: JSON.stringify(data),
      headers: {"Authorization": `Bearer ${token}`, "X-Actor": actor, "Content-Type": "application/json"},
    });
  }
  const body = join(scratch, "body.txt");
  await writeFile(body, Array.from({length: 60}, (_, n) => `Reading position, paragraph ${n}.`).join("\n\n"));
  const {results: [{id}]} = await tk("create", "--title", "Live dashboard fixture", "--body-file", body);
  for (let n = 0; n < 18; n++) await tk("create", "--title", `Scroll fixture ${n}`);
  assert.match(id, /^T[a-z][a-z0-9]{3}$/);
  const displayId = id;
  assert.equal((await tkOutput("show", displayId)).split("\n")[0], `${displayId}  open  Live dashboard fixture`);
  assert.equal((await tk("show", displayId)).id, displayId);
  await tk("event", "session", JSON.stringify({session: "Casey", ticket: displayId, action: "start"}));
  await tk("event", "ci", JSON.stringify({os: "macos", passed: false}));
  await tk("event", "ci", JSON.stringify({os: "windows", passed: false}));
  await tk("event", "ci", JSON.stringify({os: "macos", passed: true, tip: "abcdef1234567890"}));
  assert.equal((await api("leases/staging", {op: "acquire", holder: "Owner", ttl_s: 180})).status, 200);

  browser = await chromium.launch({channel: process.env.CHROME_CHANNEL ?? "chrome"});
  const context = await browser.newContext({viewport: {width: 1280, height: 720}, reducedMotion: "reduce"});
  async function open(path, ctx = context, route) {
    const page = await ctx.newPage();
    if (route) await page.routeWebSocket("**/beryl/api/feed", route);
    const feed = page.waitForEvent("websocket", {predicate: (ws) => ws.url().endsWith("/beryl/api/feed")})
      .then((ws) => ws.waitForEvent("framesent"));
    const response = await page.goto(origin + path);
    assert.equal(response.status(), 200, path);
    await feed;
    return page;
  }

  const coldStart = performance.now();
  assert.equal((await fetch(origin + "/beryl/")).status, 200);
  const coldMs = performance.now() - coldStart;
  const warmStart = performance.now();
  assert.equal((await fetch(origin + "/beryl/")).status, 200);
  console.log(`Dashboard render: cold ${(coldMs).toFixed(1)} ms, warm ${(performance.now() - warmStart).toFixed(1)} ms.`);
  const home = await open("/beryl/");
  assert.equal(await home.getByRole("link").filter({hasText: displayId}).getAttribute("href"),
    "/beryl/tickets/" + displayId);
  let connected;
  let disconnecting = false;
  const detail = await open("/beryl/tickets/" + id, context, (route) => {
    if (disconnecting) return route.close({code: 1012});
    const server = route.connectToServer();
    connected = () => Promise.all([route.close({code: 1012}), server.close({code: 1012})]);
  });
  const status = home.locator("#project-status");
  await status.getByText("Casey").waitFor();
  await status.getByText(/^macOS/).waitFor();
  await status.getByText(/^Windows/).waitFor();
  assert.match(await status.textContent(), /Passed.*Failed/s);
  assert.match(await status.textContent(), /HolderOwner.*Requests\d+Rows written\d+/s);

  await home.bringToFront();
  const untouchedCard = home.getByRole("link").filter({has: home.getByRole("heading", {name: "Scroll fixture 0", exact: true})});
  assert.equal(await untouchedCard.evaluate((card) => card.getBoundingClientRect().top > innerHeight), true);
  const href = await untouchedCard.evaluate((card) => card.href);
  assert.equal(await home.evaluate((url) => performance.getEntriesByName(url).length, href), 0);
  const prefetched = home.waitForResponse((response) => response.url() === href && response.ok());
  await untouchedCard.scrollIntoViewIfNeeded();
  await prefetched;

  for (const page of [home, detail]) {
    await page.evaluate(() => {
      window.retained = true;
      scrollTo(0, 280);
    });
    assert.equal(await page.evaluate(() => scrollY), 280);
  }
  const started = performance.now();
  await tk("update", id, "--title", "Updated in both open pages");
  await Promise.all([
    home.getByRole("heading", {name: "Updated in both open pages"}).waitFor(),
    detail.getByRole("heading", {name: "Updated in both open pages"}).waitFor(),
  ]);
  const elapsed = performance.now() - started;
  for (const page of [home, detail]) {
    assert.deepEqual(await page.evaluate(() => [window.retained, scrollY]), [true, 280]);
  }

  assert.equal((await api("leases/staging", {op: "acquire", holder: "Waiting session", ttl_s: 180})).status, 409);
  await status.getByText("Waiting session", {exact: true}).waitFor();
  assert.equal((await api("state", {cutover: "paused"})).status, 200);
  await status.getByText("Writes paused").waitFor();

  const idleRequests = [];
  for (const page of [home, detail]) page.on("request", (request) => idleRequests.push(request.url()));
  // Observe many feed-coalescing intervals with no mutations or interaction.
  await new Promise((resolve) => setTimeout(resolve, 2_000));
  assert.deepEqual(idleRequests, []);

  disconnecting = true;
  await connected();
  await tk("update", id, "--title", "Replayed after reconnect");
  await home.getByRole("heading", {name: "Replayed after reconnect"}).waitFor();
  assert.equal(await detail.getByRole("heading", {name: "Updated in both open pages"}).count(), 1);
  disconnecting = false;
  await detail.getByRole("heading", {name: "Replayed after reconnect"}).waitFor();
  assert.deepEqual(await detail.evaluate(() => [window.retained, scrollY]), [true, 280]);

  for (const width of [375, 1280]) {
    for (const colorScheme of ["light", "dark"]) {
      const view = await browser.newContext({viewport: {width, height: 720}, colorScheme, reducedMotion: "reduce"});
      const page = await open("/beryl/", view);
      await page.locator(".c-card").filter({hasText: displayId}).getByText(displayId, {exact: true}).waitFor();
      const ticket = await open("/beryl/tickets/" + id, view);
      await ticket.locator(".c-eyebrow").getByText(displayId, {exact: true}).waitFor();
      assert.ok(await ticket.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      const panel = page.locator("#project-status");
      await panel.getByText("Waiting session", {exact: true}).waitFor();
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      const link = page.getByRole("link", {name: "Project status"});
      await link.click();
      assert.equal(await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches), true);
      const reveal = panel.locator(".c-reveal");
      assert.equal(await reveal.evaluate((el) => getComputedStyle(el).animationDuration), "0.001s");
      if (process.env.DASHBOARD_SCREENSHOTS) {
        await page.screenshot({path: join(process.env.DASHBOARD_SCREENSHOTS, `status-${width}-${colorScheme}.png`)});
      }
      await view.close();
    }
  }
  console.log(`Both open pages updated in ${elapsed.toFixed(1)} ms with scroll preserved and zero idle requests.`);
} finally {
  await browser?.close();
  await server?.close();
  await rm(scratch, {recursive: true, force: true});
}
