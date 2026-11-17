"""Single-process CLI fixture for policy that does not need Git or a ticket server.

The real integration suite owns persistence, synchronization, concurrent
claims, filesystem recovery and delivery. This small store exposes only the
Tickets operations needed by the policy scenarios and rejects unknown calls.
"""

import contextlib
import datetime as dt
import subprocess
import io
import json
import os
import tempfile
from pathlib import Path
from unittest import mock


class PolicyWorld:
    def __init__(self, module):
        self.module = module
        self.stack = contextlib.ExitStack()
        self.path = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.repo = module.Repo(self.path, self.path, self.path / ".git", module.Config())
        self.graph, self.dependencies, self.comments = {}, {}, {}
        self.leases = {}
        self.sequence = 0
        for name, kwargs in (
            ("find_repo", {"return_value": self.repo}),
            ("Tickets", {"return_value": self}),
            ("ticket_lease", {"side_effect": self.lease}),
            ("fetch", {"return_value": {}}),
            ("recover_deliveries", {"return_value": []}),
        ):
            self.stack.enter_context(mock.patch.object(module, name, **kwargs))
        self.stack.enter_context(mock.patch.object(module.Repo, "git", return_value=""))
        self.stack.enter_context(mock.patch.object(module.Repo, "git_ok", return_value=False))

    def close(self):
        self.stack.close()

    def create(self, title, **options):
        bead = f"tp-{len(self.graph) + 1}"
        self.graph[bead] = {
            "id": bead, "title": title, "status": "open", "assignee": "",
            "issue_type": options.pop("type", "task"),
            "labels": options.pop("labels", "").split(",") if "labels" in options else [],
            "metadata": {}, "version": 0, "updated_at": self.module.now(), **options,
        }
        return bead

    def show(self, bead):
        row = json.loads(json.dumps(self.graph[bead]))
        row["dependencies"] = [
            {"id": dep, "title": self.graph[dep]["title"], "dependency_type": "blocks"}
            for dep in self.dependencies.get(bead, [])]
        return row

    def all(self):
        return [self.show(bead) for bead in self.graph]

    def ticket(self, command, *args):
        if command == "comments":
            return [{"text": text} for text in self.comments.get(args[0], [])]
        if command == "leases":
            return list(self.leases.values())
        if command == "events":
            return []
        raise AssertionError((command, args))

    def lease(self, repo, action, name, holder, ttl=180, payload=None, **options):
        previous = self.leases.get(name)
        if previous and previous["holder"] != holder:
            return subprocess.CompletedProcess([], 3, "{}", "lease held")
        if action == "release":
            self.leases.pop(name, None)
        else:
            self.leases[name] = {"name": name, "holder": holder, "payload": payload or {},
                                 "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl)).isoformat()}
        return subprocess.CompletedProcess([], 0, "{}", "")

    def ticket_graph(self):
        rows = self.all()
        for ticket in rows:
            ticket["ready"] = ticket["status"] == "open" and all(
                self.graph[dependency]["status"] == "closed"
                for dependency in self.dependencies.get(ticket["id"], []))
        return rows, self.sequence

    def batch(self, *operations, actor=None, conditions=()):
        results = []
        for operation in operations:
            kind = operation["op"]
            name = operation["id"]
            row = self.graph[name]
            if kind == "update":
                row.update(operation.get("set", {}))
                self.set_meta(name, operation.get("set_meta", {}))
                for key in operation.get("unset_meta", []):
                    row["metadata"].pop(key, None)
            elif kind == "claim":
                row.update(assignee=actor, status="in_progress")
            elif kind == "close":
                self.finish(name)
            elif kind == "reopen":
                row["status"] = "open"
            elif kind == "comment":
                self.comments.setdefault(name, []).append(operation["text"])
            else:
                assert kind == "event", kind
            row["updated_at"] = self.module.now()
            row["version"] += 1
            results.append({"ready": [r["id"] for r in self.ticket_graph()[0] if r["ready"]]})
        self.sequence += 1
        return {"seq": self.sequence, "results": results}

    def finish(self, name):
        row = self.graph[name]
        row["status"] = "closed"
        parent = row.get("parent")
        if parent and all(r["status"] == "closed" for r in self.graph.values() if r.get("parent") == parent):
            self.finish(parent)

    def reopen(self, name):
        self.graph[name]["status"] = "open"

    def set_meta(self, bead, pairs, actor=None):
        for key, value in pairs.items():
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                pass
            self.graph[bead]["metadata"][key] = value

    def run(self, command, bead, *args, actor=None):
        if command == "comments":
            assert bead == "add", (command, bead, args)
            self.comments.setdefault(args[0], []).append(args[1])
            return ""
        row = self.graph[bead]
        if command == "close":
            assert args[0] == "--reason", args
            self.finish(bead)
        else:
            assert command == "update", (command, bead, args)
            options = iter(args)
            for key in options:
                if key == "--claim":
                    row.update(assignee=actor, status="in_progress")
                elif key == "--set-metadata":
                    name, value = next(options).split("=", 1)
                    self.set_meta(bead, {name: value})
                elif key == "--unset-metadata":
                    row["metadata"].pop(next(options), None)
                else:
                    assert key in ("--assignee", "--status", "--title"), key
                    row[key[2:]] = next(options)
        row["updated_at"] = self.module.now()
        return ""

    def bd(self, *args):
        if args[:2] == ("dep", "add"):
            self.dependencies.setdefault(args[2], []).append(args[3])
            return
        return self.run(*args)

    def enact(self, *args, code=0, session=None, env=None):
        environment = {
            "ENACT_HOST": "host-a", "ENACT_PROVIDER": "claude", "ENACT_APP": "claude",
            "ENACT_SESSION": session or "sess-a", "CLAUDE_CODE_SESSION_ID": "",
            "CLAUDE_CODE_HOST_SESSION_ID": "", "CODEX_THREAD_ID": "", **(env or {}),
        }
        output = io.StringIO()
        with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(io.StringIO()):
            actual = self.module.main(list(args))
        assert actual == code, (args, actual, code, output.getvalue())
        return json.loads(output.getvalue())
