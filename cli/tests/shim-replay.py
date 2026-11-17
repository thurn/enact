"""Replay the checked-in Beads transcript against the real local Worker."""
import argparse
from contextlib import contextmanager
import datetime as dt
import gzip
from itertools import groupby
import json
import os
import re
import shutil
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = Path(__file__).resolve().parent
CORPUS = HERE.parent.parent / "tests" / "corpus"
TOKEN = "shim-replay-token"
EXPORT_ARGS = ["export", "--json"]
EXPORT_ERROR = ("bd: full exports are disabled after ticket cutover. "
                "Use the ticket dashboard or targeted tk list/show reads.\n")
RULES = (
    "JSON whitespace and object key order",
    "opaque version tokens and newly allocated comment and root ticket IDs (mapped consistently)",
    "simulator-generated timestamps on fixture tickets and comments",
    "date-only deferral represented in the caller's local timezone",
    "recorded future snoozes kept future when replayed later",
    "relative list order among tickets with equal creation time and priority",
    "unordered raw dependency records",
    "platform line endings",
    "terminal padding and generated fixture dates in human bd show",
    "order within equally ranked ready explanations and their blocker lists",
)


def read_gzip(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def run(binary, args, directory, env, stdin=None):
    # The recorded stdin is byte-exact, including line endings on Windows.
    data = None if stdin is None else stdin.encode("utf-8", "surrogateescape")
    reply = subprocess.run([binary, *args], cwd=directory, env=env, input=data, capture_output=True)
    stdout, stderr = (stream.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                      for stream in (reply.stdout, reply.stderr))
    return subprocess.CompletedProcess(reply.args, reply.returncode, stdout, stderr)


def ok(binary, args, directory, env):
    reply = run(binary, args, directory, env)
    if reply.returncode:
        raise AssertionError(f"{args}: {reply.returncode}\n{reply.stdout}\n{reply.stderr}")
    return json.loads(reply.stdout)


def request(address, path, payload=None, *, token=TOKEN):
    headers = {"Authorization": f"Bearer {token}", "X-Actor": "shim-test"}
    data = None if payload is None else json.dumps(payload).encode()
    if path.startswith("/test-token/"):
        data = token.encode()
    with urllib.request.urlopen(urllib.request.Request(address + path, data, headers), timeout=20) as response:
        text = response.read()
        return json.loads(text) if text else None


def parse_time(value):
    value = re.sub(r"\.(\d+)", lambda match: "." + match[1][:6].ljust(6, "0"), value)
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize(value, comment_ids, allocated_roots):
    if isinstance(value, list):
        return [normalize(row, comment_ids, allocated_roots) for row in value]
    if not isinstance(value, dict):
        return value
    result = {key: normalize(field, comment_ids, allocated_roots) for key, field in value.items()}
    if result.get("dependencies") and all("depends_on_id" in row for row in result["dependencies"]):
        result["dependencies"].sort(key=lambda row: (row["depends_on_id"], row["type"]))
    if "revision" in result:
        assert isinstance(result["revision"], str) and result["revision"]
        result["revision"] = "<version>"
    ticket_id = result.get("issue_id", result.get("id", ""))
    if ticket_id.startswith("by-corpus") or ticket_id in allocated_roots:
        for field in ("created_at", "updated_at", "started_at", "closed_at", "heartbeat_at", "lease_expires_at"):
            if field in result:
                parse_time(result[field])
                result[field] = "<generated time>"
    if "issue_id" in result and "id" in result and "author" in result:
        assert result["id"]
        result["id"] = comment_ids.setdefault(result["id"], f"comment-{len(comment_ids) + 1}")
    return result


def difference(expected, actual, path=""):
    if type(expected) != type(actual):
        return f"{path} types differ: {type(expected).__name__}, {type(actual).__name__}"
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return f"{path} missing {expected.keys() - actual.keys()}, extra {actual.keys() - expected.keys()}"
        for key in expected:
            mismatch = difference(expected[key], actual[key], f"{path}.{key}")
            if mismatch:
                return mismatch
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path} length: expected {len(expected)}, got {len(actual)}"
        for index, (want, got) in enumerate(zip(expected, actual)):
            mismatch = difference(want, got, f"{path}[{index}] expected_id={want.get('id')} actual_id={got.get('id')}"
                                  if isinstance(want, dict) and isinstance(got, dict) else f"{path}[{index}]")
            if mismatch:
                return mismatch
    elif expected != actual:
        return f"{path}: expected {repr(expected)[:180]}, got {repr(actual)[:180]}"
    return None



