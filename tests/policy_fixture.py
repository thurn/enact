"""Single-process CLI fixture for policy that does not need git or Dolt.

The real integration suite owns persistence, synchronization, concurrent
claims, filesystem recovery and delivery. This small store exposes only the
Beads operations needed by the policy scenarios and rejects unknown calls.
"""

import contextlib
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
        self.graph, self.dependencies = {}, {}
        for name, kwargs in (
            ("find_repo", {"return_value": self.repo}),
            ("Beads", {"return_value": self}),
            ("sync", {"return_value": {"remote": False, "dropped": []}}),
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
            "metadata": {}, "updated_at": self.module.now(), **options,
        }
        return bead

    def show(self, bead):
        return json.loads(json.dumps(self.graph[bead]))

    def all(self):
        return [self.show(bead) for bead in self.graph]

    def json(self, *args):
        assert args == ("ready", "-n", "0", "--exclude-type", "epic", "--unassigned"), args
        return [bead for bead in self.all() if bead["status"] == "open"
                and bead["issue_type"] != "epic" and not bead["assignee"]
                and all(self.graph[dep]["status"] == "closed"
                        for dep in self.dependencies.get(bead["id"], []))]

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
            return ""
        row = self.graph[bead]
        if command == "close":
            assert args[0] == "--reason", args
            row["status"] = "closed"
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
