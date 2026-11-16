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
backend = "server"
project = "{self.project}"
url = "{self.address}"
[checks]
fast = "python3 check.py fast"
slow = "python3 check.py slow"
[push]
queue = true
""", encoding="utf-8")
        (clone / "check.py").write_text("""import os, sys

if sys.argv[1] == "slow":
    sys.exit("simulated slow-CI failure" if os.environ.get("TEST_FAIL_SLOW") else 0)
""", encoding="utf-8")
        sh(["git", "add", "-A"], clone)
        sh(["git", "commit", "-qm", "Initial commit"], clone)
        sh(["git", "push", "-q", "origin", "HEAD:staging", "HEAD:master"], clone)

    def assert_no_enact_refs(self):
        for path in [self.origin, *self.clones.values()]:
            self.assertEqual(sh(["git", "for-each-ref", "refs/enact/"], path), "")

    def fixture(self, action, data):
        request = Request(f"{self.address}/__coordination/{self.project}/{action}",
                          data=json.dumps(data).encode(), method="POST")
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def tk(self, host, *args):
        return self.call(host, [os.environ["ENACT_TEST_TK"], "--json", *args])

    def enact(self, host, *args, code=0, cwd=None):
        return self.call(host, [sys.executable, str(SCRIPT), *args], code=code, cwd=cwd)

    def call(self, host, command, *, cwd=None, code=0, extra=None):
        env = {**self.envs[host], **(extra or {})}
        result = subprocess.run(command, cwd=cwd or self.clones[host], env=env, text=True,
                                capture_output=True, timeout=90)
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