def verify_ticket(native, row):
    for legacy, key in (
        ("title", "title"), ("status", "status"), ("priority", "priority"), ("issue_type", "type"),
        ("created_by", "created_by"), ("assignee", "assignee"), ("description", "body"),
        ("acceptance_criteria", "acceptance"), ("design", "design"), ("notes", "notes"),
        ("close_reason", "close_reason"),
    ):
        actual = native[key] if native[key] != "" else None
        assert actual == row.get(legacy), (row["id"], key, actual, row.get(legacy))
    assert sorted(native["labels"]) == sorted(row.get("labels", []))
    metadata = dict(native["metadata"])
    assert metadata.pop("_enact_beads", {}).get("owner") == row.get("owner")
    assert metadata == row.get("metadata", {})


def verify_persisted(address, expected, actual_id, prior_parent):
    native = request(address, f"/by/api/tickets/{actual_id}")
    verify_ticket(native, expected)
    for field in ("closed_at", "defer_until"):
        recorded = expected.get(field)
        if recorded is not None:
            parse_time(native[field])
            if field == "defer_until":
                assert parse_time(native[field]) == parse_time(recorded), field
        else:
            assert native[field] is None, field
    legacy = native["metadata"].get("_enact_beads", {})
    for field in ("started_at", "heartbeat_at", "lease_expires_at"):
        if field in expected:
            parse_time(legacy[field])
        else:
            assert field not in legacy, (actual_id, field)
    if "lease_expires_at" in expected:
        def lease_duration(ticket):
            return parse_time(ticket["lease_expires_at"]) - parse_time(ticket["heartbeat_at"])
        assert lease_duration(legacy) == lease_duration(expected)
    parent = expected.get("parent")
    assert native["parent"] == (parent.removeprefix("by-") if parent else None), "parent"
    expected_edges = []
    for field in ("dependencies", "dependents"):
        for edge in expected.get(field, []):
            kind = edge["dependency_type"]
            if kind == "parent-child":
                continue
            target = edge["id"].removeprefix("by-")
            src, dst = (native["id"], target) if field == "dependencies" else (target, native["id"])
            expected_edges.append((src, dst, "related" if kind == "relates-to" else kind))
    actual_edges = [(edge["src"], edge["dst"], edge["kind"]) for edge in native["edges"]]
    assert sorted(actual_edges) == sorted(expected_edges), (actual_edges, expected_edges)
    if parent:
        assert native["id"] in request(address, f"/by/api/tickets/{parent}")["children"]
    if prior_parent and prior_parent != native["parent"]:
        assert native["id"] not in request(address, f"/by/api/tickets/{prior_parent}")["children"]


def verify_write(address, args, recorded):
    if args[0] == "--actor":
        args = args[2:]
    command = args[0]
    def inspect(id, comments=False):
        return request(address, f"/by/api/tickets/{id}" + ("?comments=1" if comments else ""))

    if command in {"create", "update", "close", "defer", "undefer"}:
        row = recorded if command == "create" else recorded[0]
        native = inspect(row["id"])
        verify_ticket(native, row)
        if command == "create" and "--parent" in args:
            assert "by-" + native["parent"] == args[args.index("--parent") + 1]
        if command == "defer":
            assert parse_time(native["defer_until"]).astimezone().date().isoformat() == args[3]
        if command == "undefer":
            assert native["defer_until"] is None
    elif command == "dep" and args[1] in {"add", "remove"}:
        native = inspect(recorded["issue_id"])
        outgoing = [edge for edge in native["edges"] if edge["src"] == native["id"]
                    and "by-" + edge["dst"] == recorded["depends_on_id"]]
        assert outgoing == ([{"src": native["id"], "dst": recorded["depends_on_id"][3:],
                              "kind": recorded["type"]}] if args[1] == "add" else [])
    elif command == "label" and args[1] in {"add", "remove"}:
        row = recorded[0]
        assert (row["label"] in inspect(row["issue_id"])["labels"]) == (args[1] == "add")
    elif command == "comments" and args[1] == "add":
        comments = inspect(recorded["issue_id"], comments=True)["comments"]
        assert [(row["author"], row["text"]) for row in comments] == [(recorded["author"], recorded["text"])]
    elif command == "epic" and args[1] == "close-eligible":
        for id in recorded["closed"]:
            native = inspect(id)
            assert native["status"] == "closed" and native["close_reason"] == recorded["reason"]


