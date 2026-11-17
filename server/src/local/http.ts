import {existsSync} from "node:fs";
import {readFile, readdir} from "node:fs/promises";
import {createServer, type IncomingMessage, type ServerResponse} from "node:http";
import type {Socket} from "node:net";
import {extname, isAbsolute, join} from "node:path";
import {URL as FileURL} from "node:url";
import {WebSocket, WebSocketServer} from "ws";
import {version} from "../../package.json";
import {dashboardRoute} from "../dashboard";
import {TicketProject, apiPath} from "../project";
import {localUsage} from "../project-usage";
import type {FeedSocket} from "../ticket-runtime";
import {SqliteStorage} from "./sqlite";

const address = "127.0.0.1";
const defaultPort = 7345;
const maxBody = 100 * 1024 * 1024;
const contentTypes: Record<string, string> = {
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
};

export type Config = {
  data_dir: string;
  projects: Record<string, string>;
  public_url?: string;
  port?: number;
  read_only?: boolean;
};

export function projectFile(directory: string, project: string): string {
  if (!/^[a-z0-9][a-z0-9-]*$/.test(project)) throw new Error("Invalid project key");
  return join(directory, `${project}.db`);
}

export function validateConfig(config: Config): void {
  if (!config || typeof config.data_dir !== "string" || !isAbsolute(config.data_dir)
      || !config.projects || typeof config.projects !== "object" || Array.isArray(config.projects)
      || !Object.keys(config.projects).length) throw new Error("Expected an absolute data_dir and configured projects");
  for (const [project, repo] of Object.entries(config.projects)) {
    projectFile(config.data_dir, project);
    if (typeof repo !== "string" || !/^[\w.-]+\/[\w.-]+$/.test(repo)) throw new Error(`Invalid repository for ${project}`);
  }
  if (config.port !== undefined && (!Number.isInteger(config.port) || config.port < 1 || config.port > 65535)) {
    throw new Error("Invalid listening port");
  }
  if (config.read_only !== undefined && typeof config.read_only !== "boolean") throw new Error("Invalid read_only");
  if (config.public_url !== undefined) {
    const publicUrl = new URL(config.public_url);
    if (publicUrl.protocol !== "https:" || publicUrl.origin !== config.public_url) {
      throw new Error("public_url must be an HTTPS origin without a trailing slash");
    }
  }
}

function mount(name: string, repository: string, storage: SqliteStorage, readOnly: boolean) {
  let alarm: {at: number; timer: ReturnType<typeof setTimeout>} | undefined;
  let stopped = false;
  const sockets = new Map<WebSocket, FeedSocket>();
  function schedule(at: number) {
    if (stopped) return;
    if (alarm) clearTimeout(alarm.timer);
    alarm = {at, timer: setTimeout(() => {
      alarm = undefined;
      void project.alarm().catch(error => {
        console.error(`Deadline job failed for ${name}`, error);
        if (!alarm) schedule(Date.now() + 1000);
      });
    }, Math.max(0, at - Date.now()))};
  }
  const project = new TicketProject(name, repository, {
    storage, readOnly, usage: localUsage(),
    getAlarm: async () => alarm?.at ?? null,
    setAlarm: async at => { if (!alarm || at < alarm.at) schedule(at); },
    feed: {
      sockets: () => sockets.values(),
      waitUntil: promise => { void promise.catch(console.error); },
      upgrade: () => Response.json({error: "Expected a WebSocket upgrade"}, {status: 400}),
    },
  });
  return {
    project, storage,
    connect(socket: WebSocket) {
      let cursor: number | null = null;
      const feed: FeedSocket = {
        send(message) {
          if (socket.bufferedAmount > 1024 * 1024) socket.terminate();
          else socket.send(message);
        },
        close(code, reason) { socket.close(code, reason); },
        serializeAttachment(value) { cursor = value; },
        deserializeAttachment() { return cursor; },
      };
      sockets.set(socket, feed);
      socket.on("message", (message, binary) => {
        project.feed.subscribe(feed, binary ? new ArrayBuffer(0) : message.toString());
      });
      socket.on("error", () => socket.terminate());
      socket.on("close", () => sockets.delete(socket));
    },
    close() {
      stopped = true;
      if (alarm) clearTimeout(alarm.timer);
      for (const socket of sockets.keys()) socket.terminate();
      sockets.clear();
      storage.close();
    },
  };
}

async function readAssets(root: URL) {
  const assets = new Map<string, {body: Uint8Array; headers: Record<string, string>}>();
  for (const pathname of await readdir(root, {recursive: true})) {
    const name = pathname.replaceAll("\\", "/");
    const type = contentTypes[extname(name)];
    if (!type) continue;
    assets.set(`/${name}`, {
      body: new Uint8Array(await readFile(new FileURL(name, root))),
      headers: {"content-type": type, "cache-control": name.startsWith("assets/")
        ? "public, max-age=31536000, immutable" : "no-cache"},
    });
  }
  return assets;
}

