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