def rank_order(rows):
    # Beads leaves the order among equal priority and creation time unspecified.
    rows[:] = [
        row for _, tied in groupby(rows, key=lambda row: (row["priority"], row["created_at"]))
        for row in sorted(tied, key=lambda row: row["id"])
    ]


def show_text(text):
    # Beads pads its Markdown rendering to the terminal width. The simulator
    # creates the compatibility fixtures on the day the replay runs.
    text = re.sub(r"(?<=: )\d{4}-\d{2}-\d{2}(?= ·|$)", "<fixture date>", text, flags=re.M)
    text = re.sub(r"^  \d{4}-\d{2}-\d{2} \d{2}:\d{2} ", "  <fixture minute> ", text, flags=re.M)
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def retired_export(bd, args, cwd, env, address, project):
    endpoint = f"/{project}/api/state"
    files = (set(cwd.iterdir()), set((cwd / "workspace").iterdir()) if (cwd / "workspace").is_dir() else set())
    requests_before = request(address, endpoint)["requests"]
    reply = run(bd, args, cwd, env)
    assert (reply.returncode, reply.stdout, reply.stderr) == (1, "", EXPORT_ERROR), reply
    assert files[0] == set(cwd.iterdir()), args
    if (cwd / "workspace").is_dir():
        assert files[1] == set((cwd / "workspace").iterdir()), args
    # Reading the counter itself accounts for the sole additional request.
    assert request(address, endpoint)["requests"] == requests_before + 1, args


