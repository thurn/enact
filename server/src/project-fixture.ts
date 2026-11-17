import {afterEach} from "vitest";
import {SqliteStorage} from "./local/sqlite";
import {TicketProject} from "./project";
import {localUsage} from "./project-usage";
import type {FeedSocket, FeedTransport} from "./ticket-runtime";

const fixtures: Array<() => Promise<void>> = [];
afterEach(async () => {
  for (const close of fixtures.splice(0)) await close();
});

export function projectFixture(name: string, repository?: string) {
  const storage = new SqliteStorage(":memory:");
  const sockets = new Set<FeedSocket>();
  const pending: Promise<void>[] = [];
  let deadline: number | null = null;
  const getAlarm = async () => deadline;
  const feed: FeedTransport = {
    sockets: () => sockets,
    upgrade: () => Response.json({error: "Expected a WebSocket upgrade"}, {status: 400}),
    waitUntil: promise => { pending.push(promise); },
  };
  const open = () => new TicketProject(name, repository, {
    storage, feed, usage: localUsage(), getAlarm, setAlarm: async at => { deadline = at; },
  });
  let project = open();
  fixtures.push(async () => {
    sockets.clear();
    try {
      await Promise.all(pending);
    } finally {
      storage.close();
    }
  });
  return {
    fetch: (url: string, init?: RequestInit) => project.fetch(new Request(url, init)),
    inspect: <T>(inspect: (project: TicketProject) => T) => inspect(project),
    storage, feed, getAlarm,
    reload: () => { project = open(); },
    async alarm() {
      if (deadline === null) return false;
      deadline = null;
      await project.alarm();
      return true;
    },
    connectFeed() {
      const events = new EventTarget();
      let cursor: number | null = null;
      const peer: FeedSocket = {
        send: data => events.dispatchEvent(new MessageEvent("message", {data})),
        close: (code, reason) => {
          sockets.delete(peer);
          events.dispatchEvent(new MessageEvent("close", {data: {code, reason}}));
        },
        serializeAttachment: value => { cursor = value; },
        deserializeAttachment: () => cursor,
      };
      sockets.add(peer);
      return {
        send: (message: string) => project.feed.subscribe(peer, message),
        close: () => peer.close(1000, ""),
        addEventListener: events.addEventListener.bind(events),
      };
    },
  };
}
