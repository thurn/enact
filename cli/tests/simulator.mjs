// Exercise the CLI over HTTP and WebSocket with deterministic API replies.
import {createServer} from "node:http";
import {WebSocketServer} from "../../server/node_modules/ws/wrapper.mjs";
import protocol, {openFeed} from "./simulator-protocol.mjs";

function url(input) {
  return "http://127.0.0.1:" + server.address().port + input.url;
}

const server = createServer(async (input, output) => {
  try {
    const chunks = [];
    for await (const chunk of input) chunks.push(chunk);
    const request = new Request(url(input), {
      method: input.method, headers: input.headers,
      body: input.method === "POST" ? Buffer.concat(chunks) : undefined,
    });
    const response = await protocol.fetch(request);
    output.writeHead(response.status, Object.fromEntries(response.headers));
    output.end(Buffer.from(await response.arrayBuffer()));
  } catch (error) {
    console.error(error);
    output.writeHead(500);
    output.end();
  }
});
const websocket = new WebSocketServer({noServer: true});
server.on("upgrade", (input, socket, head) => {
  const request = new Request(url(input), {headers: input.headers});
  if (!/^\/[^/]+\/api\/feed$/.test(new URL(request.url).pathname)) {
    socket.end("HTTP/1.1 404 Not Found\r\n\r\n");
    return;
  }
  websocket.handleUpgrade(input, socket, head, (connected) => openFeed(connected, request));
});

await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
console.log(url({url: ""}));
process.stdin.resume();
process.stdin.once("end", () => {
  for (const socket of websocket.clients) socket.terminate();
  websocket.close();
  server.close();
});