def replay(tk, bd, directory, env, address, corpus):
    rows = read_gzip(corpus / "start.jsonl.gz")
    # A ticket due after the recording was snoozed for every captured call.
    # Keep that relationship independent of the day CI runs this replay.
    snoozes = {row["id"]: row["defer_until"] for row in rows
               if row["status"] == "deferred" and row.get("defer_until")}
    future = "2099-01-01T08:00:00Z"
    for row in rows:
        if row["id"] in snoozes:
            row["defer_until"] = future
    export = directory / "start.jsonl"
    history = directory / "history"
    history.mkdir()
    with export.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
            # The corpus contains an export, which does not include changelog
            # events. Seed the importer's cache with the history available.
            (history / (row["id"] + ".json")).write_text(json.dumps({"source": row, "events": []}))
    imported = ok(tk, ["--json", "import", str(export), "--history-dir", str(history)], directory, env)
    print(f"Import report: {imported}")
    assert imported["imported"] == len(rows), imported
    request(address, "/by/api/state", {"cutover": "paused"})
    request(address, "/by/api/state", {"cutover": "after"})

    calls = read_gzip(corpus / "calls.jsonl.gz")
    problems = []
    expected_ids, actual_ids = {}, {}
    allocated_roots = {}
    # The first read observes cutover and saves the permanent local marker.
    ok(bd, ["list", "--id", "by-r61mr", "--json"], directory, env)
    retired = 0
    for call in calls:
        args = [allocated_roots.get(arg, arg) for arg in call["argv"]]
        cwd = (directory / call["cwd"]).resolve()
        assert cwd.is_relative_to(directory.parent.resolve()), call["cwd"]
        cwd.mkdir(parents=True, exist_ok=True)
        for name, contents in call.get("files", {}).items():
            path = (cwd / name).resolve()
            assert path.is_relative_to(cwd), name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents.encode("utf-8"))
        if call["command"] == ["export"]:
            retired_export(bd, args, cwd, env, address, "by")
            retired += 1
            continue
        persisted = call.get("persisted")
        rejected = call.get("unchanged_ticket")
        prior_parent = None
        if persisted and call["command"] == ["update"]:
            prior_parent = request(address, f"/by/api/tickets/{persisted['id']}")["parent"]
        if rejected:
            before_ticket = request(address, f"/by/api/tickets/{rejected}?history=1")
        unchanged = rejected or ("--readonly" in args and call["command"] == ["update"]) or (
            call["command"] in (["create"], ["close"]) and call["returncode"] != 0)
        if unchanged:
            before = request(address, "/by/api/state")["seq"]
        reply = run(bd, args, cwd, env, call["stdin"])
        if unchanged:
            assert request(address, "/by/api/state")["seq"] == before, args
        if rejected:
            assert request(address, f"/by/api/tickets/{rejected}?history=1") == before_ticket, args
        want = call["stdout"]
        got = reply.stdout
        if reply.returncode != call["returncode"]:
            problems.append(f"{args}: exit {reply.returncode}, expected {call['returncode']} ({reply.stderr.strip()})")
            continue
        if reply.stderr != (call["stderr"] or ""):
            problems.append(f"{args}: stderr {reply.stderr!r}, expected {call['stderr']!r}")
        if "--json" in args and want and got:
            want, got = json.loads(want), json.loads(got)
            if call.get("allocated_root"):
                actual_id = got["id"]
                assert re.fullmatch(r"by-[a-hj-km-np-tv-z][0-9a-hj-km-np-tv-z]{3}", actual_id), actual_id
                assert actual_id not in allocated_roots.values(), actual_id
                allocated_roots[want["id"]] = actual_id
                got["id"] = want["id"]
            if args[0] == "defer":
                assert parse_time(want[0]["defer_until"]).date().isoformat() == args[3]
                assert parse_time(got[0]["defer_until"]).astimezone().date().isoformat() == args[3]
                want[0]["defer_until"] = got[0]["defer_until"] = args[3]
            for row in got if isinstance(got, list) else [got]:
                if isinstance(row, dict) and row.get("id") in snoozes and row.get("defer_until") == future:
                    row["defer_until"] = snoozes[row["id"]]
            if call["command"][0] in {"list", "ready"}:
                for listing in (want, got):
                    if "--explain" in args:
                        for field in ("ready", "blocked"):
                            rank_order(listing[field])
                        for row in listing["ready"]:
                            if row["resolved_blockers"]:
                                row["resolved_blockers"].sort()
                        for row in listing["blocked"]:
                            row["blocked_by"].sort(key=lambda blocker: blocker["id"])
                    else:
                        rank_order(listing)
            mismatch = difference(normalize(want, expected_ids, allocated_roots),
                                  normalize(got, actual_ids, allocated_roots))
        elif call["command"] == ["show"] and args[1:3] == ["by-corpus.2", "by-corpus.1"]:
            mismatch = difference(show_text(want), show_text(got))
        else:
            if call["command"] == ["close"] and "persisted" in call:
                old_id = call["persisted"]["id"]
                want = want.replace(old_id, allocated_roots.get(old_id, old_id), 1)
            mismatch = difference(want or "", got)
        if mismatch:
            problems.append(f"{args}: {mismatch}")
        elif persisted:
            try:
                id = persisted["id"]
                verify_persisted(address, persisted, allocated_roots.get(id, id), prior_parent)
            except AssertionError as error:
                problems.append(f"{args}: persisted ticket differs ({error})")
        elif call["returncode"] == 0 and "--json" in args:
            try:
                verify_write(address, args, json.loads(call["stdout"]))
            except AssertionError as error:
                problems.append(f"{args}: persisted ticket differs ({error})")
        elif call["returncode"] == 0 and call["command"] == ["comments", "add"] and "--help" not in args:
            author, fields = (args[1], args[2:]) if args[0] == "--actor" else ("Derek Thurn", args)
            comments = request(address, f"/by/api/tickets/{fields[2]}?comments=1")["comments"]
            assert (comments[-1]["author"], comments[-1]["text"]) == (author, fields[3])
    assert not problems, "\n".join(problems)
    print(f"{len(calls)} recorded calls, zero unexplained exit, stdout or stderr differences.")
    print(f"{retired} retired export shapes wrote no file and sent no ticket-server request after observing cutover.")
    print("Normalization rules: " + "; ".join(RULES))


