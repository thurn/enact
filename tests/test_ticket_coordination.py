"""Drive Enact and tk against the real Worker in Wrangler's local simulator."""

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import Request, urlopen
from uuid import uuid4

from tests.test_integration import SCRIPT, sh


@unittest.skipUnless(os.environ.get("ENACT_TEST_TK"), "requires the built tk binary")
class TicketCoordinationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        launcher = SCRIPT.parent.parent / "cli" / "tests" / "simulator.mjs"
        worker = subprocess.Popen(["node", str(launcher), "--coordination"], text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        cls.addClassCleanup(worker.stdout.close)
        cls.addClassCleanup(cls.stop_worker, worker)
        cls.address = worker.stdout.readline().strip()
        if not cls.address.startswith("http://"):
            raise RuntimeError("ticket Worker simulator did not start")

    @staticmethod
    def stop_worker(worker):
        worker.stdin.close()
        try:
            worker.wait(timeout=30)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = "coord-" + uuid4().hex
        self.origin = self.root / "origin.git"
        self.hosts = ("host-a", "host-b")
        self.tokens = {host: f"coordination-{host}" for host in self.hosts}
        self.fixture("seed", {hashlib.sha256(token.encode()).hexdigest(): host
                              for host, token in self.tokens.items()})
        self.envs = {}
        self.clones = {}
        sh(["git", "init", "-q", "--bare", "-b", "staging", str(self.origin)], self.root)
        for host in self.hosts:
            clone = self.root / host
            sh(["git", "clone", "-q", str(self.origin), str(clone)], self.root)
            sh(["git", "config", "user.name", "Test"], clone)
            sh(["git", "config", "user.email", "test@example.com"], clone)
            home = self.root / (host + "-home")
            token_dir = home / ("enact" if os.name == "nt" else ".config/enact")
            token_dir.mkdir(parents=True)
            (token_dir / "token").write_text(self.tokens[host], encoding="utf-8")
            self.clones[host] = clone
            self.envs[host] = {**os.environ, "HOME": str(home), "APPDATA": str(home),
                               "ENACT_TK": os.environ["ENACT_TEST_TK"], "ENACT_HOST": host,
                               "ENACT_SESSION": f"{host}-session", "ENACT_NO_SELF_UPDATE": "1",
                               "TEST_COORDINATION_DIR": str(self.root)}
            if host == self.hosts[0]:
                self.seed_checkout(clone)
        self.addCleanup(self.assert_no_enact_refs)

    def seed_checkout(self, clone):
        (clone / ".enact.toml").write_text(f"""[tickets]
project = "{self.project}"
url = "{self.address}"
[checks]
fast = "python3 check.py fast"
slow = "python3 check.py slow"
[push]
queue = true
""", encoding="utf-8")
        (clone / "check.py").write_text("""import os, sys
from pathlib import Path

if sys.argv[1] == "slow" and (os.environ.get("TEST_FAIL_SLOW") or Path("SLOW").exists()):
    sys.exit("simulated slow-CI failure")
""", encoding="utf-8")
        sh(["git", "add", "-A"], clone)
        sh(["git", "commit", "-qm", "Initial commit"], clone)
        sh(["git", "push", "-q", "origin", "HEAD:staging", "HEAD:master"], clone)

    def cutover(self):
        self.tk(self.hosts[0], "state", "--set-cutover", "paused")
        self.tk(self.hosts[0], "state", "--set-cutover", "after")

    def test_daily_backup_and_restore(self):
        a, b = self.hosts
        ref = "refs/heads/ticket-backups"
        self.assertEqual(self.enact(a, "ticket-backup")["reason"], "legacy ticket backend")
        self.assertEqual(sh(["git", "for-each-ref", ref], self.origin), "")
        self.cutover()
        parent, closed, changed = (item["id"] for item in self.tk(a, "batch", data={"ops": [
            {"op": "create", "fields": {"title": "Epic", "type": "epic"}},
            {"op": "create", "parent": "$0", "fields": {"title": "Closed"}},
            {"op": "create", "parent": "$0", "deps": ["$1"],
             "fields": {"title": "Ticket", "labels": ["test"]}},
        ]})["results"])
        self.tk(a, "close", closed, "--reason", "Verified")
        self.tk(a, "comment", changed, "Résumé\nSecond line")

        def on_day(host, day, command):
            driver = """from scripts import enact
import sys
enact.now = lambda: sys.argv[1] + "T00:00:01Z"
sys.exit(enact.main(sys.argv[2:]))
"""
            return self.call(host, [sys.executable, "-c", driver, day, command],
                             extra={"PYTHONPATH": str(SCRIPT.parent.parent)})

        def backup_lines():
            return subprocess.check_output(["git", "show", f"{ref}:tickets.jsonl"],
                                           cwd=self.origin, encoding="utf-8").splitlines()

        self.assertTrue(on_day(a, "2026-11-15", "ci")["ran"][-1]["passed"])
        first = sh(["git", "rev-parse", ref], self.origin)
        lines = backup_lines()
        self.assertEqual([json.loads(line)["id"] for line in lines],
                         sorted([parent, closed, changed]))
        self.tk(a, "update", changed, "--title", "Edited ticket")
        repeat = on_day(b, "2026-11-15", "ticket-backup")
        self.assertFalse(repeat["backed_up"])
        self.assertEqual(repeat["commit"], first)
        self.assertEqual(backup_lines(), lines)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda host: on_day(host, "2026-11-16", "ticket-backup"),
                                    self.hosts))
        self.assertTrue(any(result["backed_up"] for result in results))
        self.assertEqual(results[0]["commit"], results[1]["commit"])
        self.assertEqual(sh(["git", "rev-list", "--count", ref], self.origin), "2")
        updated = backup_lines()
        self.assertEqual(len(updated), len(lines))
        self.assertEqual([json.loads(after)["id"] for before, after in zip(lines, updated)
                          if before != after], [changed])

        path = self.root / "tickets.jsonl"
        path.write_text("\n".join(updated) + "\n", encoding="utf-8")
        original = self.project
        self.project = "restore-" + uuid4().hex
        self.addCleanup(setattr, self, "project", original)
        self.fixture("seed", {hashlib.sha256(self.tokens[a].encode()).hexdigest(): a})
        target = ["--project", self.project]
        result = self.tk(a, *target, "restore", str(path))
        self.assertEqual((result["imported"], result["remaining"]), (3, 0))
        self.assertEqual(self.tk(a, *target, "restore", str(path))["imported"], 0)
        restored = subprocess.check_output([os.environ["ENACT_TEST_TK"], *target, "export"],
                                           cwd=self.clones[a], env=self.envs[a], encoding="utf-8")

        def ticket_data(rows):
            return [{key: value for key, value in json.loads(row).items() if key != "version"}
                    for row in rows]

        self.assertEqual(ticket_data(restored.splitlines()), ticket_data(updated))
        self.tk(a, *target, "update", changed, "--title", "Edited after restore")
        failure = subprocess.run([os.environ["ENACT_TEST_TK"], *target, "restore", str(path)],
                                 cwd=self.clones[a], env=self.envs[a], text=True,
                                 capture_output=True, timeout=30)
        self.assertEqual(failure.returncode, 1, failure.stdout + failure.stderr)
        self.assertIn(changed, failure.stderr)
        self.assertEqual(self.tk(a, *target, "show", changed)["title"], "Edited after restore")

    def assert_no_enact_refs(self):
        for path in [self.origin, *self.clones.values()]:
            self.assertEqual(sh(["git", "for-each-ref", "refs/enact/"], path), "")
            self.assertFalse((path / ".git" / "enact" / "ownership.lock").exists())

    def fixture(self, action, data):
        request = Request(f"{self.address}/__coordination/{self.project}/{action}",
                          data=json.dumps(data).encode(), method="POST")
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def tk(self, host, *args, data=None):
        return self.call(host, [os.environ["ENACT_TEST_TK"], "--json", *args], data=data)

    def enact(self, host, *args, code=0, cwd=None):
        return self.call(host, [sys.executable, str(SCRIPT), *args], code=code, cwd=cwd)

    def call(self, host, command, *, cwd=None, code=0, extra=None, data=None):
        env = {**self.envs[host], **(extra or {})}
        result = subprocess.run(command, cwd=cwd or self.clones[host], env=env, text=True,
                                input=json.dumps(data) if data is not None else None,
                                capture_output=True, timeout=180)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def branch_change(self, host, name):
        tree = Path(self.enact(host, "worktree", "create", name)["path"])
        (tree / name).write_text(name, encoding="utf-8")
        sh(["git", "add", name], tree)
        sh(["git", "commit", "-qm", name], tree)
        return tree

    def until(self, condition):
        # This timeout catches a hung fixture. Progress is signaled by the
        # marker or lease state, independent of how fast either pusher runs.
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if result := condition():
                return result
            time.sleep(0.05)
        self.fail("the integration fixture did not reach its next state")

    def test_staging_serializes_pushes_and_replaces_an_expired_holder(self):
        self.cutover()
        a, b = self.hosts
        first = self.branch_change(a, "first")
        second = self.branch_change(b, "second")
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_bytes(b"""#!/bin/sh
if [ -n "$TEST_HOLD_BATCH" ]; then
  exec python3 -c '
import os, sys, time
from pathlib import Path

root = Path(os.environ["TEST_COORDINATION_DIR"])
(root / "checking").touch()
deadline = time.monotonic() + 60
while not (root / "release").exists():
    if time.monotonic() >= deadline:
        sys.exit("test did not release the first push")
    time.sleep(0.05)
'
fi
""")
        hook.chmod(0o755)

        def busy_lease():
            return next((lease for lease in self.tk(b, "leases")
                         if lease["name"] == "staging" and lease["waiters"]), None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            push = [sys.executable, str(SCRIPT), "push", "--no-ci"]
            one = pool.submit(self.call, a, push, cwd=first, extra={"TEST_HOLD_BATCH": "1"})
            try:
                def first_has_lease():
                    if one.done():
                        self.fail(f"first push exited before reaching the receive hook: {one.result()}")
                    return (self.root / "checking").exists()

                self.until(first_has_lease)
                two = pool.submit(self.call, b, push, cwd=second)
                lease = self.until(busy_lease)
                self.assertEqual(lease["machine"], a)
                self.assertEqual([waiter["machine"] for waiter in lease["waiters"]], [b])
            finally:
                (self.root / "release").touch()
            first_sha = one.result()["staging"]
            second_sha = two.result()["staging"]
        sh(["git", "merge-base", "--is-ancestor", first_sha, second_sha], self.origin)
        self.assertEqual(self.tk(a, "leases"), [])

        self.tk(a, "lease", "acquire", "staging", "--holder", "dead-pusher")
        self.fixture("expire", {"name": "staging"})
        third = self.branch_change(b, "third")
        self.assertTrue(self.enact(b, "push", "--no-ci", cwd=third)["pushed"])
        self.assertEqual(self.tk(a, "leases"), [])

    def test_justiciar_is_per_machine_and_fences_stale_transitions(self):
        self.cutover()
        a, b = self.hosts
        self.enact(a, "justiciar", "request", "--reason", "repair requested")
        old = self.enact(a, "justiciar", "status")["justiciar"]
        active = self.enact(a, "justiciar", "acquire", "--actor", "owner")["acquired"]
        self.assertEqual(active["session"], "owner")
        self.enact(a, "justiciar", "acquire", "--actor", "rival", code=3)
        self.enact(a, "justiciar", "release", "--actor", "rival", code=3)
        stale = self.call(a, [os.environ["ENACT_TEST_TK"], "--json", "lease", "release",
                              f"justiciar/{a}", "--holder", old["lease_holder"],
                              "--if-revision", old["revision"]], code=3)
        self.assertEqual(stale["holder"], active["lease_holder"])
        self.assertEqual(self.enact(b, "justiciar", "acquire")["acquired"]["host"], b)
        self.enact(a, "justiciar", "release", "--actor", "owner")
        self.assertIsNone(self.enact(a, "justiciar", "status")["justiciar"])
        self.assertIsNotNone(self.enact(b, "justiciar", "status")["justiciar"])

    def test_slow_ci_reuses_a_steward_per_os_and_posts_results(self):
        self.cutover()
        a, b = self.hosts
        change = self.branch_change(a, "needs-check")
        tip = self.enact(a, "push", "--no-ci", cwd=change)["staging"]
        # Run the actual slow check on both clones with controlled OS names.
        # The OS override lets the same fixture test concurrent same-OS
        # failures and independent failures across two operating systems.
        driver = """from scripts import enact
import json, sys
enact.ci_os = lambda: sys.argv[1]
print(json.dumps(enact.ci_run_once(enact.find_repo(), sys.argv[2])))
"""

        def run_ci(host, os_name, *, failing=True):
            sh(["git", "fetch", "-q", "origin"], self.clones[host])
            return self.call(host, [sys.executable, "-c", driver, os_name, tip],
                             extra={"PYTHONPATH": str(SCRIPT.parent.parent),
                                    "TEST_FAIL_SLOW": "1" if failing else ""})

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run_ci, a, "macos")
            second = pool.submit(run_ci, b, "macos")
            mac_steward = first.result()["steward"]
            self.assertEqual(second.result()["steward"], mac_steward)
        windows_steward = run_ci(b, "windows")["steward"]
        self.assertNotEqual(windows_steward, mac_steward)
        stewards = self.tk(a, "list", "--label", "role:steward")
        self.assertEqual(len(stewards), 2)
        events = self.tk(a, "events", "--kind", "ci")
        self.assertEqual({(event["data"]["os"], event["data"]["ticket"])
                          for event in events}, {("macos", mac_steward), ("windows", windows_steward)})
        self.assertEqual(len(events), 3)
        self.assertTrue(run_ci(b, "windows", failing=False)["master_advanced"])
        status = self.enact(a, "ci", "--status")["results_by_os"]
        self.assertFalse(status["macos"]["passed"])
        self.assertEqual(status["macos"]["steward"], mac_steward)
        self.assertTrue(status["windows"]["master_advanced"])
        self.assertEqual(self.enact(a, "ci", "--wait", tip, "--timeout", "0")["state"], "passed")


    def test_vizier_finishes_legacy_named_lane_through_steward_repair(self):
        vizier, worker = self.hosts
        self.assertEqual(self.enact(worker, "backend")["backend"], "beads")
        self.cutover()
        epic, first, second = (item["id"] for item in self.tk(vizier, "batch", data={"ops": [
            {"op": "create", "fields": {"title": "Run", "type": "epic"}},
            {"op": "create", "parent": "$0", "fields": {"title": "First", "labels": ["lane:work"]}},
            {"op": "create", "parent": "$0", "deps": ["$1"],
             "fields": {"title": "Second", "labels": ["lane:work"]}},
        ]})["results"])

        def next_step():
            return self.enact(vizier, "next", "--epic", epic)

        def start(step):
            self.enact(worker, "start", step["bead"], "--reservation", step["reservation"])

        def push(ticket, filename, *, remove=False):
            legacy_id = f"{self.project}-{ticket}"
            tree = Path(self.enact(worker, "worktree", "create", legacy_id)["path"])
            target = tree / filename
            if remove:
                target.unlink()
            else:
                target.touch()
            sh(["git", "add", "-A"], tree)
            sh(["git", "commit", "-qm", ticket], tree)
            result = self.enact(worker, "push", "--bead", legacy_id, "--no-ci", cwd=tree)
            self.assertTrue(result["pushed"])
            self.assertEqual(result["queue"]["batch"], 1)
            return result["staging"]

        step = next_step()
        self.assertEqual((step["action"], step["bead"]), ("spawn", first))
        start(step)
        push(first, "FIRST")
        continued = self.enact(worker, "close", first, "--reason", "delivered", "--continue")
        self.assertEqual(continued["next"]["id"], second)
        failing = push(second, "SLOW")
        self.enact(worker, "close", second, "--reason", "delivered")
        failed = self.enact(vizier, "ci")["ran"][-1]
        self.assertEqual(failed["tip"], failing)
        self.assertFalse(failed["passed"])

        step = next_step()
        self.assertEqual(step["action"], "spawn", step)
        self.assertEqual(step["bead"], failed["steward"])
        start(step)
        fixed = push(step["bead"], "SLOW", remove=True)
        self.enact(worker, "close", step["bead"], "--reason", "repaired")
        passed = self.enact(worker, "ci")["ran"][-1]
        self.assertTrue(passed["master_advanced"])
        self.assertEqual(passed["tip"], fixed)
        done = next_step()
        self.assertEqual(done["action"], "done")
        self.assertTrue(done["delivery"]["complete"])
        self.assertIn(fixed, done["delivery"]["commits"])
        self.assertEqual({entry["bead"] for entry in done["archive"]},
                         {first, second, step["bead"]})