function requestUrl(request: IncomingMessage, loopback: string, publicUrl?: string): URL | undefined {
  const target = request.url;
  if (!target?.startsWith("/") || /^\/[/\\]/.test(target)) return;
  const host = request.headers.host;
  if (publicUrl && host === new URL(publicUrl).host) return new URL(publicUrl + target);
  if (host === new URL(loopback).host) return new URL(loopback + target);
}

async function asRequest(request: IncomingMessage, url: URL): Promise<Request> {
  let body: Uint8Array<ArrayBuffer> | undefined;
  if (request.method === "POST") {
    const chunks: Buffer[] = [];
    let bytes = 0;
    for await (const chunk of request.iterator({destroyOnReturn: false})) {
      bytes += chunk.length;
      if (bytes > maxBody) throw new RequestTooLarge();
      chunks.push(chunk);
    }
    body = Buffer.concat(chunks);
  }
  const headers = new Headers();
  for (let index = 0; index < request.rawHeaders.length; index += 2) {
    headers.append(request.rawHeaders[index], request.rawHeaders[index + 1]);
  }
  return new Request(url, {method: request.method, headers, body});
}

class RequestTooLarge extends Error {}

async function send(output: ServerResponse, response: Response) {
  output.statusCode = response.status;
  for (const [key, value] of response.headers) output.setHeader(key, value);
  output.end(response.body ? Buffer.from(await response.arrayBuffer()) : undefined);
}

export async function startServer(config: Config, test?: {
  assetRoot?: URL;
  fixture?(request: Request, projects: Map<string, ReturnType<typeof mount>>): Promise<Response | undefined>;
}) {
  if (!test) validateConfig(config);
  const assets = await readAssets(test?.assetRoot ?? new FileURL("../.cirrus/public/", import.meta.url));
  const projects = new Map<string, ReturnType<typeof mount>>();
  const websocket = new WebSocketServer({noServer: true, maxPayload: 128});
  let loopback = "";
  const server = createServer((input, output) => {
    void respond(input).then(response => send(output, response)).catch(error => {
      if (error instanceof RequestTooLarge) input.resume();
      else console.error(error);
      if (!output.headersSent) output.writeHead(error instanceof RequestTooLarge ? 413 : 500);
      output.end();
    });
  });
  async function respond(input: IncomingMessage) {
    const url = requestUrl(input, loopback, config.public_url);
    if (!url) return Response.json({error: "Invalid host or request URL"}, {status: 400});
    const {pathname} = url;
    if (test?.fixture && pathname.startsWith("/__coordination/")) {
      const fixture = await test.fixture(await asRequest(input, url), projects);
      if (fixture) return fixture;
    }
    if (pathname === "/version" && input.method === "GET") return new Response(version);
    const project = apiPath(pathname)?.[1]
      ?? (input.method === "GET" || input.method === "HEAD" ? dashboardRoute(pathname)?.project : undefined);
    if (project && projects.has(project)) return projects.get(project)!.project.fetch(await asRequest(input, url));
    const asset = assets.get(pathname);
    if (asset && (input.method === "GET" || input.method === "HEAD")) {
      return new Response(input.method === "HEAD" ? null : asset.body as Uint8Array<ArrayBuffer>, {headers: asset.headers});
    }
    return Response.json({error: "Unknown route"}, {status: 404});
  }

  server.on("upgrade", (input, socket: Socket, head) => {
    const url = requestUrl(input, loopback, config.public_url);
    const [, project, path] = url && apiPath(url.pathname) || [];
    const mounted = projects.get(project);
    if (!url || !mounted || path !== "/feed" || (input.headers.origin && input.headers.origin !== url.origin)) {
      socket.end("HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n");
      return;
    }
    websocket.handleUpgrade(input, socket, head, connected => mounted.connect(connected));
  });

  try {
    for (const [name, repo] of Object.entries(config.projects)) {
      const file = projectFile(config.data_dir, name);
      if (!test && !existsSync(file)) throw new Error(`Missing ticket database ${file}`);
      const storage = new SqliteStorage(file);
      try {
        if (!test && !storage.sql.exec("SELECT 1 FROM sqlite_master WHERE name = 'meta' AND type = 'table'").toArray().length) {
          throw new Error(`Missing ticket schema in ${file}`);
        }
        projects.set(name, mount(name, repo, storage, config.read_only ?? false));
      } catch (error) {
        storage.close();
        throw error;
      }
    }
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(config.port ?? defaultPort, address, resolve);
    });
    loopback = `http://${address}:${(server.address() as {port: number}).port}`;
    for (const {project} of projects.values()) await project.start();
  } catch (error) {
    server.close();
    websocket.close();
    for (const mounted of projects.values()) mounted.close();
    throw error;
  }

  return {
    url: loopback,
    async close() {
      for (const socket of websocket.clients) socket.terminate();
      const closed = new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
      server.closeIdleConnections();
      // Leave SQLite open while in-flight HTTP requests finish.
      await closed;
      for (const mounted of projects.values()) mounted.close();
      websocket.close();
    },
  };
}