def cutover(bd, directory, env, address):
    config = directory / ".enact.toml"
    config.write_text(f'[tickets]\nproject = "gate"\nurl = "{address}"\n')
    before = run(bd, ["show", "arg with spaces", "--json"], directory, env, "in\n")
    assert (before.returncode, before.stdout, before.stderr) == (17, 'show|arg with spaces|--json\nin\n', "legacy stderr\n"), before
    legacy_export = run(bd, EXPORT_ARGS, directory, env, "")
    assert (legacy_export.returncode, legacy_export.stdout, legacy_export.stderr) == (
        17, "export|--json\n", "legacy stderr\n"
    ), legacy_export
    request(address, "/gate/api/state", {"cutover": "paused"})
    caller = directory / "caller"
    caller.mkdir()
    paused = subprocess.Popen([bd, "-C", "..", "epic", "close-eligible", "--json"], cwd=caller, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # Wait until the Worker acknowledges the drain before completing the
        # transition. The subprocess must stay blocked until the feed wakes it.
        deadline = time.monotonic() + 45
        events = []
        while not events and time.monotonic() < deadline:
            if paused.poll() is not None:
                raise AssertionError(paused.communicate())
            events = request(address, "/gate/api/events?kind=machine")
            if not events:
                time.sleep(0.05)
        assert len(events) == 1 and events[0]["data"]["drained"] is True, events
        assert paused.poll() is None
        request(address, "/gate/api/state", {"cutover": "after"})
        stdout, stderr = paused.communicate(timeout=45)
        assert not stderr, stderr
        assert json.loads(stdout) == {"closed": [], "count": 0, "reason": "All children completed", "schema_version": 1}
        assert paused.returncode == 0
    finally:
        if paused.poll() is None:
            paused.kill()
            paused.wait()
    assert (directory / "synced").read_text() == "sync\n"
    retired_export(bd, ["export", "-o", "backup.jsonl"], directory, env, address, "gate")
    with socket.socket() as offline:
        offline.bind(("127.0.0.1", 0))
        config.write_text(f'[tickets]\nproject = "gate"\nurl = "http://127.0.0.1:{offline.getsockname()[1]}"\n')
        fallback = run(bd, ["create", "--unsupported", "arg with spaces"], directory, env, "")
        assert (fallback.returncode, fallback.stdout, fallback.stderr) == (17, "create|--unsupported|arg with spaces\n", "legacy stderr\n"), fallback
    print("Before, paused, after and unreachable-before pass-through verified.")


def dated_deferrals(tk, bd, directory, env, address):
    repo = directory / "snooze"
    repo.mkdir()
    env = {**env, "ENACT_TK": str(tk), "ENACT_SESSION": "vizier", "ENACT_NO_SELF_UPDATE": "1"}
    for args in (["init", "-q"], ["commit", "-qm", "Initial commit", "--allow-empty"],
                 ["branch", "-M", "master"], ["branch", "staging"], ["remote", "add", "origin", "."]):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True)
    (repo / ".enact.toml").write_text(f'[tickets]\nbackend = "server"\nproject = "snooze"\nurl = "{address}"\n')

    request(address, "/test-token/snooze")
    epic, snoozed, indefinite = [row["id"] for row in request(address, "/snooze/api/batch", {"ops": [
        {"op": "create", "fields": {"title": "Snoozed work", "type": "epic"}},
        {"op": "create", "parent": "$0", "fields": {"title": "Shim snooze", "priority": 0, "labels": ["snooze-test"]}},
        {"op": "create", "parent": "$0", "fields": {"title": "Indefinite", "notes": "Original", "labels": ["snooze-test"]}},
    ]})["results"]]
    imported = f"{epic}.3"
    snapshot = {
        "id": imported, "parent": epic, "next_child": 1, "title": "Imported snooze",
        "body": "", "design": "", "notes": "", "acceptance": "", "type": "task",
        "status": "deferred", "priority": 2, "assignee": None, "last": 0, "labels": ["snooze-test"], "metadata": {},
        "defer_until": "2099-01-01T00:00:00Z", "close_reason": None, "closed_at": None,
        "created_by": "legacy", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "edges": [], "comments": [], "history": [], "aliases": [f"snooze-{imported}"],
    }
    request(address, "/snooze/api/import", {"tickets": [snapshot]})
    for state in ("paused", "after"):
        request(address, "/snooze/api/state", {"cutover": state})

    def ready():
        return {row["id"]: row for row in ok(bd, ["ready", "--label", "snooze-test", "--json"], repo, env)}

    helper = HERE.parent.parent / "scripts" / "enact.py"

    def next_ticket():
        return ok(sys.executable, [str(helper), "next", "--epic", epic], repo, env)

    result, = ok(bd, ["defer", f"snooze-{snoozed}", "--until", "2099-01-01", "--json"], repo, env)
    assert result["status"] == "deferred", result
    indefinite_alias = f"snooze-{indefinite}"
    icebox = run(bd, ["defer", indefinite_alias, "--reason", "Waiting"], repo, env)
    assert (icebox.returncode, icebox.stdout, icebox.stderr) == (0, f"* Deferred {indefinite_alias}\n", "")
    saved = request(address, f"/snooze/api/tickets/{indefinite}")
    assert (saved["status"], saved["defer_until"], saved["notes"]) == ("deferred", None, "Original\nWaiting")
    assert ready() == {}, ready()
    waiting = next_ticket()
    assert (waiting["action"], waiting["reason"]) == ("wait", "scheduled work"), waiting

    seq = request(address, "/snooze/api/state")["seq"]
    request(address, "/test-expire-snoozes", {})
    rows = ready()
    assert set(rows) == {f"snooze-{snoozed}", f"snooze-{imported}"}, rows
    assert all(row["status"] == "open" and "defer_until" not in row for row in rows.values()), rows
    wake = ok(tk, ["--json", "wait", "change", "--since", str(seq), "--timeout", "15"], repo, env)
    assert set(wake["tickets"]) == {snoozed, imported}, wake

    picked = next_ticket()
    assert (picked["action"], picked["bead"]) == ("spawn", snoozed), picked
    assert indefinite_alias not in ready()
    wake = run(bd, ["undefer", indefinite_alias], repo, env)
    assert (wake.returncode, wake.stdout, wake.stderr) == (0, f"* Undeferred {indefinite_alias} (now open)\n", "")
    assert indefinite_alias in ready()
    print("Imported and shim snoozes agree across bd ready, the change feed, and enact next.")


