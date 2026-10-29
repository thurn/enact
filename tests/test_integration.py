"""Slow black-box tests: enact.py against real git, bd and Dolt.

Each test builds a bare "origin" plus clones that stand in for separate
machines, then drives the CLI exactly as agents do.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "enact.py"
HAVE_TOOLS = all(shutil.which(t) for t in ("git", "bd", "dolt"))

ENACT_TOML = """\
master_branch = "master"
[checks]
fast = "python3 check.py fast"
slow = "python3 check.py slow"
"""

CHECK_PY = """\
import pathlib, sys
if pathlib.Path(sys.argv[1].upper()).exists():
    print(sys.argv[1] + " check failed")
    sys.exit(1)
"""


def sh(cmd, cwd, check=True, env=None):
    proc = subprocess.run(
        cmd, cwd=cwd, text=True, capture_output=True, env={**os.environ, **(env or {})}
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"{cmd} failed in {cwd}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip()


class Machine:
    """One clone of the project, with its own host and session identity."""

    def __init__(self, path: Path, host: str, session: str):
        self.path, self.host, self.session = path, host, session

    def env(self, session=None):
        return {
            "ENACT_HOST": self.host,
            "ENACT_PROVIDER": "claude",
            "ENACT_SESSION": session or self.session,
            "CLAUDE_CODE_SESSION_ID": "",
            "CODEX_THREAD_ID": "",
            "BD_NON_INTERACTIVE": "1",
        }

    def enact(self, *args, code=0, cwd=None, session=None):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=cwd or self.path,
            text=True,
            capture_output=True,
            env={**os.environ, **self.env(session)},
        )
        if proc.returncode != code:
            raise AssertionError(
                f"enact {' '.join(args)} exited {proc.returncode}, wanted {code}:\n"
                f"{proc.stdout}\n{proc.stderr}"
            )
        return json.loads(proc.stdout)

    def bd(self, *args, actor=None):
        cmd = ["bd"] + (["--actor", actor] if actor else []) + list(args) + ["--json"]
        out = sh(cmd, self.path, env=self.env())
        return json.loads(out) if out else None

    def create(self, title, **kw):
        args = ["create", "--title", title]
        for key, value in kw.items():
            args += [f"--{key.replace('_', '-')}", str(value)]
        return self.bd(*args)["id"]

    def show(self, bead):
        data = self.bd("show", bead)
        return data[0] if isinstance(data, list) else data

    def git(self, *args, cwd=None):
        return sh(["git", *args], cwd or self.path)

    def commit_file(self, name, text, cwd=None, message=None):
        where = Path(cwd or self.path)
        (where / name).write_text(text)
        self.git("add", name, cwd=where)
        self.git("commit", "-q", "-m", message or f"add {name}", cwd=where)


@unittest.skipUnless(HAVE_TOOLS, "needs git, bd and dolt")
class EnactTest(unittest.TestCase):
    # Building origin, machine a and machine b (bd init, Dolt bootstrap) costs
    # most of a test's runtime, so it happens once per class. Each test then
    # restores that snapshot to the same path, keeping absolute paths in git
    # and Dolt config valid.

    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="enact-test-")).resolve()
        cls.tmp = cls.base / "work"
        cls.snapshot = cls.base / "snapshot"
        cls.tmp.mkdir()
        test = cls("run")
        test.origin = cls.tmp / "origin.git"
        sh(["git", "init", "-q", "--bare", "-b", "master", str(test.origin)], cls.tmp)
        test.clone("a", "host-a", "sess-a", first=True)
        test.clone("b", "host-b", "sess-b")
        shutil.copytree(cls.tmp, cls.snapshot, symlinks=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.copytree(self.snapshot, self.tmp, symlinks=True)
        self.origin = self.tmp / "origin.git"
        self.a = Machine(self.tmp / "a", "host-a", "sess-a")
        self.b = Machine(self.tmp / "b", "host-b", "sess-b")

    def tearDown(self):
        for proc_dir in self.tmp.glob("*"):
            lock = proc_dir / ".git" / "enact" / "ci.lock"
            deadline = time.time() + 30
            while lock.exists() and time.time() < deadline:
                time.sleep(0.5)

    def clone(self, name, host, session, first=False):
        path = self.tmp / name
        if first:
            sh(["git", "init", "-q", "-b", "master", str(path)], self.tmp)
            machine = Machine(path, host, session)
            self.configure_git(path)
            (path / ".enact.toml").write_text(ENACT_TOML)
            (path / "check.py").write_text(CHECK_PY)
            machine.git("add", ".")
            machine.git("commit", "-q", "-m", "init")
            machine.git("remote", "add", "origin", str(self.origin))
            machine.git("push", "-q", "origin", "master")
            machine.enact("setup", "--prefix", "tp")  # bd init commits .beads/
            if machine.git("status", "--porcelain", ".gitignore"):
                machine.git("add", ".gitignore")
                machine.git("commit", "-q", "-m", "ignore worktrees")
            machine.git("push", "-q", "origin", "HEAD:master", "HEAD:staging")
            return machine
        sh(["git", "clone", "-q", str(self.origin), str(path)], self.tmp)
        self.configure_git(path)
        machine = Machine(path, host, session)
        assert machine.enact("setup")["beads"] == "bootstrapped from remote"
        return machine

    def configure_git(self, path):
        sh(["git", "config", "user.email", "t@example.com"], path)
        sh(["git", "config", "user.name", "Test"], path)

    def remote_sha(self, branch):
        return sh(["git", "rev-parse", branch], self.origin)

    def wait_for_ci(self, machine, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not machine.enact("ci", "--status")["running"]:
                return
            time.sleep(0.5)
        self.fail("slow CI runner did not finish")

    # -- git flow ---------------------------------------------------------

    def test_setup_creates_staging_and_beads(self):
        self.assertEqual(self.remote_sha("staging"), self.remote_sha("master"))
        self.assertTrue((self.a.path / ".beads").is_dir())
        self.assertIn("/.worktrees/", (self.a.path / ".gitignore").read_text())
        # bd's audit log is ignored, not tracked, so it never dirties master.
        self.assertEqual(self.a.git("ls-files", ".beads/interactions.jsonl"), "")
        self.assertIn("/.beads/interactions.jsonl", (self.a.path / ".gitignore").read_text())

    def test_worktrees_live_ignored_inside_the_checkout(self):
        path = Path(self.a.enact("worktree", "create", "inside")["path"])
        self.assertEqual(path.parent, self.a.path / ".worktrees")
        self.assertEqual(self.a.git("status", "--porcelain"), "")
        # A clone whose .gitignore lacks the root falls back to info/exclude.
        gitignore = self.a.path / ".gitignore"
        gitignore.write_text(gitignore.read_text().replace("/.worktrees/\n", ""))
        self.a.git("commit", "-qam", "drop ignore")
        self.a.enact("worktree", "create", "inside2")
        self.assertIn("/.worktrees/", (self.a.path / ".git" / "info" / "exclude").read_text())
        self.assertEqual(self.a.git("status", "--porcelain"), "")

    def test_push_from_worktree_records_commits_and_cleans_up(self):
        bead = self.a.create("feature")
        wt = self.a.enact("worktree", "create", bead)
        path = Path(wt["path"])
        self.configure_git(path)
        self.a.commit_file("feature.txt", "x", cwd=path)
        result = self.a.enact("push", "--bead", bead, "--no-ci", cwd=path)
        self.assertTrue(result["pushed"])
        self.assertEqual(self.remote_sha("staging"), result["staging"])
        self.assertFalse(path.exists())
        self.assertEqual(
            self.a.show(bead)["metadata"]["enact_commits"], " ".join(result["commits"])
        )

    def test_push_rebases_over_peer_and_merges_diverged_master(self):
        b = self.b
        b.git("checkout", "-q", "-b", "work", "origin/staging")
        b.commit_file("peer.txt", "peer")
        b.enact("push", "--no-ci")
        # A human pushes straight to master, bypassing staging.
        self.a.commit_file("hotfix.txt", "hot")
        self.a.git("push", "-q", "origin", "HEAD:master")
        wt = Path(self.a.enact("worktree", "create", "w1")["path"])
        self.configure_git(wt)
        self.a.commit_file("mine.txt", "mine", cwd=wt)
        self.a.enact("push", "--no-ci", cwd=wt)
        tree = sh(["git", "ls-tree", "--name-only", "staging"], self.origin).split()
        self.assertTrue({"peer.txt", "hotfix.txt", "mine.txt"} <= set(tree))
        sh(["git", "merge-base", "--is-ancestor", "master", "staging"], self.origin)

    def test_push_refuses_when_fast_checks_fail(self):
        wt = Path(self.a.enact("worktree", "create", "w2")["path"])
        self.configure_git(wt)
        self.a.commit_file("FAST", "boom", cwd=wt)
        before = self.remote_sha("staging")
        self.a.enact("push", "--no-ci", cwd=wt, code=2)
        self.assertEqual(self.remote_sha("staging"), before)

    def test_worktree_create_hook_replaces_git(self):
        hook = (
            "python3 -c \"import os,subprocess; subprocess.check_call(['git','worktree','add',"
            "'-q','-b',os.environ['ENACT_BRANCH'],os.environ['ENACT_WORKTREE_PATH'],"
            "os.environ['ENACT_BASE_SHA']]); open(os.path.join(os.environ['ENACT_WORKTREE_PATH'],"
            "'.hooked'),'w').close()\""
        )
        toml = self.a.path / ".enact.toml"
        toml.write_text(ENACT_TOML + f"[worktree]\ncreate = '''{hook}'''\n")
        path = Path(self.a.enact("worktree", "create", "hooked")["path"])
        self.assertTrue((path / ".hooked").exists())

    # -- slow CI ----------------------------------------------------------

    def test_slow_ci_pass_advances_master(self):
        wt = Path(self.a.enact("worktree", "create", "ci1")["path"])
        self.configure_git(wt)
        self.a.commit_file("ok.txt", "ok", cwd=wt)
        result = self.a.enact("push", cwd=wt)
        tip = result["staging"]
        self.assertEqual(self.a.git("rev-parse", "staging"), tip)
        self.wait_for_ci(self.a)
        self.assertEqual(self.remote_sha("master"), tip)
        # The checked-out local master follows, files included.
        self.assertEqual(self.a.git("rev-parse", "master"), tip)
        self.assertTrue((self.a.path / "ok.txt").exists())

    def test_local_branches_left_alone_when_dirty_or_diverged(self):
        b = self.b
        (self.a.path / "check.py").write_text(CHECK_PY + "# local edit\n")
        before = self.a.git("rev-parse", "master")
        b.git("checkout", "-q", "-b", "work", "origin/staging")
        b.commit_file("peer.txt", "peer")
        b.enact("push", "--no-ci")
        b.git("push", "-q", "origin", "origin/staging:refs/heads/master")
        local = self.a.enact("status")["local_branches"]
        self.assertEqual(local["staging"], "fast-forwarded")
        self.assertIn("local changes", local["master"])
        self.assertEqual(self.a.git("rev-parse", "master"), before)
        self.a.git("checkout", "-q", "check.py")
        self.a.commit_file("mine.txt", "mine")
        self.assertEqual(self.a.enact("status")["local_branches"]["master"], "diverged")

    def test_slow_ci_failure_files_one_steward_bead(self):
        for name in ("SLOW", "more.txt"):
            wt = Path(self.a.enact("worktree", "create", f"ci-{name.lower()}")["path"])
            self.configure_git(wt)
            self.a.commit_file(name, "x", cwd=wt)
            self.a.enact("push", cwd=wt)
            self.wait_for_ci(self.a)
        stewards = self.a.bd("list", "--label", "role:steward")
        self.assertEqual(len(stewards), 1)
        self.assertEqual(stewards[0]["priority"], 0)
        self.assertGreaterEqual(len(self.a.bd("comments", stewards[0]["id"]) or []), 1)
        self.assertNotEqual(self.remote_sha("master"), self.remote_sha("staging"))

    # -- beads coordination -----------------------------------------------

    def test_claim_race_has_one_winner(self):
        bead = self.a.create("contested")
        self.a.enact("sync")
        b = self.b
        b.enact("sync")
        self.a.enact("claim", bead)
        b.enact("claim", bead, code=3)
        self.assertEqual(b.show(bead)["assignee"], "sess-a")

    def test_sync_conflict_remote_wins_and_reports(self):
        bead = self.a.create("both edit")
        self.a.enact("sync")
        b = self.b
        b.enact("sync")
        self.a.bd("update", bead, "--title", "a title")
        self.a.enact("sync")
        b.bd("update", bead, "--title", "b title")
        result = b.enact("sync")
        self.assertEqual([d["id"] for d in result["dropped"]], [bead])
        self.assertEqual(b.show(bead)["title"], "a title")

    def test_vizier_schedules_archives_and_finishes(self):
        epic = self.a.create("epic", type="epic")
        first = self.a.create("first", parent=epic, priority=1)
        second = self.a.create("second", parent=epic, labels="role:warden")
        self.a.bd("dep", "add", second, first)
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual((step["action"], step["bead"], step["role"]), ("spawn", first, "executor"))
        self.assertIn("executor.md", step["prompt"])
        self.a.enact("meta", first, "enact_session=local_1")
        self.a.enact("start", first, session="exec-1")
        self.a.enact("close", first, "--reason", "done", session="exec-1")
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual(step["archive"], [{"bead": first, "session": "local_1"}])
        self.assertEqual((step["action"], step["bead"], step["role"]), ("spawn", second, "warden"))
        self.a.enact("start", second, session="exec-2")
        self.a.enact("close", second, "--reason", "done", session="exec-2")
        self.assertEqual(self.a.show(epic)["status"], "closed")
        self.assertEqual(self.a.enact("next", "--epic", epic)["action"], "done")


if __name__ == "__main__":
    unittest.main()
