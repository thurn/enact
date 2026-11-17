#!/usr/bin/env python3
"""Stage, switch, and reconcile one explicitly enrolled ticket project."""

import argparse
import datetime as dt
import gzip
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from enact import (ENACT_ROOT, Fail, Repo, dolt_db_dir, emit, find_repo, log,
                   run, ticket_binary, ticket_process, write_json)

MACHINE_WINDOW = dt.timedelta(minutes=10)
REPLAY_WINDOW = dt.timedelta(days=3)
RECONCILE_DELAY = dt.timedelta(hours=1)
PAUSE_TIMEOUT = 300
EVENT_PAGE = 200


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise Fail("Cutover pause timed out")
    return seconds


def tk(repo: Repo, *args: str, deadline: float | None = None, allow_timeout: bool = False):
    reply = ticket_process(repo, *args, timeout=remaining(deadline) if deadline is not None else 120)
    if reply.returncode == 6 and allow_timeout:
        return None
    if reply.returncode:
        try:
            details = json.loads(reply.stdout)
        except ValueError:
            details = {}
        raise Fail(f"tk {args[0]}: {reply.stderr.strip()}", code=reply.returncode, details=details)
    return json.loads(reply.stdout)


def freeze(repo: Repo, destination: Path, deadline: float | None = None) -> None:
    """Back up the embedded working set and pull the remote into that private copy."""
    database = dolt_db_dir(repo)
    state = json.loads((database / ".dolt" / "repo_state.json").read_text(encoding="utf-8"))
    branch = state["head"].removeprefix("refs/heads/")
    remote = state["remotes"][repo.config.remote]["url"]
    beads = destination / ".beads"
    beads.mkdir(mode=0o700, parents=True)
    embedded = beads / "embeddeddolt"
    embedded.mkdir()
    database_copy = embedded / repo.config.ticket_project
    for name in ("config.yaml", "metadata.json"):
        source = repo.main / ".beads" / name
        if source.is_file():
            shutil.copyfile(source, beads / name)

    def dolt(directory: Path, *arguments: str, env: dict[str, str] | None = None) -> None:
        # Dolt's detached send-metrics process can create files in DOLT_ROOT_PATH
        # after restore returns, while TemporaryDirectory removes that root.
        run(["dolt", *arguments], directory, env={**(env or {}), "DOLT_DISABLE_EVENT_FLUSH": "1"},
            timeout=remaining(deadline) if deadline is not None else 300)

    # Beads keeps its audit events and leases in ignored Dolt tables.
    # A clone of the remote drops the working set that contains them.
    with tempfile.TemporaryDirectory(prefix="dolt-backup-", dir=destination) as temporary:
        backup = (Path(temporary) / "backup").as_uri()
        dolt(database, "backup", "sync-url", backup)
        # Restore initializes a database before it can use a local author.
        restore_env = {"DOLT_ROOT_PATH": temporary}
        dolt(embedded, "config", "--global", "--add", "user.name", "Enact cutover", env=restore_env)
        dolt(embedded, "config", "--global", "--add", "user.email", "cutover@localhost", env=restore_env)
        dolt(embedded, "backup", "restore", backup, database_copy.name, env=restore_env)
    dolt(database_copy, "checkout", branch)
    dolt(database_copy, "config", "--local", "--add", "user.name", "Enact cutover")
    dolt(database_copy, "config", "--local", "--add", "user.email", "cutover@localhost")
    dolt(database_copy, "remote", "add", repo.config.remote, remote)
    dolt(database_copy, "pull", repo.config.remote, branch)


def import_frozen(repo: Repo, source: Path, row_budget: int | None, *options: str,
                  deadline: float | None = None) -> dict:
    budget = ["--row-budget", str(row_budget)] if row_budget is not None else []
    return tk(repo, "import", "--frozen-beads", str(source), *budget, *options, deadline=deadline)


def compare(repo: Repo, source: Path, deadline: float | None = None) -> dict:
    return tk(repo, "compare", "--frozen-beads", str(source), deadline=deadline)


def replay(corpus: Path) -> None:
    with gzip.open(corpus / "calls.jsonl.gz", "rt", encoding="utf-8") as records:
        times = [json.loads(line)["start_ns"] for line in records]
    now = time.time_ns()
    if not times or min(times) < now - REPLAY_WINDOW.total_seconds() * 10**9 or max(times) > now:
        raise Fail("Replay corpus must contain calls recorded within the last three days")
    result = run([sys.executable, str(ENACT_ROOT / "cli/tests/shim-replay.py"), str(ticket_binary()),
                  "--corpus", str(corpus), "--replay-only"], ENACT_ROOT, check=False, timeout=300)
    if result.returncode:
        raise Fail(f"Shim replay failed:\n{result.stdout}{result.stderr}")
    log(result.stdout.strip())