def migration_compare(tk, fake, directory, env, address):
    work = directory / "migration"
    work.mkdir()
    (work / ".enact.toml").write_text(f'[tickets]\nproject = "migration"\nurl = "{address}"\n')
    request(address, "/test-token/migration")
    legacy = {**env, "ENACT_REAL_BD": str(fake), "SHIM_TEST_SOURCE": str(work)}
    export = work / "export"
    ready = work / "ready"
    history = work / "history-cache"
    timestamp = "2026-11-16T08:00:00Z"
    body = "雪\r\n‘quoted’\t" * 14_000

    def issue(id, parent=None, blocker=None, **fields):
        row = {"id": "by-" + id, "title": id, "issue_type": "task", "status": "open",
               "priority": 2, "created_at": timestamp, "updated_at": timestamp, **fields}
        row["dependencies"] = [
            {"issue_id": row["id"], "depends_on_id": "by-" + target, "type": kind}
            for target, kind in ((parent, "parent-child"), (blocker, "blocks")) if target
        ]
        return row

    events = [{"id": "first", "created_at": timestamp, "event_type": "created"},
              {"id": "second", "created_at": "2026-11-16T09:00:00Z", "event_type": "updated"}]
    (work / "history").write_text(json.dumps(events))
    comment = {"id": "source-comment", "author": "Ada", "created_at": timestamp, "text": "雪\r\ncomment"}
    container = issue("container")
    leaf = issue("elsewhere.3", parent="container", description=body, comments=[comment])
    gate = issue("gate", blocker="elsewhere.3")
    below_gate = issue("gate.1", parent="gate")
    closed = issue("closed", parent="container", status="closed")
    rows = [container, leaf, gate, below_gate, closed]
    export.write_text("".join(json.dumps(row) + "\n" for row in rows))
    ready.write_text(json.dumps([container, leaf]))

    copy = ["--json", "import", str(export), "--history-dir", str(history)]
    paused = run(tk, [*copy, "--row-budget", "16"], work, legacy)
    assert paused.returncode == 5, paused
    first = json.loads(paused.stdout)
    assert 0 < first["imported"] < len(rows), first
    assert ok(tk, copy, work, legacy)["imported"] == len(rows) - first["imported"]
    assert ok(tk, copy, work, legacy)["rows_written"] == 0
    saved = request(address, "/migration/api/tickets/by-elsewhere.3?comments=1&history=1")
    assert saved["id"] == "elsewhere.3" and saved["parent"] == "container"
    assert saved["body"] == body and saved["comments"][0]["text"] == comment["text"]
    assert saved["history"][0]["change"] == events[::-1]

    compare = ["--json", "compare", "--history-dir", str(history)]
    expected = [{"id": "container", "beads": True, "server": False, "live_children": ["elsewhere.3"]}]
    assert ok(tk, compare, work, legacy) == {
        "compared": len(rows), "differences": [], "expected_readiness_differences": expected,
    }
    # The old report omits a ready leaf and claims a container is ready
    # despite an active blocker in addition to its own child.
    ready.write_text(json.dumps([container, gate]))
    drift = run(tk, compare, work, legacy)
    assert drift.returncode == 1, drift
    result = json.loads(drift.stdout)
    assert result["expected_readiness_differences"] == expected
    assert [(d["id"], d["fields"], d["ready"]["beads"], d["ready"]["server"])
            for d in result["differences"]] == [
        ("elsewhere.3", [], False, True), ("gate", [], True, False),
    ]
    assert {row["rule"] for row in result["differences"][1]["ready"]["blocked_by"]} == {2, 5}

    ready.write_text(json.dumps([container, leaf]))
    ok(tk, ["--json", "update", "elsewhere.3", "--title", "Changed on server"], work, legacy)
    drift = run(tk, compare, work, legacy)
    assert drift.returncode == 1, drift
    assert "title" in json.loads(drift.stdout)["differences"][0]["fields"]
    print("Migration compare accepts rule 5 only and fails on leaf, mixed blocker and field drift.")


