import {ProjectUsage} from "./project-usage";
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

export function registeredMachine(accepted: string | undefined, hash: string): string | null | undefined {
  const hashes = JSON.parse(accepted ?? "[]") as string[] | Record<string, string>;
  if (Array.isArray(hashes)) return hashes.includes(hash) ? null : undefined;
  return hashes[hash];
}

const refreshInterval = 60_000;
const tokenRef = /^refs\/enact\/tokens\/([a-z0-9][a-z0-9_-]*)\/([a-f0-9]{64})$/;

export class TokenRegistry {
  private pending?: Promise<void>;

  constructor(private readonly storage: DurableObjectStorage, private readonly store: TicketStore) {}

  async lookup(repository: string | undefined, hash: string): Promise<string | null | undefined> {
    const accepted = registeredMachine(this.store.meta("token_hashes"), hash);
    if (accepted !== undefined || typeof repository !== "string" || !repository) return accepted;

    if (!this.pending) {
      const now = Date.now();
      const lastAttempt = this.store.meta("token_fetched_at");
      if (lastAttempt !== undefined && now - Number(lastAttempt) < refreshInterval) return;
      // Record attempts before sending them so even errors or eviction cannot
      // cause a burst of requests to GitHub.
      this.save("token_fetched_at", String(now));
      this.pending = this.refresh(repository).finally(() => { this.pending = undefined; });
    }
    await this.pending;
    return registeredMachine(this.store.meta("token_hashes"), hash);
  }

  private async refresh(repository: string): Promise<void> {
    let refs: unknown;
    try {
      const response = await fetch(`https://api.github.com/repos/${repository}/git/matching-refs/enact/tokens`, {
        headers: {Accept: "application/vnd.github+json", "User-Agent": "enact-tickets"},
        redirect: "error",
        signal: AbortSignal.timeout(5_000),
      });
      if (!response.ok) return;
      refs = await response.json();
    } catch {
      return;
    }
    if (!Array.isArray(refs)) return;

    const accepted = new Map<string, string>();
    for (const entry of refs) {
      if (typeof entry?.ref !== "string") continue;
      const match = tokenRef.exec(entry.ref);
      if (match) accepted.set(match[2], match[1]);
    }
    this.save("token_hashes", JSON.stringify(Object.fromEntries(accepted)));
  }

  private save(key: string, value: string): void {
    this.storage.transactionSync(() => {
      const usage = new ProjectUsage(this.store.sql, Date.now());
      usage.check(1);
      this.store.sql.exec("INSERT OR REPLACE INTO meta VALUES (?, ?)", key, value);
      // The route accounts for the request separately from the cache writes.
      usage.record(1, 0);
    });
  }
}