def recent_machines(repo: Repo, at: dt.datetime, deadline: float | None = None) -> set[str]:
    events = tk(repo, "events", "--kind", "machine", "--limit", str(EVENT_PAGE), deadline=deadline)
    recent = {event["machine"] for event in events if timestamp(event["at"]) >= at - MACHINE_WINDOW}
    if len(events) == EVENT_PAGE and timestamp(events[-1]["at"]) >= at - MACHINE_WINDOW:
        raise Fail("The recent machine event window exceeds one page")
    return recent


def drain(repo: Repo, pause: dict, machines: set[str], cursor: int, deadline: float) -> None:
    at = pause["cutover_at"]
    machines |= recent_machines(repo, timestamp(at), deadline)
    tk(repo, "drain", deadline=deadline)
    pending = machines.copy()
    while pending:
        state = tk(repo, "state", deadline=deadline)
        if state["cutover"] != "paused" or state["cutover_id"] != pause["cutover_id"]:
            raise Fail("Cutover pause changed while waiting for the fleet")
        # Read the feed cursor before the events. An acknowledgement that
        # arrives after the events query will wake the feed subscription.
        events = tk(repo, "events", "--kind", "machine", "--since", str(cursor),
                    "--limit", str(EVENT_PAGE), deadline=deadline)
        for event in events:
            cursor = event["seq"]
            data = event["data"]
            if (isinstance(data, dict) and data.get("drained") is True
                    and data.get("cutover_at") == at and data.get("cutover_id") == pause["cutover_id"]):
                pending.discard(event["machine"])
        if not pending or len(events) == EVENT_PAGE:
            continue
        try:
            tk(repo, "wait", "change", "--since", str(state["seq"]),
               "--timeout", str(max(0, remaining(deadline) - 1)), deadline=deadline, allow_timeout=True)
        except subprocess.TimeoutExpired:
            raise Fail(f"Timed out waiting for Beads drains from {', '.join(sorted(pending))}") from None
        if deadline - time.monotonic() < 1:
            raise Fail(f"Timed out waiting for Beads drains from {', '.join(sorted(pending))}")


def switch(repo: Repo, archive: Path, machines: set[str], corpus: Path,
           row_budget: int | None, pause_timeout: float) -> dict:
    if archive.exists():
        raise Fail(f"Use a new archive directory: {archive}")
    require_state(repo, "before")
    replay(corpus)
    with tempfile.TemporaryDirectory(prefix="enact-preimport-") as source:
        preflight = Path(source)
        freeze(repo, preflight / "snapshot")
        pre_import = import_frozen(repo, preflight / "snapshot", row_budget, "--before-cutover")
        compare(repo, preflight / "snapshot")
    observed = recent_machines(repo, utc_now())
    if missing := machines - observed:
        raise Fail(f"Missing recent machine events for {', '.join(sorted(missing))}")

    archive.mkdir(parents=True)
    receipt = {"project": repo.config.ticket_project, "url": repo.config.ticket_url,
               "pause_id": uuid.uuid4().hex}
    record = archive / "cutover.json"
    write_json(record, receipt)
    identity = ["--pause-id", receipt["pause_id"]]
    deadline = time.monotonic() + pause_timeout
    before = require_state(repo, "before")
    try:
        pause = tk(repo, "state", "--set-cutover", "paused", *identity, deadline=deadline)
        if pause["cutover"] != "paused" or pause["cutover_id"] != receipt["pause_id"]:
            raise Fail("The server did not acknowledge this cutover pause")
        receipt["cutover_at"] = pause["cutover_at"]
        write_json(record, receipt)
        # Retrying a lost pause reply may return a seq after some peers have
        # already drained. Begin at the last cursor observed before pausing.
        drain(repo, pause, observed, before["seq"], deadline)
        frozen = archive / "frozen"
        freeze(repo, frozen, deadline)
        final = import_frozen(repo, frozen, row_budget, *identity, deadline=deadline)
        comparison = compare(repo, frozen, deadline)
        state = tk(repo, "state", "--set-cutover", "after", *identity, deadline=deadline)
    except (Exception, KeyboardInterrupt) as error:
        # Recover a lost switch reply without ever attempting to reverse after.
        try:
            state = cancel(repo, receipt)
        except (Exception, KeyboardInterrupt) as cleanup:
            raise Fail(f"{error}. Could not check or release the pause: {cleanup}. "
                       f"Use abort with archive {archive}") from error
        if state["cutover"] != "after" or state.get("cutover_id") != receipt["pause_id"]:
            raise
    return {"state": state, "pre_import": pre_import, "final_import": final,
            "comparison": comparison, "archive": str(archive)}