@contextmanager
def simulator(directory):
    with subprocess.Popen(["node", HERE / "shim-simulator.mjs"], cwd=directory,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as worker:
        try:
            address = worker.stdout.readline().strip()
            assert address.startswith("http://"), address
            yield address
        finally:
            worker.stdin.close()
            try:
                worker.wait(timeout=45)
            except subprocess.TimeoutExpired:
                worker.kill()
                raise


def migration_audit(tk, directory, env):
    on_path = Path(shutil.which("bd") or "bd").absolute()
    paired = on_path.with_name(f"bd-real{on_path.suffix}")
    legacy = Path(os.environ.get("ENACT_REAL_BD") or (paired if paired.is_file() else on_path))
    assert legacy.is_file(), "the real Beads CLI is required for the audit regression"
    assert shutil.which("dolt"), "the Dolt CLI is required for the audit regression"
    env = {**env, "ENACT_REAL_BD": str(legacy), "DOLT_DISABLE_EVENT_FLUSH": "1",
           "BD_DISABLE_METRICS": "1", "BD_DISABLE_EVENT_FLUSH": "1"}
    live = directory / "live"
    frozen = directory / "frozen"
    live.mkdir()
    frozen.mkdir()

    def bd(*args):
        reply = run(legacy, args, live, env)
        assert reply.returncode == 0, reply
        return reply.stdout

    subprocess.run(["git", "init", "-q"], cwd=live, env=env, check=True)
    subprocess.run(["git", "init", "-q"], cwd=frozen, env=env, check=True)
    bd("init", "--prefix", "en", "--skip-agents", "--skip-hooks", "--non-interactive")
    ticket = json.loads(bd("create", "--title", "Audit history fixture", "--json"))["id"]
    initial = bd("--readonly", "--sandbox", "export")
    export = directory / "rehearsal.jsonl"
    export.write_text(initial, encoding="utf-8")
    history = directory / "history-cache"
    cached_import = ["--json", "import", str(export), "--history-dir", str(history)]

    with simulator(directory) as address:
        request(address, "/test-token/en")
        (live / ".enact.toml").write_text(f'[tickets]\nproject = "en"\nurl = "{address}"\n')
        assert ok(tk, cached_import, live, env)["imported"] == 1
        assert (history / f"{ticket}.json").is_file()
        path = f"/en/api/tickets/{ticket}?history=1"
        before = request(address, path)["history"][0]["change"]
        assert [event["event_type"] for event in before] == ["created"]

        bd("label", "add", ticket, "temporary")
        bd("label", "remove", ticket, "temporary")
        assert bd("--readonly", "--sandbox", "export") == initial
        expected = json.loads(bd("--readonly", "--sandbox", "history", ticket, "--events", "--json"))
        assert {event["event_type"] for event in expected} == {"created", "label_added", "label_removed"}
        shutil.copytree(live / ".beads", frozen / ".beads")
        bd("update", ticket, "--title", "New live title")

        # The final gate uses the copy even with the live repo as cwd and an
        # ambient Beads override. It must see the stale server as a mismatch.
        env["BEADS_DIR"] = str(live / ".beads")
        ok(tk, ["--json", "state", "--set-cutover", "paused"], live, env)
        frozen_flag = ["--frozen-beads", str(frozen)]
        compare = ["--json", "compare", *frozen_flag]
        drift = run(tk, compare, live, env)
        assert drift.returncode == 1, drift
        assert json.loads(drift.stdout)["differences"] == [
            {"id": ticket.removeprefix("en-"), "fields": ["history"], "ready": None},
        ]
        frozen_import = ["--json", "import", *frozen_flag]
        assert ok(tk, frozen_import, live, env)["imported"] == 1
        assert ok(tk, frozen_import, live, env)["imported"] == 0
        assert ok(tk, compare, live, env)["differences"] == []
        saved = request(address, path)
        assert saved["title"] == "Audit history fixture"
        assert sorted(saved["history"][0]["change"], key=lambda event: event["id"]) == sorted(
            expected, key=lambda event: event["id"],
        )
        print("Frozen Dolt history catches and repairs audit drift with an identical Beads export.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tk", type=Path)
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--replay-only", action="store_true")
    args = parser.parse_args()
    tk = args.tk.resolve()
    with tempfile.TemporaryDirectory() as scratch:
        directory = Path(scratch)
        env = {**os.environ, "HOME": scratch, "APPDATA": scratch,
               "GIT_CONFIG_GLOBAL": str(directory / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
        tokens = directory / ("enact" if os.name == "nt" else ".config/enact")
        tokens.mkdir(parents=True)
        (tokens / "token").write_text(TOKEN)
        (directory / "gitconfig").write_text("[user]\nname = Derek Thurn\nemail = derek@fake.email\n")
        for key in tuple(env):
            if key.upper().startswith("BEADS_"):
                env.pop(key)
        if args.audit:
            migration_audit(tk, directory, env)
            return

        bd = directory / f"bd{tk.suffix}"
        fake = directory / f"legacy{tk.suffix}"
        help_output = directory / "help"
        help_output.mkdir()
        for case in read_gzip(args.corpus / "calls.jsonl.gz"):
            if case["argv"][-1:] == ["--help"] and case["command"] != ["export"]:
                (help_output / "_".join(case["argv"][:-1])).write_text(
                    case["stdout"], encoding="utf-8", newline="\n")
        source = directory / "legacy.rs"
        source.write_text("""
use std::io::Read;
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if let Some(source) = std::env::var_os("SHIM_TEST_SOURCE") {
        let command = args.iter().find(|arg| !arg.starts_with("--")).unwrap();
        assert!(matches!(command.as_str(), "export" | "history" | "ready"));
        let file = std::path::PathBuf::from(source).join(command);
        print!("{}", std::fs::read_to_string(file).unwrap());
        return;
    }
    if args.last().is_some_and(|arg| arg == "--help") {
        let file = std::path::PathBuf::from(std::env::var_os("SHIM_TEST_HELP").unwrap()).join(args[..args.len()-1].join("_"));
        print!("{}", std::fs::read_to_string(file).unwrap());
        return;
    }
    println!("{}", args.join("|"));
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).unwrap();
    print!("{input}");
    eprintln!("legacy stderr");
    std::process::exit(17);
}
""")
        subprocess.run(["rustc", str(source), "-o", str(fake)], check=True)
        installer = HERE.parent.parent / "scripts" / "install_bd_shim.py"
        subprocess.run([sys.executable, installer, tk, fake, bd], check=True)
        sync_script = directory / "sync.py"
        sync_script.write_text(
            'import os, sys\nfrom pathlib import Path\n'
            'assert os.environ["ENACT_SYNC_NOW"] == "1"\n'
            'assert sys.argv[1:] == ["sync"]\n'
            'with Path("synced").open("a") as file: file.write("sync\\n")\n'
            "print('{\"remote\": true}')\n"
        )
        env.update({"SHIM_TEST_HELP": str(help_output), "ENACT_SHIM_HELPER": str(sync_script),
                    "ENACT_SHIM_PYTHON": sys.executable})
        env.pop("ENACT_REAL_BD", None)
        with simulator(directory) as address:
            for project in ("by", "gate"):
                request(address, f"/test-token/{project}")
            (directory / ".enact.toml").write_text(f'[tickets]\nproject = "by"\nurl = "{address}"\n')
            if not args.replay_only:
                cutover(bd, directory, env, address)
            (directory / ".enact.toml").unlink()
            workspace = directory / "workspace"
            workspace.mkdir()
            (workspace / ".enact.toml").write_text(f'[tickets]\nproject = "by"\nurl = "{address}"\n')
            replay(tk, bd, workspace, env, address, args.corpus)
            if not args.replay_only:
                dated_deferrals(tk, bd, directory, env, address)
                migration_compare(tk, fake, directory, env, address)


if __name__ == "__main__":
    main()
