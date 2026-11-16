"""Replay the checked-in Beads transcript against the real local Worker."""
import datetime as dt
import gzip
from itertools import groupby
import json
import os
import re
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
    "opaque version tokens and newly allocated comment IDs (mapped consistently)",
    "simulator-generated timestamps on fixture tickets and comments",
    "date-only deferral represented in the caller's local timezone",
    "relative list order among tickets with equal creation time and priority",
    "unordered raw dependency records",
    "platform line endings",
)


def read_gzip(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def run(binary, args, directory, env, stdin=None):
    return subprocess.run([binary, *args], cwd=directory, env=env, input=stdin,
                          capture_output=True, text=True, encoding="utf-8")


def ok(binary, args, directory, env):
    reply = run(binary, args, directory, env)
    if reply.returncode:
        raise AssertionError(f"{args}: {reply.returncode}\n{reply.stdout}\n{reply.stderr}")
    return json.loads(reply.stdout)


def request(address, path, payload=None):
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Actor": "shim-test"}
    data = None if payload is None else json.dumps(payload).encode()
    if path.startswith("/test-token/"):
        data = TOKEN.encode()
    with urllib.request.urlopen(urllib.request.Request(address + path, data, headers), timeout=20) as response:
        text = response.read()
        return json.loads(text) if text else None


def parse_time(value):
    value = re.sub(r"\.(\d+)", lambda match: "." + match[1][:6].ljust(6, "0"), value)
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize(value, comment_ids):
    if isinstance(value, list):
        return [normalize(row, comment_ids) for row in value]
    if not isinstance(value, dict):
        return value
    result = {key: normalize(field, comment_ids) for key, field in value.items()}
    if result.get("dependencies") and all("depends_on_id" in row for row in result["dependencies"]):
        result["dependencies"].sort(key=lambda row: (row["depends_on_id"], row["type"]))
    if "revision" in result:
        assert isinstance(result["revision"], str) and result["revision"]
        result["revision"] = "<version>"
    if result.get("id", "").startswith("by-corpus") or result.get("issue_id", "").startswith("by-corpus"):
        for field in ("created_at", "updated_at", "started_at", "closed_at"):
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



def verify_write(address, args, recorded):
    if args[0] == "--actor":
        args = args[2:]
    command = args[0]
    def inspect(id, comments=False):
        return request(address, f"/by/api/tickets/{id}" + ("?comments=1" if comments else ""))

    if command in {"create", "update", "close", "defer", "undefer"}:
        row = recorded if command == "create" else recorded[0]
        native = inspect(row["id"])
        for legacy, key in (
            ("title", "title"), ("status", "status"), ("priority", "priority"), ("issue_type", "type"),
            ("created_by", "created_by"), ("assignee", "assignee"), ("description", "body"),
            ("acceptance_criteria", "acceptance"), ("notes", "notes"), ("close_reason", "close_reason"),
        ):
            assert (native.get(key) or None) == (row.get(legacy) or None), (row["id"], key)
        assert sorted(native["labels"]) == sorted(row.get("labels", []))
        assert {key: value for key, value in native["metadata"].items() if key != "_enact_beads"} == row.get("metadata", {})
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


def replay(tk, bd, directory, env, address):
    rows = read_gzip(CORPUS / "start.jsonl.gz")
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

    calls = read_gzip(CORPUS / "calls.jsonl.gz")
    problems = []
    expected_ids, actual_ids = {}, {}
    for call in calls:
        args = call["argv"]
        reply = run(bd, args, directory, env, call["stdin"])
        if args == EXPORT_ARGS and call["returncode"] == 0:
            # Scotty polls this command. Its replacement is the live dashboard.
            assert (reply.returncode, reply.stdout, reply.stderr) == (1, "", EXPORT_ERROR), reply
            print("Recorded Scotty export rejected after cutover as expected.")
            continue
        want = call["stdout"]
        got = reply.stdout
        if reply.returncode != call["returncode"]:
            problems.append(f"{args}: exit {reply.returncode}, expected {call['returncode']} ({reply.stderr.strip()})")
        if reply.stderr != (call["stderr"] or ""):
            problems.append(f"{args}: stderr {reply.stderr!r}, expected {call['stderr']!r}")
        if "--json" in args and want and got:
            want, got = json.loads(want), json.loads(got)
            if args[0] == "defer":
                assert parse_time(want[0]["defer_until"]).date().isoformat() == args[3]
                assert parse_time(got[0]["defer_until"]).astimezone().date().isoformat() == args[3]
                want[0]["defer_until"] = got[0]["defer_until"] = args[3]
            if args == ["list", "--json"]:
                # Beads and SQLite do not promise an order among equal rank.
                for listing in (want, got):
                    listing[:] = [
                        row for _, tied in groupby(listing, key=lambda row: (row["priority"], row["created_at"]))
                        for row in sorted(tied, key=lambda row: row["id"])
                    ]
            mismatch = difference(normalize(want, expected_ids), normalize(got, actual_ids))
        else:
            mismatch = difference(want or "", got)
        if mismatch:
            problems.append(f"{args}: {mismatch}")
        elif call["returncode"] == 0 and "--json" in args:
            try:
                verify_write(address, args, json.loads(call["stdout"]))
            except AssertionError as error:
                problems.append(f"{args}: persisted ticket differs ({error})")
    assert not problems, "\n".join(problems)
    print(f"{len(calls)} recorded calls, zero unexplained differences.")
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
    paused = subprocess.Popen([bd, "epic", "close-eligible", "--json"], cwd=directory, env=env,
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
    requests_before = request(address, "/gate/api/state")["requests"]
    for args in (EXPORT_ARGS, ["export"], EXPORT_ARGS):
        retired = run(bd, args, directory, env)
        assert (retired.returncode, retired.stdout, retired.stderr) == (1, "", EXPORT_ERROR), retired
    # Only the observation request itself should reach the ticket server.
    assert request(address, "/gate/api/state")["requests"] == requests_before + 1
    output_export = run(bd, ["export", "-o", "backup.jsonl"], directory, env)
    assert (output_export.returncode, output_export.stdout, output_export.stderr) == (
        1, "", EXPORT_ERROR
    ), output_export
    assert not (directory / "backup.jsonl").exists()
    with socket.socket() as offline:
        offline.bind(("127.0.0.1", 0))
        config.write_text(f'[tickets]\nproject = "gate"\nurl = "http://127.0.0.1:{offline.getsockname()[1]}"\n')
        fallback = run(bd, ["create", "--unsupported", "arg with spaces"], directory, env, "")
        assert (fallback.returncode, fallback.stdout, fallback.stderr) == (17, "create|--unsupported|arg with spaces\n", "legacy stderr\n"), fallback
    print("Before, paused, after and unreachable-before pass-through verified.")


def main():
    tk = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory() as scratch:
        directory = Path(scratch)
        bd = directory / f"bd{tk.suffix}"
        fake = directory / f"legacy{tk.suffix}"
        source = directory / "legacy.rs"
        source.write_text("""
use std::io::Read;
fn main() {
    println!("{}", std::env::args().skip(1).collect::<Vec<_>>().join("|"));
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
        env = {**os.environ, "HOME": scratch, "APPDATA": scratch,
               "GIT_CONFIG_GLOBAL": str(directory / "gitconfig"),
               "GIT_CONFIG_NOSYSTEM": "1",
               "ENACT_SHIM_HELPER": str(sync_script), "ENACT_SHIM_PYTHON": sys.executable}
        env.pop("ENACT_REAL_BD", None)
        tokens = directory / ("enact" if os.name == "nt" else ".config/enact")
        tokens.mkdir(parents=True)
        (tokens / "token").write_text(TOKEN)
        (directory / "gitconfig").write_text("[user]\nname = Derek Thurn\nemail = derek@fake.email\n")
        worker = subprocess.Popen(["node", HERE / "shim-simulator.mjs"], cwd=directory,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            address = worker.stdout.readline().strip()
            assert address.startswith("http://"), address
            for project in ("by", "gate"):
                request(address, f"/test-token/{project}")
            (directory / ".enact.toml").write_text(f'[tickets]\nproject = "by"\nurl = "{address}"\n')
            cutover(bd, directory, env, address)
            (directory / ".enact.toml").write_text(f'[tickets]\nproject = "by"\nurl = "{address}"\n')
            replay(tk, bd, directory, env, address)
        finally:
            worker.stdin.close()
            worker.wait(timeout=45)


if __name__ == "__main__":
    main()
