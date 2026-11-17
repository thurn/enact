"""Slow black-box tests: enact.py against real git, bd and Dolt.

Each test builds a bare "origin" plus clones that stand in for separate
machines, then drives the CLI exactly as agents do.
"""

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "enact.py"

ENACT_TOML = """\
master_branch = "master"
[checks]
fast = "python3 check.py fast"
slow = "python3 check.py slow"
"""

CHECK_PY = """\
import pathlib, sys
if pathlib.Path(sys.argv[1].upper()).exists():
    for i in range(80):
        print("stderr noise", i, file=sys.stderr, flush=True)
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


def stop_processes_in(path):
    """Stop every process started from `path`, and what each started.

    A slow-CI runner and its check outlive the test that launched them, and
    a runner may hold its lock guard between lock cycles. On Windows they
    keep the fixture open, so the next test could not delete it.
    """
    query = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
             "$_.CommandLine.Contains($env:ENACT_TEST_TREE) } | "
             "ForEach-Object { $_.ProcessId }")
    pids = subprocess.run(["powershell", "-NoProfile", "-Command", query],
                          capture_output=True, text=True,
                          env={**os.environ, "ENACT_TEST_TREE": str(path)}).stdout.split()
    for pid in pids:
        if pid.isdigit() and int(pid) != os.getpid():
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)


def rmtree(path, timeout=60):
    """shutil.rmtree that also deletes read-only files (git's objects on Windows).

    Windows deletes no file a process holds open, nor its working directory.
    A slow-CI runner holds both in its repository until it exits, a moment
    after it releases its lock, so a busy tree is retried until it is free.
    """
    def writable(func, target, error):
        # Only a failed removal can be retried as writable. Adding the write
        # bit keeps a directory listable; S_IWRITE alone made it unreadable,
        # so one busy tree broke every later scenario's setUp.
        if func not in (os.unlink, os.remove, os.rmdir):
            raise error[1] if isinstance(error, tuple) else error  # onerror passes exc_info
        os.chmod(target, os.lstat(target).st_mode | stat.S_IWRITE)
        func(target)
    deadline = time.time() + timeout
    while True:
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=writable)
            else:
                shutil.rmtree(path, onerror=writable)
            return
        except PermissionError:
            if os.name != "nt" or time.time() > deadline:
                raise
            time.sleep(0.5)


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
                # POSIX shell on every OS (Git for Windows' bash on Windows).
                slow = shlex.join([Path(sys.executable).as_posix(), "check.py"])
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
                    "enact.backup_tickets = lambda *_: {}\n"
                    "enact.post_ci_event = lambda *_, **kw: None\n"
                    "enact.Tickets.ticket = lambda *args: {\"seq\": 0, \"tickets\": [], \"edges\": []} if args[1] == \"graph\" else []\n"
                    f"root, phase = Path({str(root)!r}), {phase!r}\n"
                    "def pause():\n"
                    "    (root / 'paused').write_text(str(os.getpid()))\n"
                    "    while not (root / 'resume').exists(): time.sleep(0.02)\n"
                    "original_spawn, original_write = enact.subprocess.Popen, enact.write_json\n"
                    "def spawn(*args, **kwargs):\n"
                    "    command = args[0] if args else kwargs['args']\n"
                    "    target = '--check-worker' in command\n"
                    "    if target and phase == 'before_spawn': pause()\n"
                    "    child = original_spawn(*args, **kwargs)\n"
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
                runner = root / "runner.py"
                runner.write_text(
                    "import runpy, sys\n"
                    f"main = runpy.run_path({str(SCRIPT)!r}, run_name='test_runner')['main']\n"
                    "namespace = main.__globals__\n"
                    "namespace['__file__'] = __file__\n"
                    "namespace['backup_tickets'] = lambda *args: {}\n"
                    "namespace['post_ci_event'] = lambda *args, **kwargs: None\n"
                    "namespace['Tickets'].ticket = lambda *args: {\"seq\": 0, \"tickets\": [], \"edges\": []} if args[1] == \"graph\" else []\n"
                    "sys.exit(main())\n"
                )
                state = repo / ".git" / "enact"
                if phase in ("cleanup", "recording"):
                    (root / "finish-check").touch()

                def cli(*args):
                    result = subprocess.run(
                        [sys.executable, str(runner), "ci", *args], cwd=repo,
                        text=True, capture_output=True, timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return json.loads(result.stdout)

                with (root / "coordinator.log").open("w") as output:
                    entrypoint = runner if phase == "running" else wrapper
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


@unittest.skipUnless(shutil.which("git"), "needs git")
@unittest.skipIf(os.name == "nt", "reaping is skipped on Windows")
class WorktreeRemovalReapsTest(unittest.TestCase):
    def test_removing_a_worktree_ends_processes_running_in_it(self):
        spec = importlib.util.spec_from_file_location("enact_reap", SCRIPT)
        enact = importlib.util.module_from_spec(spec)
        sys.modules["enact_reap"] = enact
        spec.loader.exec_module(enact)
        with tempfile.TemporaryDirectory() as directory:
            repo_dir = Path(directory).resolve() / "repo"
            sh(["git", "init", "-q", "-b", "master", str(repo_dir)], Path(directory))
            sh(["git", "config", "user.email", "test@example.com"], repo_dir)
            sh(["git", "config", "user.name", "Test"], repo_dir)
            sh(["git", "commit", "-q", "--allow-empty", "-m", "initial"], repo_dir)
            repo = enact.find_repo(repo_dir)
            tree = repo_dir / ".worktrees" / "bead"
            sh(["git", "worktree", "add", "-q", "-b", "enact/bead", str(tree)], repo_dir)
            sleeper = subprocess.Popen(["sleep", "60"], cwd=tree, start_new_session=True)
            try:
                ended = enact.remove_worktree(repo, tree, "enact/bead")
                self.assertEqual([e["pid"] for e in ended], [sleeper.pid])
                sleeper.wait(timeout=10)
                self.assertFalse(tree.exists())
            finally:
                sleeper.kill()
                sleeper.wait()


@unittest.skipUnless(shutil.which("git"), "needs git")
class WorktreeSweepTest(unittest.TestCase):
    """Real git and CLI; a harness stands in for the Beads graph and sync."""

    def test_sweep_deletes_only_trees_nothing_needs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, origin = root / "repo", root / "origin.git"
            sh(["git", "init", "-q", "--bare", "-b", "master", str(origin)], root)
            sh(["git", "init", "-q", "-b", "master", str(repo)], root)
            sh(["git", "config", "user.email", "test@example.com"], repo)
            sh(["git", "config", "user.name", "Test"], repo)
            slow = shlex.join([Path(sys.executable).as_posix(), "-c", "pass"])
            (repo / ".enact.toml").write_text(f"[checks]\nslow = {json.dumps(slow)}\n")
            (repo / ".gitignore").write_text("/.worktrees/\n")
            sh(["git", "add", "."], repo)
            sh(["git", "commit", "-qm", "initial"], repo)
            sh(["git", "remote", "add", "origin", str(origin)], repo)
            sh(["git", "push", "-q", "origin", "HEAD:master", "HEAD:staging"], repo)
            sh(["git", "fetch", "-q", "origin"], repo)
            harness = root / "harness.py"
            harness.write_text(
                "import runpy, sys\n"
                f"module = runpy.run_path({str(SCRIPT)!r}, run_name='sweep_test')\n"
                "namespace = module['main'].__globals__\n"
                "namespace['__file__'] = __file__\n"
                "namespace['backup_tickets'] = lambda *args: {}\n"
                "namespace['post_ci_event'] = lambda *args, **kwargs: None\n"
                "namespace['Tickets'].all = lambda self: [\n"
                "    {'id': 'done', 'status': 'closed'}, {'id': 'dirty', 'status': 'closed'},\n"
                "    {'id': 'ahead', 'status': 'closed'}, {'id': 'open', 'status': 'open'}]\n"
                "sys.exit(module['main']())\n"
            )

            def enact(*args, cwd=repo):
                return json.loads(sh([sys.executable, str(harness), *args], cwd))

            trees = repo / ".worktrees"

            def tree(name, *branch):
                path = trees / name
                sh(["git", "worktree", "add", "-q", *branch, str(path), "origin/staging"], repo)
                return path

            for name in ("done", "dirty", "ahead", "open", "stray"):
                tree(name, "-b", f"enact/{name}")
            (trees / "dirty" / "notes.log").write_text("scratch")
            sh(["git", "commit", "-q", "--allow-empty", "-m", "unpushed"], trees / "ahead")
            tree("human", "-b", "feature")
            sh(["git", "worktree", "lock", "--reason", "initializing",
                str(tree("ci-0123456789ab", "--detach"))], repo)
            tree("pool/spare", "--detach")  # a hook's pool of ready trees
            old = time.time() - 2 * 3600
            for name, marker in (("stale", "obj/out.dll"), ("lost", ".git"),
                                 (".cache", "x"), ("pool", "x"), ("fresh", "x")):
                (trees / name / marker).parent.mkdir(parents=True, exist_ok=True)
                (trees / name / marker).write_text("gitdir: /missing\n")
                if name != "fresh":
                    os.utime(trees / name, (old, old))
            (trees / "build.log").write_text("log")

            # Delivery reports a tree it keeps rather than leaving it silently.
            stray = trees / "stray"
            sh(["git", "commit", "-q", "--allow-empty", "-m", "delivered"], stray)
            (stray / "capture.log").write_text("scratch")
            pushed = enact("push", "--no-ci", cwd=stray)
            self.assertTrue(pushed["pushed"])
            self.assertIn("untracked files", pushed["kept_worktree"])

            swept = enact("worktree", "prune")
            self.assertEqual(sorted(Path(p).name for p in swept["removed"]), ["done", "stale"])
            self.assertEqual(sorted(Path(p).name for p in swept["kept"]), [
                ".cache", "ahead", "ci-0123456789ab", "dirty", "fresh", "human", "lost",
                "open", "pool", "stray"])
            self.assertEqual(sh(["git", "branch", "--list", "enact/done"], repo), "")
            self.assertTrue((trees / "build.log").exists())
            # Only the runner, under its lock, removes its dead trees.
            self.assertTrue((trees / "ci-0123456789ab").exists())
            enact("ci")
            self.assertFalse((trees / "ci-0123456789ab").exists())
            self.assertTrue((trees / "dirty").exists())


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
            "CLAUDE_CODE_HOST_SESSION_ID": "",
            "CODEX_THREAD_ID": "",
            "BD_NON_INTERACTIVE": "1",
            # Never fast-forward the real Enact checkout from a test.
            "ENACT_NO_SELF_UPDATE": "1",
            # No background pusher outlives a test; one test opts back in.
            "ENACT_SYNC_NOW": "1",
        }

    def enact(self, *args, code=0, cwd=None, session=None, env=None):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=cwd or self.path,
            text=True,
            capture_output=True,
            env={**os.environ, **self.env(session), **(env or {})},
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






@unittest.skipUnless(shutil.which("git"), "needs git")
class TicketBackendTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "checkout"
        self.repo.mkdir()
        self.trace = root / "requests"
        self.trace.touch()
        self.stub = SCRIPT.with_name("tk_stub.py")
        executable = root / ("tk.cmd" if os.name == "nt" else "tk")
        self.env = {
            **os.environ, "TK_STUB_DB": str(root / "tickets.sqlite"),
            "TK_STUB_TRACE": str(self.trace), "ENACT_TK": str(executable),
            "ENACT_NO_SELF_UPDATE": "1", "ENACT_PROVIDER": "codex", "ENACT_HOST": "test-host",
        }
        if os.name == "nt":
            executable.write_text(f'@"{sys.executable}" "{self.stub}" %*\n')
        else:
            executable.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(self.stub))} \"$@\"\n")
            executable.chmod(0o755)
        sh(["git", "init", "-q"], self.repo)
        sh(["git", "config", "user.name", "Test"], self.repo)
        sh(["git", "config", "user.email", "test@example.com"], self.repo)
        sh(["git", "commit", "-qm", "Initial commit", "--allow-empty"], self.repo)
        sh(["git", "branch", "-M", "master"], self.repo)
        sh(["git", "branch", "staging"], self.repo)
        sh(["git", "remote", "add", "origin", "."], self.repo)
        self.config = self.repo / ".enact.toml"
        self.config.write_text('[tickets]\nproject = "by"\nurl = "http://fixture.test"\n')

    def tk(self, *args, data=None, actor="vizier"):
        proc = subprocess.run([sys.executable, str(self.stub), "--json", "--actor", actor, *args],
                              cwd=self.repo, env=self.env, input=json.dumps(data),
                              capture_output=True, text=True, check=True)
        return json.loads(proc.stdout)

    def enact(self, *args, actor="vizier", code=0):
        self.trace.write_text("")
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.repo,
                              env={**self.env, "ENACT_SESSION": actor}, capture_output=True, text=True)
        self.assertEqual(proc.returncode, code, proc.stdout + proc.stderr)
        self.assertFalse((self.repo / ".git" / "enact" / "ownership.lock").exists())
        return json.loads(proc.stdout), self.trace.read_text().splitlines()

    def test_reserve_start_close_and_continue_a_lane(self):
        epic, first, second = [r["id"] for r in self.tk("batch", data={"ops": [
            {"op": "create", "fields": {"title": "Run", "type": "epic", "body": "Plan"}},
            {"op": "create", "parent": "$0", "fields": {"title": "First", "labels": ["lane:test"]}},
            {"op": "create", "parent": "$0", "deps": ["$1"],
             "fields": {"title": "Second", "labels": ["lane:test"]}},
        ]})["results"]]
        step, requests = self.enact("next", "--epic", "by-" + epic)
        self.assertEqual((step["action"], step["bead"]), ("spawn", first))
        self.assertEqual(requests, ["lease", "graph", "leases", "batch"])
        self.enact("meta", first, "enact_session=worker", "--reservation", step["reservation"])
        self.enact("start", first, "--reservation", "wrong", actor="worker", code=3)
        started, requests = self.enact("start", first, "--reservation", step["reservation"], actor="worker")
        self.assertEqual(started["bead"]["assignee"], "worker")
        self.assertEqual(requests, ["graph", "batch"])
        self.enact("close", first, "--reason", "wrong owner", actor="stale", code=3)
        continued, requests = self.enact("close", first, "--continue", "--reason", "done", actor="worker")
        self.assertEqual(continued["next"]["id"], second)
        self.assertEqual(requests, ["graph", "batch", "batch"])
        brief, _ = self.enact("brief", second, actor="worker")
        self.assertEqual(brief["epics"][0]["description"], "Plan")
        self.assertEqual(brief["dependencies"][0]["status"], "closed")
        closed, requests = self.enact("close", second, "--reason", "done", actor="worker")
        self.assertEqual(closed["epics_closed"], [epic])
        self.assertEqual(requests, ["graph", "batch"])
        done, requests = self.enact("next", "--epic", epic)
        self.assertEqual(done["action"], "done")
        self.assertEqual(done["archive"], [{"bead": first, "session": "worker"},
                                           {"bead": second, "session": "worker"}])
        self.assertEqual(requests, ["lease", "graph", "leases", "lease"])
        self.enact("meta", first, "enact_archived=true")
        changes = self.tk("events", "--kind", "session")
        self.assertEqual([(e["data"]["ticket"], e["data"]["action"]) for e in changes],
                         [(first, "archive"), (second, "close"), (first, "close"),
                          (first, "start"), (first, "spawn")])
        self.assertFalse(self.tk("leases"))
        self.assertFalse(list((self.repo / ".git" / "enact").glob("vizier-*.json")))

    def test_backend_requires_project_and_url(self):
        self.assertEqual(self.enact("backend"),
                         ({"backend": "server", "tk": self.env["ENACT_TK"]}, []))
        self.config.write_text('[tickets]\nproject = "by"\n')
        failure, requests = self.enact("backend", code=1)
        self.assertIn("requires [tickets].project and [tickets].url", failure["error"])
        self.assertEqual(requests, [])

    def test_run_holds_and_websocket_wait_commands(self):
        epic, child = [r["id"] for r in self.tk("batch", data={"ops": [
            {"op": "create", "fields": {"title": "Run", "type": "epic"}},
            {"op": "create", "parent": "$0", "fields": {"title": "Child"}},
        ]})["results"]]
        self.enact("run", "register", epic)
        self.enact("run", "pause", epic, "--reason", "hold")
        self.enact("claim", child, actor="worker", code=3)
        self.assertEqual(self.enact("next", "--epic", epic)[0]["action"], "paused")
        self.enact("run", "resume", epic, "--reason", "continue", "--explicit")
        self.assertEqual(self.enact("next", "--epic", epic)[0]["action"], "spawn")
        result, requests = self.enact("next", "--epic", epic, "--wait", "60")
        self.assertEqual((result["action"], result["reason"]), ("wait", "capacity"))
        self.assertEqual(requests, ["lease", "graph", "leases", "wait", "graph", "leases"])
        result, requests = self.enact("wait", child, "--timeout", "60")
        self.assertEqual(result["state"], "waiting")
        self.assertEqual(requests, ["graph", "wait", "graph"])

    def test_next_waits_for_a_routable_ticket_whose_only_blocker_is_time(self):
        epic, prerequisite, _ = [r["id"] for r in self.tk("batch", data={"ops": [
            {"op": "create", "fields": {"title": "Scheduled run", "type": "epic"}},
            {"op": "create", "parent": "$0",
             "fields": {"title": "Scheduled prerequisite", "defer_until": "2099-01-01"}},
            {"op": "create", "parent": "$0", "deps": ["$1"], "fields": {"title": "Dependent"}},
        ]})["results"]]
        step, _ = self.enact("next", "--epic", epic)
        self.assertEqual((step["action"], step["reason"]), ("wait", "scheduled work"))

        self.tk("batch", data={"ops": [
            {"op": "update", "id": prerequisite, "set_meta": {"enact_host": "unserved"}},
        ]})
        step, _ = self.enact("next", "--epic", epic)
        self.assertEqual((step["action"], step["stranded"]), ("deadlock", [prerequisite]))

        self.tk("batch", data={"ops": [
            {"op": "update", "id": prerequisite, "unset_meta": ["enact_host"]},
            {"op": "defer", "id": prerequisite, "until": "2099-01-01"},
        ]})
        step, _ = self.enact("next", "--epic", epic)
        self.assertEqual((step["action"], step["reason"]), ("wait", "scheduled work"))

        self.tk("batch", data={"ops": [
            {"op": "reopen", "id": prerequisite},
            {"op": "update", "id": prerequisite, "set": {"defer_until": "2000-01-01"}},
        ]})
        step, _ = self.enact("next", "--epic", epic)
        self.assertEqual((step["action"], step["bead"]), ("spawn", prerequisite))

    def test_server_scheduler_holds_a_legacy_archivist_during_an_imported_gate(self):
        run, _, audit, archivist = [r["id"] for r in self.tk("batch", data={"ops": [
            {"op": "create", "fields": {"title": "Run", "type": "epic"}},
            {"op": "create", "parent": "$0", "fields": {"title": "Review gate", "type": "epic"}},
            {"op": "create", "parent": "$1", "fields": {"title": "Mason", "labels": ["role:mason"]}},
            {"op": "create", "parent": "$0",
             "fields": {"title": "Archivist", "labels": ["role:archivist"], "priority": 0}},
        ]})["results"]]
        self.assertTrue(self.tk("show", archivist)["ready"])
        step, _ = self.enact("next", "--epic", run)
        self.assertEqual((step["action"], step["bead"]), ("spawn", audit))






if __name__ == "__main__":
    unittest.main()
