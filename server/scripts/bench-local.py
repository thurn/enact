"""Measure end-to-end latency against an isolated ticket project with a release tk."""
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="scratch checkout enrolled in a scratch ticket project")
    parser.add_argument("tk", type=Path, help="release tk executable")
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--concurrent-writes", type=int, default=0)
    parser.add_argument("--check-loopback-targets", action="store_true")
    args = parser.parse_args()
    if args.samples < 1 or args.concurrent_writes < 0:
        parser.error("samples must be positive and concurrent-writes must be nonnegative")
    tk = args.tk.resolve()
    enact = Path(__file__).resolve().parents[2] / "scripts" / "enact.py"
    holder = f"benchmark-{uuid4().hex}"
    env = {**os.environ, "ENACT_TK": str(tk), "ENACT_NO_SELF_UPDATE": "1"}

    def run(command: list[str]) -> dict:
        output = subprocess.run(command, cwd=args.checkout, env=env, capture_output=True, check=True, text=True)
        return json.loads(output.stdout)

    def ticket(*operation: str) -> dict:
        return run([str(tk), "--json", "--actor", holder, "--retry-for", "0", *operation])

    checkout = run([sys.executable, str(enact), "backend"])
    if args.check_loopback_targets and not checkout["url"].startswith("http://127.0.0.1:"):
        parser.error("loopback acceptance targets require the 127.0.0.1 endpoint")
    ident = ticket("create", "--title", holder)["results"][0]["id"]
    lease_args = (holder, "--holder", holder)
    lease = False
    workers = []
    try:
        ticket("lease", "acquire", *lease_args, "--ttl", "300")
        lease = True
        for index in range(args.concurrent_writes):
            workers.append(ticket("create", "--title", f"{holder} worker {index}")["results"][0]["id"])
        probes = {
            "show": (20, lambda _: ticket("show", ident)),
            "ready": (20, lambda _: ticket("ready")),
            "graph_revalidation": (20, None),
            "graph_full": (None, lambda _: ticket("graph", "--all")),
            "update": (35, lambda index: ticket("update", ident, "--notes", str(index))),
            "lease_renew": (35, lambda _: ticket("lease", "renew", *lease_args, "--ttl", "300")),
            "enact_brief": (None, lambda _: run([sys.executable, str(enact), "brief", ident])),
        }
        # Fetch the ETag outside the timed command, including under writer load.
        results = {}
        failed = False
        with ThreadPoolExecutor(max_workers=max(len(workers), 1)) as pool:
            for name, (limit, probe) in probes.items():
                samples = []
                not_modified = 0
                for index in range(args.samples):
                    tag = ticket("graph", "--all")["etag"] if name == "graph_revalidation" else None
                    pending = [pool.submit(ticket, "update", worker, "--notes", uuid4().hex) for worker in workers]
                    started = time.perf_counter()
                    if tag is not None:
                        not_modified += ticket("graph", "--all", "--if-none-match", tag).get("unchanged") is True
                    elif probe is not None:
                        probe(index)
                    samples.append((time.perf_counter() - started) * 1000)
                    for write in pending:
                        write.result()
                samples.sort()
                p95 = samples[math.ceil(len(samples) * 0.95) - 1]
                p99 = samples[math.ceil(len(samples) * 0.99) - 1]
                results[name] = {"p50_ms": round(statistics.median(samples), 2),
                                 "p95_ms": round(p95, 2), "p99_ms": round(p99, 2)}
                if name == "graph_revalidation":
                    results[name]["not_modified"] = not_modified
                    if args.check_loopback_targets and not_modified != len(samples):
                        failed = True
                failed |= args.check_loopback_targets and limit is not None and p95 >= limit
        print(json.dumps({"url": checkout["url"], "samples": args.samples, "timings": results}, indent=2))
        return int(failed)
    finally:
        if lease:
            ticket("lease", "release", *lease_args)
        for created in [*workers, ident]:
            ticket("close", created, "--reason", "Benchmark finished")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        sys.exit(error.stderr.strip() or error.stdout.strip())
