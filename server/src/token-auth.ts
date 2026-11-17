import type {UsageMeter} from "./project-usage";
import type {TicketStore} from "./ticket-store";
import {ApiError, bad} from "./ticket-types";

export type Identity = {actor: string; machine: string};

// A hash can resolve to a registered machine, or null for the flat test set.
export type TokenLookup = (hash: string) => string | null | undefined | Promise<string | null | undefined>;

export async function authorizeWrite(request: Request, lookup: TokenLookup): Promise<Identity> {
  const token = /^Bearer (\S+)$/i.exec(request.headers.get("Authorization") ?? "")?.[1];
  if (token) {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
    const hash = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    const machine = await lookup(hash);
    if (machine !== undefined) {
      const actor = request.headers.get("X-Actor")?.trim() || bad("Missing X-Actor");
      return {actor, machine: machine ?? actor};
    }
  }
  throw new ApiError(401, {error: "Missing or unknown token"});
}

type Tokens = string[] | Record<string, string>;

function registeredMachine(hashes: Tokens, hash: string): string | null | undefined {
  if (Array.isArray(hashes)) return hashes.includes(hash) ? null : undefined;
  return hashes[hash];
}

const refreshInterval = 60_000;
const advertisementType = "application/x-git-upload-pack-advertisement";
const serviceBanner = "001e# service=git-upload-pack\n0000";
const tokenRef = /^(?:[a-f0-9]{40}|[a-f0-9]{64}) refs\/enact\/tokens\/([a-z0-9][a-z0-9_-]*)\/([a-f0-9]{64})\n?$/;

function advertisedTokens(data: Uint8Array): Map<string, string> | undefined {
  const decoder = new TextDecoder();
  if (decoder.decode(data.subarray(0, serviceBanner.length)) !== serviceBanner) return;

  const tokens = new Map<string, string>();
  // Git's four hex digits count bytes, including the digits themselves.
  for (let start = serviceBanner.length; start + 4 <= data.length;) {
    const header = decoder.decode(data.subarray(start, start + 4));
    if (!/^[a-f0-9]{4}$/.test(header)) return;
    const length = Number.parseInt(header, 16);
    if (length === 0) return start + 4 === data.length ? tokens : undefined;
    if (length < 4 || start + length > data.length) return;
    const [ref] = decoder.decode(data.subarray(start + 4, start + length)).split("\0", 1);
    const match = tokenRef.exec(ref);
    if (match) tokens.set(match[2], match[1]);
    start += length;
  }
  return undefined;
}

export class TokenRegistry {
  private pending?: Promise<void>;
  private accepted?: Tokens;
  private lastAttempt?: number;

  constructor(
    private readonly store: TicketStore,
    private readonly usage: () => UsageMeter,
    private readonly writable: () => boolean,
  ) {}

  private machine(hash: string) {
    this.accepted ??= JSON.parse(this.store.meta("token_hashes") ?? "[]") as Tokens;
    return registeredMachine(this.accepted, hash);
  }

  async lookup(repository: string | undefined, hash: string): Promise<string | null | undefined> {
    const accepted = this.machine(hash);
    if (accepted !== undefined || typeof repository !== "string" || !repository || !this.writable()) return accepted;

    if (!this.pending) {
      const now = Date.now();
      if (this.lastAttempt !== undefined && now - this.lastAttempt < refreshInterval) return;
      // Throttle failed refreshes as well as successful ones.
      this.lastAttempt = now;
      this.pending = this.refresh(repository).finally(() => { this.pending = undefined; });
    }
    await this.pending;
    return this.machine(hash);
  }

  private async refresh(repository: string): Promise<void> {
    let accepted: Map<string, string> | undefined;
    try {
      const response = await fetch(`https://github.com/${repository}.git/info/refs?service=git-upload-pack`, {
        headers: {Accept: advertisementType},
        redirect: "manual",
        signal: AbortSignal.timeout(5_000),
      });
      if (!response.ok || response.headers.get("Content-Type") !== advertisementType) return;
      accepted = advertisedTokens(new Uint8Array(await response.arrayBuffer()));
    } catch {
      return;
    }
    if (!accepted || !this.writable()) return;
    const tokens = Object.fromEntries(accepted);
    this.store.sql.exec("INSERT OR REPLACE INTO meta VALUES ('token_hashes', ?)", JSON.stringify(tokens));
    this.usage().record(1, 0);
    this.accepted = tokens;
  }
}