def cancel(repo: Repo, receipt: dict) -> dict:
    state = tk(repo, "state")
    if state.get("cutover_id") != receipt["pause_id"]:
        return state
    if state["cutover"] == "paused":
        state = tk(repo, "state", "--set-cutover", "before", "--pause-id", receipt["pause_id"])
    return state


def require_state(repo: Repo, expected: str) -> dict:
    state = tk(repo, "state")
    if state["cutover"] != expected:
        raise Fail(f"Expected cutover state {expected}, found {state['cutover']}")
    return state


def reconcile(repo: Repo, archive: Path, receipt: dict, row_budget: int | None) -> dict:
    state = require_state(repo, "after")
    if state.get("cutover_id") != receipt["pause_id"] or state["cutover_at"] != receipt["cutover_at"]:
        raise Fail("Archive does not match the completed cutover")
    due = timestamp(state["cutover_at"]) + RECONCILE_DELAY
    if utc_now() < due:
        raise Fail(f"Reconcile after {due.isoformat()}")
    # Retain the late source when the server reports a native edit conflict.
    late = Path(tempfile.mkdtemp(prefix="late-", dir=archive))
    log(f"Reconciliation snapshot: {late}")
    freeze(repo, late)
    result = import_frozen(repo, late, row_budget, "--since-frozen-beads", str(archive / "frozen"))
    shutil.rmtree(late)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    stage = actions.add_parser("preimport", help="Stage a resumable import within the daily budget")
    start = actions.add_parser("start", help="Run the gates, drain the fleet and switch the project")
    abort = actions.add_parser("abort", help="Return an interrupted managed pause to before")
    late = actions.add_parser("reconcile", help="Pull and import subsequent legacy changes")
    for action in (start, abort, late):
        action.add_argument("--archive", type=Path, required=True)
    for action in (stage, start, late):
        action.add_argument("--row-budget", type=int)
    start.add_argument("--machine", action="append", required=True)
    start.add_argument("--replay-corpus", type=Path, required=True)
    start.add_argument("--scotty-stopped", action="store_true",
                       help="Beryl's Scotty export pollers have been stopped")
    start.add_argument("--pause-timeout", type=float, default=PAUSE_TIMEOUT)
    args = parser.parse_args()
    try:
        repo = find_repo()
        if repo.config.ticket_project not in {"by", "en"} or repo.config.ticket_backend == "server":
            raise Fail("Cutover requires an enrolled by or en project using the default backend")
        if args.action == "preimport":
            require_state(repo, "before")
            with tempfile.TemporaryDirectory(prefix="enact-preimport-") as source:
                snapshot = Path(source) / "snapshot"
                freeze(repo, snapshot)
                result = import_frozen(repo, snapshot, args.row_budget, "--before-cutover")
                result["comparison"] = compare(repo, snapshot)
        elif args.action == "start":
            if len(set(args.machine)) < 3:
                raise Fail("Specify the three enrolled machine names with --machine")
            if repo.config.ticket_project == "by" and not args.scotty_stopped:
                raise Fail("Stop Beryl's Scotty export pollers before switching and pass --scotty-stopped")
            if not 0 < args.pause_timeout <= PAUSE_TIMEOUT:
                raise Fail("The pause timeout must be positive and at most 300 seconds")
            result = switch(repo, args.archive.resolve(), set(args.machine), args.replay_corpus.resolve(),
                            args.row_budget, args.pause_timeout)
        else:
            archive = args.archive.resolve()
            receipt = json.loads((archive / "cutover.json").read_text(encoding="utf-8"))
            if (receipt["project"], receipt["url"]) != (repo.config.ticket_project, repo.config.ticket_url):
                raise Fail("Archive belongs to a different ticket project or server")
            result = (cancel(repo, receipt) if args.action == "abort"
                      else reconcile(repo, archive, receipt, args.row_budget))
        emit(result)
        return 0
    except (Fail, KeyError, OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        emit({"error": str(error), **(error.extra if isinstance(error, Fail) else {})})
        return error.code if isinstance(error, Fail) else 1


if __name__ == "__main__":
    sys.exit(main())
