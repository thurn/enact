"""Slow black-box tests: enact.py against real git, bd and Dolt.

Each test builds a bare "origin" plus clones that stand in for separate
machines, then drives the CLI exactly as agents do.
"""

import json
import os
import shlex
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


@unittest.skipUnless(shutil.which("git"), "needs git")
class CiLifetimeTest(unittest.TestCase):
    """Real CLI/check processes; barriers make coordinator crashes repeatable."""

    def wait_for(self, predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("CI process did not reach the expected state")

    def test_coordinator_crash_across_check_handoff(self):
        for phase in ("before_spawn", "spawned", "published", "authorized",
                      "running", "cleanup", "recording"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                repo, origin = root / "repo", root / "origin.git"
                sh(["git", "init", "-q", "--bare", "-b", "master", str(origin)], root)
                sh(["git", "init", "-q", "-b", "master", str(repo)], root)
                sh(["git", "config", "user.email", "test@example.com"], repo)
                sh(["git", "config", "user.name", "Test"], repo)
                command = [sys.executable, "check.py"]
                slow = (subprocess.list2cmdline(command) if os.name == "nt"
                        else shlex.join(command))
                (repo / ".enact.toml").write_text(f"[checks]\nslow = {json.dumps(slow)}\n")
                (repo / "check.py").write_text(
                    "import json, os, time\nfrom pathlib import Path\n"
                    f"root = Path({str(root)!r})\n"
                    "sentinel = Path.cwd() / 'check-sentinel'\n"
                    "sentinel.write_text(str(os.getpid()))\n"
                    "with (root / 'started').open('a') as handle:\n"
                    "    handle.write(json.dumps([os.getpid(), str(Path.cwd())]) + '\\n')\n"
                    "(root / 'started-ready').touch()\n"
                    "while not (root / 'finish-check').exists(): time.sleep(0.02)\n"
                    "assert sentinel.read_text() == str(os.getpid())\n"
                )
                sh(["git", "add", "."], repo)
                sh(["git", "commit", "-qm", "initial"], repo)
                sh(["git", "remote", "add", "origin", str(origin)], repo)
                sh(["git", "push", "-q", "origin", "HEAD:master"], repo)
                sh(["git", "commit", "-q", "--allow-empty", "-m", "candidate"], repo)
                sh(["git", "push", "-q", "origin", "HEAD:staging"], repo)
                tip = sh(["git", "rev-parse", "HEAD"], repo)
                # Invoke the real CLI with pauses around actual operations.
                # No lock, subprocess, worktree or result behavior is faked.
                wrapper = root / "cli.py"
                wrapper.write_text(
                    "import importlib.util, os, sys, time\nfrom pathlib import Path\n"
                    f"spec = importlib.util.spec_from_file_location('enact', {str(SCRIPT)!r})\n"
                    "enact = importlib.util.module_from_spec(spec)\n"
                    "sys.modules['enact'] = enact\nspec.loader.exec_module(enact)\n"
                    "enact.__file__ = __file__\n"
                    f"root, phase = Path({str(root)!r}), {phase!r}\n"
                    "def pause():\n"
                    "    (root / 'paused').write_text(str(os.getpid()))\n"
                    "    while not (root / 'resume').exists(): time.sleep(0.02)\n"
                    "original_spawn, original_write = enact.subprocess.Popen, enact.write_json\n"
                    "def spawn(command, *args, **kwargs):\n"
                    "    target = '--check-worker' in command\n"
                    "    if target and phase == 'before_spawn': pause()\n"
                    "    child = original_spawn(command, *args, **kwargs)\n"
                    "    if target:\n"
                    "        if phase == 'spawned': pause()\n"
                    "    return child\n"
                    "def write(path, value):\n"
                    "    if path.name == 'ci-result.json' and phase == 'recording': pause()\n"
                    "    original_write(path, value)\n"
                    "    if path.name == 'ci.lock' and value != os.getpid() and phase == 'published': pause()\n"
                    "enact.subprocess.Popen, enact.write_json = spawn, write\n"
                    "if '--check-worker' in sys.argv and phase == 'cleanup':\n"
                    "    original_remove = enact.remove_worktree\n"
                    "    def remove(*args):\n"
                    "        pause()\n"
                    "        return original_remove(*args)\n"
                    "    enact.remove_worktree = remove\n"
                    "if '--check-worker' in sys.argv and phase == 'authorized':\n"
                    "    original_check = enact.ci_run_once\n"
                    "    def check(*args):\n"
                    "        pause()\n"
                    "        return original_check(*args)\n"
                    "    enact.ci_run_once = check\n"
                    "sys.exit(enact.main())\n"
                )
                state = repo / ".git" / "enact"
                if phase in ("cleanup", "recording"):
                    (root / "finish-check").touch()

                def cli(*args):
                    result = subprocess.run(
                        [sys.executable, str(SCRIPT), "ci", *args], cwd=repo,
                        text=True, capture_output=True, timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return json.loads(result.stdout)

                with (root / "coordinator.log").open("w") as output:
                    entrypoint = SCRIPT if phase == "running" else wrapper
                    coordinator = subprocess.Popen(
                        [sys.executable, str(entrypoint), "ci"], cwd=repo,
                        stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                    )
                    try:
                        barrier = root / ("started-ready" if phase == "running" else "paused")
                        self.wait_for(barrier.exists)
                        self.assertIn("another runner", cli()["reason"])
                        coordinator.kill()
                        coordinator.wait(timeout=10)
                        if phase in ("authorized", "running", "cleanup", "recording"):
                            self.assertTrue(cli("--status")["running"])
                            self.assertIn("another runner", cli()["reason"])
                            if phase == "authorized":
                                (root / "resume").touch()
                            self.wait_for((root / "started-ready").exists)
                            pid, cwd = json.loads((root / "started").read_text().splitlines()[0])
                            sentinel = Path(cwd) / "check-sentinel"
                            self.assertIn("another runner", cli()["reason"])
                            if phase == "recording":
                                self.assertFalse(Path(cwd).exists())
                            else:
                                self.assertEqual(sentinel.read_text(), str(pid))
                            (root / "resume").touch()
                            (root / "finish-check").touch()
                            self.wait_for(lambda: (state / "ci-last").exists())
                            self.wait_for(lambda: not cli("--status")["running"])
                            self.assertFalse(Path(cwd).exists())
                        else:
                            # EOF cancels a child that never got permission.
                            self.wait_for(lambda: not cli("--status")["running"])
                            self.assertFalse((root / "started").exists())
                            (root / "finish-check").touch()
                        self.assertNotIn("reason", cli())
                        self.assertEqual(len((root / "started").read_text().splitlines()), 1)
                        self.assertEqual((state / "ci-last").read_text(), tip)
                        self.assertEqual(sh(["git", "rev-parse", "master"], origin), tip)
                    finally:
                        (root / "resume").touch()
                        (root / "finish-check").touch()
                        if coordinator.poll() is None:
                            coordinator.kill()
                        coordinator.wait(timeout=10)
                        # Let any authorized orphan finish before removing its repository.
                        self.wait_for(lambda: not cli("--status")["running"])


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
            status = machine.enact("ci", "--status")
            if not status["running"] and status["last_tested"] == self.remote_sha("staging"):
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

    def test_remove_preserves_unpublished_branch_without_checkout(self):
        path = Path(self.a.enact("worktree", "create", "orphan")["path"])
        self.a.commit_file("unfinished", "saved commit", cwd=path)
        head = self.a.git("rev-parse", "HEAD", cwd=path)
        self.a.git("worktree", "remove", str(path))
        self.a.enact("worktree", "remove", "orphan", code=2)
        self.assertEqual(self.a.git("rev-parse", "enact/orphan"), head)

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

    def test_repeated_pushes_preserve_ordered_commit_history(self):
        bead = self.a.create("two deliveries")
        expected = []
        for name in ("first", "second"):
            path = Path(self.a.enact("worktree", "create", bead)["path"])
            self.a.commit_file(name, name, cwd=path)
            result = self.a.enact("push", "--bead", bead, "--no-ci", cwd=path)
            expected += result["commits"]
        self.assertEqual(self.a.show(bead)["metadata"]["enact_commits"].split(), expected)
        path = Path(self.a.enact("worktree", "create", bead)["path"])
        self.assertFalse(self.a.enact("push", "--bead", bead, "--no-ci", cwd=path)["pushed"])
        self.assertEqual(self.a.show(bead)["metadata"]["enact_commits"].split(), expected)

    def test_push_retry_recovers_after_metadata_sync_failure(self):
        bead = self.a.create("recover delivery")
        path = Path(self.a.enact("worktree", "create", bead)["path"])
        self.a.commit_file("delivered", "yes", cwd=path)
        head = self.a.git("rev-parse", "HEAD", cwd=path)
        # Inject only the failed Beads sync; git, Beads writes, and recovery
        # are real. This represents a network failure after the git push.
        harness = """
import argparse, runpy, sys
module = runpy.run_path(sys.argv[1], run_name='fault_test')
def fail_sync(repo):
    raise RuntimeError('injected sync outage')
original_finish = module['finish_delivery']
def finish(repo, receipt, data):
    module['cmd_push'].__globals__['sync'] = fail_sync
    return original_finish(repo, receipt, data)
module['cmd_push'].__globals__['finish_delivery'] = finish
module['cmd_push'](argparse.Namespace(bead=sys.argv[2], skip_checks=False,
                                     no_ci=False, keep=False))
"""
        proc = subprocess.run([sys.executable, "-c", harness, str(SCRIPT), bead],
                              cwd=path, text=True, capture_output=True,
                              env={**os.environ, **self.a.env()})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("injected sync outage", proc.stderr)
        self.assertEqual(self.remote_sha("staging"), head)
        self.assertTrue(path.exists())
        # Also exercise losing the local metadata in a remote-wins merge.
        self.a.bd("update", bead, "--unset-metadata", "enact_commits")
        result = self.a.enact("push", "--bead", bead, cwd=path)
        self.assertFalse(result["pushed"])
        self.assertEqual(result["recovered"][0]["commits"], [head])
        self.assertFalse(path.exists())
        self.assertEqual(self.a.show(bead)["metadata"]["enact_commits"], head)
        self.assertEqual(list((self.a.path / ".git/enact/deliveries").glob("*.json")), [])
        self.wait_for_ci(self.a)
        self.assertEqual(self.remote_sha("master"), head)

    def test_self_delivery_keeps_ci_entry_point_after_removing_worktree(self):
        path = Path(self.a.enact("worktree", "create", "self")["path"])
        self.a.commit_file("enact.py", SCRIPT.read_text(), cwd=path)
        result = json.loads(sh([sys.executable, str(path / "enact.py"), "push"],
                               path, env=self.a.env()))
        self.assertFalse(path.exists())
        self.wait_for_ci(self.a)
        self.assertEqual(self.remote_sha("master"), result["staging"])

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

    def ci_without_promotion(self):
        # Keep the simulated unavailable remote in both the coordinator and
        # its keeper, while exercising the real CLI and checks in each.
        harness = self.tmp / "reject-promotion.py"
        harness.write_text(
            "import runpy, sys\n"
            f"module = runpy.run_path({str(SCRIPT)!r}, run_name='fault_test')\n"
            "namespace = module['cmd_ci'].__globals__\n"
            "namespace['ci_pass'] = lambda repo, tip: False\n"
            "namespace['__file__'] = __file__\n"
            "sys.exit(module['main']())\n"
        )
        return json.loads(sh([sys.executable, str(harness), "ci"], self.a.path))

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

    def test_passing_ci_retries_a_failed_promotion(self):
        path = Path(self.a.enact("worktree", "create", "promotion")["path"])
        self.a.commit_file("good", "yes", cwd=path)
        pushed = self.a.enact("push", "--no-ci", cwd=path)
        result = self.ci_without_promotion()
        self.assertTrue(result["ran"][0]["passed"])
        self.assertFalse(result["ran"][0]["master_advanced"])
        self.assertNotEqual(self.remote_sha("master"), pushed["staging"])
        # Older runners stored only the result; upgrade that state too.
        (self.a.path / ".git/enact/ci-promotion.json").unlink()
        result = self.a.enact("ci")
        self.assertTrue(result["ran"][0]["master_advanced"])
        self.assertEqual(self.remote_sha("master"), pushed["staging"])

    def test_failed_promotion_survives_newer_failed_checks(self):
        epic = self.a.create("green delivery", type="epic")
        bead = self.a.create("last task", parent=epic)
        path = Path(self.a.enact("worktree", "create", bead)["path"])
        self.a.commit_file("good", "yes", cwd=path)
        pushed = self.a.enact("push", "--bead", bead, "--no-ci", cwd=path)
        self.a.enact("close", bead, "--reason", "delivered")
        self.ci_without_promotion()
        path = Path(self.a.enact("worktree", "create", "unrelated")["path"])
        self.a.commit_file("SLOW", "unrelated failure", cwd=path)
        self.a.enact("push", "--no-ci", cwd=path)
        # Promotion is still unavailable when the newer check fails. Its
        # failure must not erase the older passing candidate or blame its run.
        self.ci_without_promotion()
        self.assertNotEqual(self.remote_sha("master"), pushed["staging"])
        self.a.enact("ci")
        self.assertEqual(self.remote_sha("master"), pushed["staging"])
        self.assertNotEqual(self.remote_sha("master"), self.remote_sha("staging"))
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual(step["action"], "done")

    def test_recovered_delivery_joins_an_existing_shared_repair(self):
        epic = self.a.create("late delivery metadata", type="epic")
        bead = self.a.create("last task", parent=epic)
        peer = self.a.create("peer run", type="epic")
        before = self.remote_sha("master")
        path = Path(self.a.enact("worktree", "create", bead)["path"])
        self.a.commit_file("good", "yes", cwd=path)
        pushed = self.a.enact("push", "--bead", bead, "--no-ci", cwd=path)
        # CI may file its bead before a delivery receipt's metadata sync
        # recovers. Existing explicit links are not necessarily exhaustive.
        repair = self.a.create("shared repair", labels="role:steward",
                               metadata=json.dumps({"enact_runs": peer,
                                   "enact_ci_range": before + ".." + pushed["staging"]}))
        self.a.enact("close", bead, "--reason", "delivered")
        self.a.enact("ci")  # another machine's passing check can promote
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual((step["action"], step.get("bead")), ("spawn", repair))
        self.assertIn(repair, step["delivery"]["stewards"])

    def test_legacy_ci_status_rechecks_an_unpromoted_tip(self):
        path = Path(self.a.enact("worktree", "create", "legacy-ci")["path"])
        self.a.commit_file("good", "yes", cwd=path)
        pushed = self.a.enact("push", "--no-ci", cwd=path)
        (self.a.path / ".git/enact/ci-last").write_text(pushed["staging"])
        self.assertIsNone(self.a.enact("ci", "--status")["result"])
        result = self.a.enact("ci")
        self.assertTrue(result["ran"][0]["passed"])
        self.assertEqual(self.remote_sha("master"), pushed["staging"])

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

    def start_worker(self, machine, bead, session):
        token = machine.show(bead)["metadata"]["enact_reservation"]
        return machine.enact("start", bead, "--reservation", token, session=session)

    def test_repeated_reservation_rejects_delayed_vizier_actions(self):
        epic = self.a.create("repeated reservation", type="epic")
        bead = self.a.create("spawn retry", parent=epic)
        first = self.a.enact("next", "--epic", epic)["reservation"]
        self.a.enact("release", bead, "--reservation", first, "--reason", "spawn failed")
        second = self.a.enact("next", "--epic", epic)["reservation"]
        self.assertNotEqual(first, second)
        for token in (first, None):
            option = ["--reservation", token] if token else []
            self.a.enact("meta", bead, "enact_session=late-child", *option, code=3)
            self.a.enact("release", bead, "--reason", "late failure", *option, code=3)
        self.a.enact("meta", bead, "enact_session=current", "--reservation", second)
        self.start_worker(self.a, bead, "current")
        self.a.enact("meta", bead, "enact_spawn_project=stale", "--reservation", first, code=3)
        self.assertEqual(self.a.show(bead)["assignee"], "current")

    def test_local_recovery_restores_surviving_branch(self):
        epic = self.a.create("lost checkout", type="epic")
        bead = self.a.create("saved branch", parent=epic)
        self.a.enact("next", "--epic", epic)
        self.start_worker(self.a, bead, "old")
        path = Path(self.a.enact("worktree", "create", bead, session="old")["path"])
        self.a.commit_file("saved", "unpublished work", cwd=path)
        head = self.a.git("rev-parse", "HEAD", cwd=path)
        self.a.enact("release", bead, "--reason", "recovery", session="old")
        # Simulate loss of only the directory, leaving git's registration too.
        self.a.git("worktree", "lock", str(path))
        shutil.rmtree(path)
        self.a.enact("next", "--epic", epic)
        self.start_worker(self.a, bead, "new")
        self.a.enact("worktree", "create", bead, session="new", code=1)
        self.assertEqual(self.a.git("rev-parse", f"enact/{bead}"), head)
        self.a.git("worktree", "unlock", str(path))
        recovered = self.a.enact("worktree", "create", bead, session="new")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(self.a.git("rev-parse", "HEAD", cwd=path), head)
        self.assertEqual((path / "saved").read_text(), "unpublished work")
        self.a.enact("push", "--no-ci", cwd=path, session="new")
        self.assertIn(head, self.a.show(bead)["metadata"]["enact_commits"].split())

    def test_same_host_viziers_race_without_overwriting_reservations(self):
        epic = self.a.create("race", type="epic")
        bead = self.a.create("one assignment", parent=epic)
        self.a.bd("update", bead, "--set-metadata", "enact_provider=claude")
        self.a.enact("sync")
        processes = [subprocess.Popen(
            [sys.executable, str(SCRIPT), "next", "--epic", epic],
            cwd=self.a.path, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, **self.a.env(session)}) for session in ("viz-1", "viz-2")]
        results = []
        for process in processes:
            out, err = process.communicate(timeout=180)
            self.assertEqual(process.returncode, 0, (out, err))
            results.append(json.loads(out))
        self.assertEqual(sorted(r["action"] for r in results), ["spawn", "wait"])
        saved = self.a.show(bead)
        winner = next(r["vizier"] for r in results if r["action"] == "spawn")
        self.assertEqual(saved["metadata"]["enact_vizier"], winner)
        self.assertIn(saved["metadata"]["enact_reservation"],
                      next(r["prompt"] for r in results if r["action"] == "spawn"))
        # Targeting may change independently; it is not the creator route.
        self.a.bd("update", bead, "--set-metadata", "enact_provider=codex")
        row = self.a.enact("status")["working"][0]
        self.assertEqual(row["routing"], {"host": "host-a", "provider": "claude",
                                         "app": "claude", "project": str(self.a.path)})
        loser = "viz-2" if winner == "viz-1" else "viz-1"
        self.a.enact("meta", bead, "enact_session=wrong", session=loser, code=3)
        self.a.enact("start", bead, "--reservation", "wrong", session="worker", code=3)
        token = saved["metadata"]["enact_reservation"]
        self.a.enact("meta", bead, "enact_session=worker", "--reservation", token,
                     session=winner)
        self.a.enact("start", bead, "--reservation", token, session="wrong", code=3)
        self.start_worker(self.a, bead, "worker")
        self.a.enact("meta", bead, "enact_session=late-child", "--reservation", token,
                     session=winner, code=3)

    def test_local_recovery_fences_stale_workers_and_preserves_all_work(self):
        epic = self.a.create("local recovery", type="epic")
        bead = self.a.create("unfinished", parent=epic)
        self.a.enact("next", "--epic", epic)
        old_token = self.a.show(bead)["metadata"]["enact_reservation"]
        self.start_worker(self.a, bead, "old")
        path = Path(self.a.enact("worktree", "create", bead, session="old")["path"])
        self.a.commit_file("committed", "original", cwd=path)
        head = self.a.git("rev-parse", "HEAD", cwd=path)
        (path / "committed").write_text("unfinished edit")
        (path / "untracked").write_text("unfinished new file")
        self.a.enact("worktree", "remove", bead, session="old", code=2)
        # Lookup failure, a stale observation, and a different provider cannot
        # establish inactivity. None may discard this worker's assignment.
        base = ["release", bead, "--reason", "recovery", "--expected-owner", "old",
                "--expected-session", "old"]
        self.a.enact(*base, session="marshal", code=3)
        self.a.enact(*base, "--inactive", "stopped", session="marshal", code=3)
        self.a.enact(*base, "--inactive", "stopped", "--evidence", "read confirmed idle",
                     "--expected-owner", "stale", session="marshal", code=3)
        foreign = subprocess.run([sys.executable, str(SCRIPT), *base, "--inactive", "stopped",
                                  "--evidence", "wrong provider"], cwd=self.a.path,
                                 capture_output=True, text=True,
                                 env={**os.environ, **self.a.env("marshal"),
                                      "ENACT_PROVIDER": "codex"})
        self.assertEqual(foreign.returncode, 3, foreign.stdout)
        result = self.a.enact(*base, "--inactive", "stopped", "--evidence", "read confirmed idle",
                              session="marshal")
        self.assertEqual(result["recovery"]["head"], head)
        self.assertEqual(result["recovery"]["path"], str(path))
        self.a.enact("start", bead, "--reservation", old_token, session="old", code=3)
        self.a.enact("next", "--epic", epic, session="replacement-vizier")
        self.a.enact("start", bead, "--reservation", old_token, session="old", code=3)
        self.start_worker(self.a, bead, "new")
        for args in (("close", bead, "--reason", "stale"),
                     ("meta", bead, "enact_commits=stale"),
                     ("release", bead, "--reason", "stale"),
                     ("worktree", "remove", bead)):
            self.a.enact(*args, session="old", code=3)
        self.a.enact("push", "--no-ci", cwd=path, session="old", code=3)
        self.a.enact("push", "--bead", bead, "--no-ci", cwd=path, session="old", code=3)
        recovered = self.a.enact("worktree", "create", bead, session="new")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["path"], str(path))
        self.assertEqual(self.a.git("rev-parse", "HEAD", cwd=path), head)
        self.assertEqual((path / "committed").read_text(), "unfinished edit")
        self.assertEqual((path / "untracked").read_text(), "unfinished new file")
        self.a.git("add", ".", cwd=path)
        self.a.git("commit", "-qm", "finish recovered work", cwd=path)
        # Bead inference protects --bead omission and records the delivery.
        pushed = self.a.enact("push", "--no-ci", cwd=path, session="new")
        self.assertTrue(pushed["pushed"])
        self.assertFalse(path.exists())
        self.a.enact("close", bead, "--reason", "recovered", session="new")
        self.assertIn(head, self.a.show(bead)["metadata"]["enact_commits"].split())

    def test_remote_replacement_preserves_original_recovery_location(self):
        epic = self.a.create("remote recovery", type="epic")
        bead = self.a.create("unfinished remote", parent=epic)
        self.a.enact("next", "--epic", epic)
        self.start_worker(self.a, bead, "old")
        original = Path(self.a.enact("worktree", "create", bead, session="old")["path"])
        self.a.commit_file("unpublished", "local commit", cwd=original)
        head = self.a.git("rev-parse", "HEAD", cwd=original)
        (original / "untracked").write_text("local edit")
        self.a.enact("release", bead, "--reason", "relocating", session="old")
        self.b.enact("next", "--epic", epic)
        self.start_worker(self.b, bead, "remote")
        replacement = self.b.enact("worktree", "create", bead, session="remote")
        fresh = Path(replacement["path"])
        self.assertFalse(replacement["recovered"])
        self.assertEqual(self.b.git("rev-parse", "HEAD", cwd=fresh), self.remote_sha("staging"))
        self.assertEqual((original / "untracked").read_text(), "local edit")
        self.assertEqual(self.a.git("rev-parse", "HEAD", cwd=original), head)
        records = self.b.show(bead)["metadata"]["enact_recovery"]
        record = (json.loads(records) if isinstance(records, str) else records)[0]
        self.assertEqual((record["host"], record["path"], record["head"]),
                         ("host-a", str(original), head))
        self.a.enact("push", "--no-ci", cwd=original, session="old", code=3)
        self.assertTrue(original.exists())

    def test_legacy_handoff_requires_recorded_child_and_known_route(self):
        bead = self.a.create("legacy reservation")
        self.a.bd("update", bead, "--assignee", "vizier:host-a", "--status", "in_progress",
                  "--set-metadata", "enact_spawn_host=host-a",
                  "--set-metadata", "enact_vizier=sess-a")
        self.a.enact("start", bead, session="wrong", code=3)
        self.a.enact("meta", bead, "enact_session=legacy")
        self.a.enact("start", bead, session="legacy")
        self.a.enact("release", bead, "--reason", "unknown provider", "--expected-owner", "legacy",
                     "--expected-session", "legacy", "--inactive", "missing", "--evidence",
                     "unsupported lookup", session="marshal", code=3)
        self.assertFalse(self.a.enact("status")["working"][0]["routing_known"])
        self.a.bd("update", bead, "--set-metadata", "enact_spawn_provider=claude")
        # A provider alone does not prove which app/project can inspect it.
        self.a.enact("release", bead, "--reason", "unknown app", "--expected-owner", "legacy",
                     "--expected-session", "legacy", "--inactive", "missing", "--evidence",
                     "unsupported lookup", session="marshal", code=3)
        self.a.enact("close", bead, "--reason", "legacy completed", session="legacy")

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

    def test_worker_recovery_requires_route_and_notices_do_not_restart_holds(self):
        epic = self.a.create("worker recovery", type="epic")
        task = self.a.create("worker", parent=epic)
        route = self.a.enact("run", "register", epic, "--authorized")
        step = self.a.enact("next", "--epic", epic)
        self.start_worker(self.a, task, "worker")
        command = ("run", "recover", epic, "--kind", "worker", "--bead", task,
                   "--session", "worker", "--evidence", "successful lookup: idle")
        self.b.enact(*command, code=3)
        self.assertTrue(self.a.enact(*command)["allowed"])
        self.assertEqual(self.a.enact(*command)["reason"], "cooldown")
        self.a.enact("run", "pause", epic, "--reason", "user stop")
        self.a.enact(*command, code=3)
        notice = ("run", "recover", epic, "--kind", "notice", "--session", "sess-a",
                  "--evidence", "user action needed")
        self.assertTrue(self.a.enact(*notice)["allowed"])
        self.assertEqual(self.a.enact(*notice)["reason"], "exhausted")
        self.a.bd("update", epic, "--set-metadata", "enact_marshal_notified=2020-01-01T00:00:00Z")
        status = self.a.enact("status")
        self.assertEqual(status["marshal_name"], route["marshal_name"])
        self.assertFalse(status["active_runs"][0]["recoverable"])
        self.assertEqual(self.a.enact("next", "--epic", epic)["action"], "paused")
        self.a.enact("meta", epic, 'enact_control={"state":"active"}', code=3)
        # Legacy callers get a safe fallback without inventing authorization.
        self.assertEqual(self.b.enact("next", "--epic", epic)["action"], "paused")
        notice = ("run", "recover", epic, "--kind", "notice", "--session", "sess-b",
                  "--evidence", "registration needs original authorization")
        self.assertTrue(self.b.enact(*notice)["allowed"])
        self.assertEqual(self.b.enact(*notice)["reason"], "exhausted")

    def test_shared_run_pause_blocks_peers_but_allows_delivery(self):
        epic = self.a.create("shared run", type="epic")
        first = self.a.create("in flight", parent=epic, priority=1)
        second = self.a.create("later", parent=epic)
        self.a.enact("run", "register", epic, "--authorized")
        status = self.a.enact("status")
        self.assertTrue(status["sweep_needed"])
        self.assertEqual(status["active_runs"][0]["epic"], epic)
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual(step["bead"], first)
        self.start_worker(self.a, first, "worker")
        path = Path(self.a.enact("worktree", "create", first, session="worker")["path"])
        self.a.enact("run", "pause", epic, "--reason", "user requested stop")
        self.assertEqual(self.b.enact("next", "--epic", epic)["action"], "paused")
        self.b.enact("claim", second, code=3)
        self.a.enact("start", first, "--reservation", step["reservation"], session="worker", code=3)
        self.a.commit_file("finished", "in-flight work", cwd=path)
        self.a.enact("push", "--bead", first, "--no-ci", cwd=path, session="worker")
        self.a.enact("close", first, "--reason", "finished in flight", session="worker")
        self.assertEqual(self.a.show(first)["status"], "closed")
        self.assertNotEqual(self.a.show(epic)["status"], "closed")
        self.a.enact("run", "recover", epic, "--kind", "vizier", "--session", "sess-a",
                     "--evidence", "successful lookup: stopped", code=3)
        self.b.enact("run", "resume", epic, "--reason", "automatic retry", code=3)
        self.b.enact("run", "resume", epic, "--explicit", "--reason", "user says resume")
        self.assertEqual(self.a.enact("next", "--epic", epic)["bead"], second)

    def test_blocked_acceptance_and_recovery_history_survive_sweeps(self):
        epic = self.a.create("acceptance needs new access", type="epic",
                             acceptance="Required access is available")
        task = self.a.create("implementation", parent=epic)
        self.a.enact("run", "register", epic, "--authorized", "--parallel", "2")
        command = ("run", "recover", epic, "--kind", "vizier", "--session", "sess-a",
                   "--evidence", "successful lookup: stopped")
        for attempt in range(3):
            result = self.a.enact(*command)
            self.assertTrue(result["allowed"])
            self.assertEqual(result["remaining"], 2 - attempt)
            self.assertEqual(result["route"]["parallel"], 2)
            if attempt == 0:
                self.assertEqual(self.a.enact(*command)["reason"], "cooldown")
            # Simulate the passage of hourly sweeps in the real shared graph.
            bead = self.a.show(epic)
            key = next(k for k in bead["metadata"] if k.startswith("enact_marshal_"))
            history = bead["metadata"][key]
            if isinstance(history, str):
                history = json.loads(history)
            history[-1]["at"] = "2020-01-01T00:00:00+00:00"
            self.a.bd("update", epic, "--set-metadata", key + "=" + json.dumps(history))
        self.assertEqual(self.a.enact(*command)["reason"], "exhausted")
        self.a.enact("run", "block", epic, "--exhausted", "--reason", "Justiciar: access unavailable")
        self.b.enact("claim", task, code=3)
        self.assertEqual(self.b.enact("next", "--epic", epic)["action"], "blocked")
        self.a.enact(*command, code=3)
        # Even otherwise eligible epics must retain unmet whole-run acceptance.
        self.a.bd("close", task, "--reason", "implementation finished")
        self.assertEqual(self.a.enact("next", "--epic", epic)["action"], "blocked")
        self.assertNotEqual(self.a.show(epic)["status"], "closed")
        self.assertEqual(self.a.show(epic)["acceptance_criteria"], "Required access is available")
        self.a.enact("run", "resume", epic, "--explicit", "--reason", "user supplied access")
        self.assertTrue(self.a.enact(*command)["allowed"])
        self.assertEqual(self.a.enact("next", "--epic", epic)["action"], "done")
        self.assertEqual(self.b.enact("next", "--epic", epic)["action"], "done")

    def test_vizier_schedules_archives_and_finishes(self):
        epic = self.a.create("epic", type="epic")
        first = self.a.create("first", parent=epic, priority=1)
        second = self.a.create("second", parent=epic, labels="role:warden")
        self.a.bd("dep", "add", second, first)
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual((step["action"], step["bead"], step["role"]), ("spawn", first, "executor"))
        self.assertIn("executor.md", step["prompt"])
        self.a.enact("meta", first, "enact_session=exec-1", "--reservation", step["reservation"])
        self.start_worker(self.a, first, "exec-1")
        self.a.enact("close", first, "--reason", "done", session="exec-1")
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual(step["archive"], [{"bead": first, "session": "exec-1"}])
        self.assertEqual((step["action"], step["bead"], step["role"]), ("spawn", second, "warden"))
        self.start_worker(self.a, second, "exec-2")
        self.a.enact("close", second, "--reason", "done", session="exec-2")
        self.assertEqual(self.a.show(epic)["status"], "closed")
        self.assertEqual(self.a.enact("next", "--epic", epic)["action"], "done")

    def test_closed_run_waits_for_failed_ci_and_steward_delivery(self):
        epic = self.a.create("final delivery", type="epic")
        bead = self.a.create("last task", parent=epic)
        path = Path(self.a.enact("worktree", "create", bead)["path"])
        self.a.commit_file("SLOW", "failure", cwd=path)
        pushed = self.a.enact("push", "--bead", bead, "--no-ci", cwd=path)
        self.a.enact("close", bead, "--reason", "delivered")
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual((step["action"], step["reason"]), ("wait", "delivery pending"))
        self.assertEqual(step["delivery"]["pending"], pushed["commits"])
        self.wait_for_ci(self.a)
        status = self.a.enact("status")
        self.assertIn(epic, status["pending_deliveries"])
        self.assertEqual(status["staging"], self.remote_sha("staging"))
        self.assertTrue(status["sweep_needed"])
        self.assertIn(epic, [r["epic"] for r in status["active_runs"]])
        self.assertFalse(status["ci"]["result"]["passed"])
        steward = status["open_stewards"][0]
        self.assertIn(epic, self.a.show(steward)["metadata"]["enact_runs"].split())
        # Legacy failures remain attributable by their recorded failing range.
        self.a.bd("update", steward, "--unset-metadata", "enact_runs")
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual((step["action"], step["bead"]), ("spawn", steward))
        self.start_worker(self.a, steward, "steward")
        path = Path(self.a.enact("worktree", "create", steward, session="steward")["path"])
        self.a.git("rm", "SLOW", cwd=path)
        self.a.git("commit", "-qm", "repair slow check", cwd=path)
        repaired = self.a.enact("push", "--bead", steward, "--no-ci", cwd=path, session="steward")
        self.a.enact("close", steward, "--reason", "fixed", session="steward")
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual(step["action"], "wait")
        self.assertIn(repaired["staging"], step["delivery"]["pending"])
        self.wait_for_ci(self.a)
        # Unrelated later staging and its CI failure must not hold this run.
        path = Path(self.a.enact("worktree", "create", "unrelated")["path"])
        self.a.commit_file("SLOW", "different run", cwd=path)
        self.a.enact("push", "--no-ci", cwd=path)
        self.a.enact("ci")
        self.assertNotEqual(self.remote_sha("master"), self.remote_sha("staging"))
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual(step["action"], "done")
        self.assertTrue(step["delivery"]["complete"])
        self.assertIn(repaired["staging"], step["delivery"]["commits"])


if __name__ == "__main__":
    unittest.main()
