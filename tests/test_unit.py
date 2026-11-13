"""Fast tests: helper behavior that needs no git or Beads."""

import importlib.util
import contextlib
import io
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "enact.py"
spec = importlib.util.spec_from_file_location("enact", SCRIPT)
enact = importlib.util.module_from_spec(spec)
sys.modules["enact"] = enact
spec.loader.exec_module(enact)


def lock_runner(path, mode, connection):
    """A real contender, with barriers at the two former race windows."""
    path = Path(path)
    if mode == "publish":
        original = enact.os.open

        def paused_open(name, flags, *args, **kwargs):
            fd = original(name, flags, *args, **kwargs)
            if flags & os.O_EXCL:
                connection.send("paused")
                connection.recv()
            return fd

        enact.os.open = paused_open
    elif mode == "stale":
        original = enact.pid_alive

        def paused_alive(pid):
            alive = original(pid)
            connection.send("paused")
            connection.recv()
            return alive

        enact.pid_alive = paused_alive
    elif mode == "legacy":
        with path.open("x") as handle:
            connection.send("paused")
            connection.recv()
            handle.write(str(os.getpid()))
        connection.send("entered")
        connection.recv()
        path.unlink()
        return
    guard = enact.try_lock(path)
    connection.send("entered" if guard else "busy")
    if guard:
        connection.recv()
        if hasattr(enact, "release_lock"):
            enact.release_lock(path, guard)
        else:  # also reproduce the regression against the old helper
            path.unlink(missing_ok=True)
    connection.close()


class LockTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "ci.lock"
        self.context = multiprocessing.get_context("spawn")

    def runner(self, mode="normal"):
        parent, child = self.context.Pipe()
        process = self.context.Process(
            target=lock_runner, args=(str(self.path), mode, child)
        )
        process.start()
        child.close()

        def cleanup():
            if process.is_alive():
                process.terminate()
            process.join(10)
            parent.close()
            process.close()

        self.addCleanup(cleanup)
        return process, parent

    def receive(self, connection):
        self.assertTrue(connection.poll(10), "runner did not reach its barrier")
        return connection.recv()

    def finish(self, process, connection):
        connection.send("release")
        process.join(10)
        self.assertEqual(process.exitcode, 0)

    def test_startup_before_pid_publication_excludes_second_runner(self):
        first, one = self.runner("publish")
        self.assertEqual(self.receive(one), "paused")
        second, two = self.runner()
        self.assertEqual(self.receive(two), "busy")
        second.join(10)
        one.send("publish")
        self.assertEqual(self.receive(one), "entered")
        self.assertEqual(self.path.read_text(), str(first.pid))
        self.finish(first, one)
        self.assertFalse(self.path.exists())

    def test_stale_owner_recovery_excludes_competing_reaper(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=10)
        self.path.write_text(str(dead.pid))
        first, one = self.runner("stale")
        self.assertEqual(self.receive(one), "paused")
        second, two = self.runner()
        self.assertEqual(self.receive(two), "busy")
        second.join(10)
        one.send("recover")
        self.assertEqual(self.receive(one), "entered")
        self.assertEqual(self.path.read_text(), str(first.pid))
        self.finish(first, one)

    def test_crash_before_and_after_publication_recovers(self):
        for published in (False, True):
            with self.subTest(published=published):
                first, one = self.runner("publish")
                self.assertEqual(self.receive(one), "paused")
                if published:
                    one.send("publish")
                    self.assertEqual(self.receive(one), "entered")
                first.terminate()
                first.join(10)
                second, two = self.runner()
                self.assertEqual(self.receive(two), "entered")
                self.finish(second, two)
                self.assertFalse(self.path.exists())

    def test_legacy_writer_is_respected_before_and_after_publication(self):
        legacy, old = self.runner("legacy")
        self.assertEqual(self.receive(old), "paused")
        for published in (False, True):
            with self.subTest(published=published):
                if published:
                    old.send("publish")
                    self.assertEqual(self.receive(old), "entered")
                process, connection = self.runner()
                self.assertEqual(self.receive(connection), "busy")
                process.join(10)
        legacy.terminate()
        legacy.join(10)
        process, connection = self.runner()
        self.assertEqual(self.receive(connection), "entered")
        self.finish(process, connection)

    def test_stale_observation_cannot_remove_replaced_legacy_owner(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=10)
        self.path.write_text(str(dead.pid))
        first, one = self.runner("stale")
        self.assertEqual(self.receive(one), "paused")
        replacement = self.path.with_name("replacement")
        replacement.write_text(str(os.getpid()))
        replacement.replace(self.path)
        one.send("recover")
        self.assertEqual(self.receive(one), "busy")
        first.join(10)
        self.assertEqual(self.path.read_text(), str(os.getpid()))


class ConfigTest(unittest.TestCase):
    def test_parse_full_config(self):
        cfg = enact.Config.parse(
            """
            master_branch = "main"
            [checks]
            fast = "make fast"
            slow = "make slow"
            [worktree]
            create = "hook create"
            [roles.warden.claude]
            model = "opus"
            effort = "max"
            [tiers.light.claude]
            model = "sonnet"
            """
        )
        self.assertEqual(cfg.master, "main")
        self.assertEqual(cfg.staging, "staging")
        self.assertEqual((cfg.fast, cfg.slow), ("make fast", "make slow"))
        self.assertEqual(cfg.worktree_create, "hook create")
        self.assertIsNone(cfg.worktree_remove)
        self.assertEqual(cfg.role_settings("warden", "claude"), {"model": "opus", "effort": "max"})
        self.assertEqual(cfg.role_settings("warden", "codex"), {})
        # A bead's tier overrides its role's model; unknown tiers change nothing.
        self.assertEqual(cfg.role_settings("warden", "claude", "light"),
                         {"model": "sonnet", "effort": "max"})
        self.assertEqual(cfg.role_settings("executor", "claude", "heavy"), {})
        # `tier:fable`, `tier:opus` and `tier:sonnet` need no configuration, for Claude only;
        # a project's own entry for the tier wins.
        self.assertEqual(cfg.role_settings("executor", "claude", "opus"), {"model": "opus"})
        self.assertEqual(cfg.role_settings("warden", "claude", "fable"),
                         {"model": "fable", "effort": "max"})
        self.assertEqual(cfg.role_settings("executor", "codex", "fable"), {})
        self.assertEqual(cfg.role_settings("warden", "claude", "sonnet"),
                         {"model": "sonnet", "effort": "max"})
        self.assertEqual(cfg.role_settings("executor", "codex", "opus"), {})
        mapped = enact.Config.parse(
            '[tiers.opus.claude]\nmodel = "fable"\n[tiers.opus.codex]\nmodel = "sol"\n')
        self.assertEqual(mapped.role_settings("executor", "claude", "opus"), {"model": "fable"})
        self.assertEqual(mapped.role_settings("executor", "codex", "opus"), {"model": "sol"})
        self.assertEqual(cfg.slow_timeout, enact.DEFAULT_SLOW_TIMEOUT)
        self.assertEqual(enact.Config.parse("[checks]\nslow_timeout = 900\n").slow_timeout, 900)
        self.assertFalse(cfg.push_queue)
        queue = enact.Config.parse("[push]\nqueue = true\nbatch = 3\n")
        self.assertEqual((queue.push_queue, queue.push_batch), (True, 3))


class SetAsideTest(unittest.TestCase):
    """Which branches a failed merge-queue batch checks apart from the rest."""

    entries = [{"id": str(i), "files": [f"src/f{i}.py"]} for i in range(5)]

    def ids(self, output):
        return [e["id"] for e in enact.set_aside(self.entries, output)]

    def test_the_branches_whose_files_the_output_names(self):
        self.assertEqual(self.ids("src/f3.py: 900 lines, limit 800"), ["3"])

    def test_the_later_half_when_the_output_names_none_or_all(self):
        self.assertEqual(self.ids("a test failed"), ["2", "3", "4"])
        self.assertEqual(self.ids(" ".join(f"src/f{i}.py" for i in range(5))), ["2", "3", "4"])


class CiGapTest(unittest.TestCase):
    def test_config_default_and_setting(self):
        self.assertEqual(enact.Config.parse("").slow_min_gap, 0)
        self.assertEqual(enact.Config.parse("[checks]\nslow_min_gap = 600\n").slow_min_gap, 600)

    def test_remaining_wait(self):
        self.assertEqual(enact.ci_gap_remaining(None, 1000.0, 600), 0.0)  # never ran
        self.assertEqual(enact.ci_gap_remaining(900.0, 1000.0, 0), 0.0)  # no gap configured
        self.assertEqual(enact.ci_gap_remaining(900.0, 1000.0, 600), 500.0)
        self.assertEqual(enact.ci_gap_remaining(100.0, 1000.0, 600), 0.0)  # gap already passed
        # A clock that moved backwards never waits longer than the gap.
        self.assertEqual(enact.ci_gap_remaining(5000.0, 1000.0, 600), 600.0)


class RunBoundedTest(unittest.TestCase):
    def test_output_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = enact.run_bounded("echo hi; exit 3", Path(tmp), 30)
        self.assertEqual((code, out.strip()), (3, "hi"))

    @unittest.skipIf(enact.WINDOWS, "process groups are POSIX here")
    def test_a_hung_check_is_killed_with_its_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "pid"
            started = time.monotonic()
            code, out = enact.run_bounded(f"echo before; sleep 60 & echo $! > {pidfile}; wait",
                                          Path(tmp), 1)
            self.assertLess(time.monotonic() - started, 20)
            self.assertEqual(code, 124)
            self.assertIn("before", out)
            self.assertIn("slow_timeout", out)
            grandchild = int(pidfile.read_text())
            time.sleep(0.2)
            with self.assertRaises(ProcessLookupError):
                os.kill(grandchild, 0)


@unittest.skipIf(enact.WINDOWS, "reaping is skipped on Windows")
class ReapProcessesTest(unittest.TestCase):
    def test_processes_inside_a_tree_end_and_others_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree, elsewhere = Path(tmp, "tree").resolve(), Path(tmp, "other").resolve()
            tree.mkdir()
            elsewhere.mkdir()
            inside = subprocess.Popen(["sleep", "60"], cwd=tree, start_new_session=True)
            ignores_term = subprocess.Popen(
                [sys.executable, "-c",
                 "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                 "print('ready', flush=True); time.sleep(60)"],
                cwd=tree, stdout=subprocess.PIPE, text=True, start_new_session=True)
            ignores_term.stdout.readline()  # the handler is installed
            outside = subprocess.Popen(["sleep", "60"], cwd=elsewhere)
            try:
                ended = enact.reap_processes(tree, grace=1)
                self.assertEqual({e["pid"] for e in ended}, {inside.pid, ignores_term.pid})
                self.assertIsNotNone(inside.wait(timeout=10))
                self.assertIsNotNone(ignores_term.wait(timeout=10))
                self.assertIsNone(outside.poll())
            finally:
                for process in (inside, ignores_term, outside):
                    process.kill()
                    process.wait()
                ignores_term.stdout.close()

    def test_processes_in_the_callers_group_are_spared(self):
        # `enact push | jq` puts the reader in the push's group; it must survive.
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp, "tree").resolve()
            tree.mkdir()
            sibling = subprocess.Popen(["sleep", "60"], cwd=tree)  # same group as the test
            other_group = subprocess.Popen(["sleep", "60"], cwd=tree, start_new_session=True)
            try:
                ended = enact.reap_processes(tree, grace=1)
                self.assertEqual([e["pid"] for e in ended], [other_group.pid])
                self.assertIsNotNone(other_group.wait(timeout=10))
                self.assertIsNone(sibling.poll())
            finally:
                for process in (sibling, other_group):
                    process.kill()
                    process.wait()

    def test_this_process_is_spared_even_when_its_cwd_is_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                self.assertEqual(enact.reap_processes(Path(tmp), grace=1), [])
            finally:
                os.chdir(previous)


class RunMissingTest(unittest.TestCase):
    def test_missing_tool_names_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(enact.Fail) as caught:
                enact.run(["enact-no-such-tool"], Path(tmp))
        self.assertIn("enact-no-such-tool is not installed", str(caught.exception))

    def test_missing_directory_points_to_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(enact.Fail) as caught:
                enact.run([sys.executable, "-c", "pass"], Path(tmp) / "absent")
        self.assertIn("enact setup", str(caught.exception))


class ShellTest(unittest.TestCase):
    # A POSIX command with the quoting the projects' hooks use: $(...), "$VAR", '...' and &&.
    COMMAND = """f=$(mktemp) && printf '%s|%s' "$ENACT_X" 'a "b" c' > "$f" && cat "$f"; rm -f "$f\""""

    def test_project_commands_run_in_a_posix_shell(self):
        args, shell = enact.shell_args(self.COMMAND)
        proc = subprocess.run(
            args, shell=shell, env={**os.environ, "ENACT_X": "x y"},
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        self.assertEqual((proc.returncode, proc.stdout), (0, 'x y|a "b" c'), proc.stderr)

    def test_windows_finds_git_bash_beside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("cmd/git.exe", "bin/bash.exe"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).touch()
            (root / "usr" / "bin").mkdir(parents=True)
            env = {k: v for k, v in os.environ.items() if k != "ENACT_BASH"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(enact, "WINDOWS", True), \
                    mock.patch.object(enact.shutil, "which", return_value=str(root / "cmd" / "git.exe")):
                args = enact.shell_args("true")
            self.assertEqual(args, ([str((root / "bin" / "bash.exe").resolve()), "-c", "true"], False))
            with mock.patch.dict(os.environ, {"ENACT_BASH": "custom-bash"}), \
                    mock.patch.object(enact, "WINDOWS", True):
                self.assertEqual(enact.shell_args("true")[0][0], "custom-bash")


class GraphTest(unittest.TestCase):
    def test_bulk_delivery_reuses_ancestry_and_keeps_legacy_repairs(self):
        graph = [
            {"id": "one", "issue_type": "epic"},
            {"id": "first", "parent": "one", "metadata": {"enact_commits": "a b"}},
            {"id": "two", "issue_type": "epic"},
            {"id": "second", "parent": "two", "metadata": {"enact_commits": "a"}},
            {"id": "repair", "labels": ["role:steward"], "status": "open",
             "metadata": {"enact_ci_range": "a..b", "enact_commits": "c"}},
        ]
        histories = {"a": "a", "b": "a b", "master": "a"}
        calls = []

        def git(*args, **kwargs):
            calls.append(args)
            return histories[args[1]]

        repo = types.SimpleNamespace(config=types.SimpleNamespace(master="master"),
                                     remote_ref=lambda branch: branch, git=git)
        ancestry = {}
        first = enact.run_delivery(repo, graph, "one", ancestry)
        second = enact.run_delivery(repo, graph, "two", ancestry)
        self.assertEqual(first["pending"], ["b", "c"])
        self.assertEqual(first["stewards"], ["repair"])
        self.assertTrue(second["complete"])
        self.assertEqual(calls, [("rev-list", "b"), ("rev-list", "a"),
                                 ("rev-list", "master")])

    def test_descendants_are_transitive(self):
        graph = [
            {"id": "e"},
            {"id": "m1", "parent": "e"},
            {"id": "t1", "parent": "m1"},
            {"id": "other"},
        ]
        found = {b["id"] for b in enact.descendants(graph, "e")}
        self.assertEqual(found, {"m1", "t1"})

    def test_spawn_order_breaks_priority_ties_by_lowest_bead_number(self):
        ready = [{"id": "ps-e8r.9"}, {"id": "ps-e8r.10"}, {"id": "ps-e8r.1"},
                 {"id": "ps-e8r.2", "priority": 1},
                 {"id": "ps-zz", "labels": ["role:steward"], "priority": 3}]
        ready.sort(key=enact.spawn_order)
        self.assertEqual([b["id"] for b in ready],
                         ["ps-zz", "ps-e8r.2", "ps-e8r.1", "ps-e8r.9", "ps-e8r.10"])

    def test_run_identity_supports_legacy_hierarchy_and_shared_repairs(self):
        graph = [{"id": "run", "issue_type": "epic"},
                 {"id": "milestone", "parent": "run", "issue_type": "epic"},
                 {"id": "work", "parent": "milestone"}]
        self.assertEqual(enact.bead_runs(graph[-1], graph), ["run"])
        repair = {"id": "repair", "metadata": {
            "enact_runs": "run peer", "enact_run": "run"}}
        self.assertEqual(enact.bead_runs(repair, graph), ["run", "peer"])
        self.assertEqual(enact.bead_runs({"id": "unrelated"}, graph), [])

    def test_holds_apply_to_nested_work_and_shared_repairs(self):
        graph = [{"id": "run", "issue_type": "epic"},
                 {"id": "gate", "parent": "run", "issue_type": "epic",
                  "metadata": {"enact_control": {"state": "paused", "epoch": "1"}}},
                 {"id": "work", "parent": "gate"}]
        with self.assertRaises(enact.Fail):
            enact.require_running(graph[-1], graph)
        graph[0]["metadata"] = {"enact_control": {"state": "blocked", "epoch": "1"}}
        with self.assertRaises(enact.Fail):
            enact.require_running({"id": "repair", "metadata": {"enact_runs": "run peer"}}, graph)

    def test_legacy_incomplete_route_cannot_hide_current_vizier(self):
        epic = {"id": "run", "issue_type": "epic"}
        metadata = {"enact_run": "run", "enact_vizier": "vizier",
                    "enact_spawn_host": enact.host_name(),
                    "enact_spawn_provider": enact.provider()}
        old = {"id": "old", "status": "closed", "metadata": metadata}
        current = {"id": "current", "metadata": {
            **metadata, "enact_spawn_app": os.environ.get("ENACT_APP", enact.provider()),
            "enact_spawn_project": "/project"}}
        routes = enact.vizier_routes(epic, [epic, old, current])
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["session"], "vizier")
        self.assertEqual(routes[0]["project"], "/project")
        self.assertFalse(routes[0]["authorized"])

    def test_role_defaults_to_executor(self):
        self.assertEqual(enact.role_of({"labels": ["role:warden"]}), "warden")
        self.assertEqual(enact.role_of({"labels": ["ui"]}), "executor")

    def test_spawn_prompt_names_role_files(self):
        prompt = enact.spawn_prompt("warden", {"id": "x-1", "title": "Review"}, "x-0", "viz")
        self.assertIn(str(enact.ROLES_DIR / "common.md"), prompt)
        self.assertIn(str(enact.ROLES_DIR / "warden.md"), prompt)
        for role in enact.SPAWNED_ROLES:
            self.assertTrue((enact.ENACT_ROOT / "skills" / "enact" / "roles"
                             / f"{role}.md").is_file(), role)

    def test_only_claude_workers_check_their_model(self):
        bead = {"id": "x-1", "title": "Work"}
        codex_settings = {"model": "gpt-6.1-sol", "effort": "high"}
        repo = types.SimpleNamespace(config=enact.Config(roles={"executor": {
            "claude": {"model": "fable"}, "codex": codex_settings}}))

        with mock.patch.object(enact, "provider", return_value="claude"):
            action = enact.spawn_action(repo, bead, "x-0", "viz")
        self.assertIn("set_session_model", action["model_required"])
        self.assertIn("must run on the `fable` model", action["prompt"])
        self.assertLess(action["prompt"].index("fable"), action["prompt"].index("enact start"))

        with mock.patch.object(enact, "provider", return_value="codex"):
            action = enact.spawn_action(repo, bead, "x-0", "viz")
        self.assertEqual(action["settings"], codex_settings)
        self.assertNotIn("model_required", action)
        self.assertNotIn("wrong model", action["prompt"])
        self.assertIn("enact start", action["prompt"])

    def test_every_session_is_told_to_end_on_a_status_title(self):
        # Users read run state from title emojis, so each instruction source
        # names the finished emoji.
        prompt = enact.spawn_prompt("executor", {"id": "x-1", "title": "Work"}, "x-0", "viz")
        for text in (prompt, enact.watch_ci_notice("abc"), enact.TITLE_NOTICE):
            self.assertIn("✅", text)
        self.assertIn(enact.EMOJI["executor"], enact.TITLE_NOTICE)
        self.assertIn("🔍", enact.TITLE_NOTICE)
        root = enact.ENACT_ROOT
        for path in ("skills/enact/SKILL.md", "skills/enact-plan/SKILL.md",
                     "skills/enact-sweep/SKILL.md",
                     "skills/enact/roles/common.md",
                     "skills/enact/roles/justiciar.md", "templates/AGENTS.md"):
            text = (root / path).read_text(encoding="utf-8")
            self.assertIn("✅", text, path)
            self.assertIn("⛔", text, path)
        # Sessions a user is present for end a turn on a question with 🔍.
        for path in ("AGENTS.md", "skills/enact/SKILL.md", "skills/enact-plan/SKILL.md",
                     "skills/enact-sweep/SKILL.md", "templates/AGENTS.md"):
            self.assertIn("🔍", (root / path).read_text(encoding="utf-8"), path)

    def test_archivist_waits_for_other_open_work_in_the_run(self):
        archivist = {"id": "d", "status": "open", "labels": ["role:archivist"]}
        docs_epic = {"id": "m", "status": "open", "issue_type": "epic"}
        done = {"id": "t1", "status": "closed"}
        self.assertFalse(enact.archivist_waits([archivist, docs_epic, done]))
        for status in ("open", "in_progress", "blocked"):
            follow_up = {"id": "t2", "status": status, "labels": ["role:mason"]}
            self.assertTrue(enact.archivist_waits([archivist, done, follow_up]), status)

    def test_spawned_here_matches_host_and_host_app(self):
        def bead(**metadata):
            return {"id": "x-3", "metadata": {"enact_spawn_host": "box", **metadata}}

        self.assertTrue(enact.spawned_here(bead(), "box", "claude"))
        self.assertTrue(enact.spawned_here(bead(enact_spawn_provider="claude"), "box", "claude"))
        self.assertFalse(enact.spawned_here(bead(enact_spawn_provider="codex"), "box", "claude"))
        app = os.environ.get("ENACT_APP", "claude")
        self.assertTrue(enact.spawned_here(bead(enact_spawn_app=app), "box", "claude"))
        self.assertFalse(enact.spawned_here(bead(enact_spawn_app=app + "-other"), "box", "claude"))
        self.assertFalse(enact.spawned_here(bead(), "other", "claude"))

    def test_labeled_beads_spawn_with_their_role(self):
        repo = types.SimpleNamespace(config=enact.Config.parse(""))
        for role in enact.SPAWNED_ROLES:
            bead = {"id": "x-2", "title": "Work", "labels": [f"role:{role}"]}
            action = enact.spawn_action(repo, bead, "x-0", "viz")
            self.assertEqual(action["role"], role)
            self.assertTrue(action["title"].startswith(enact.EMOJI[role]), role)
            self.assertIn(str(enact.ROLES_DIR / f"{role}.md"), action["prompt"])


class InstallCliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.env = {**os.environ, "HOME": str(self.home),
                    "USERPROFILE": str(self.home),
                    "CODEX_HOME": str(self.home / ".codex"),
                    "CLAUDE_CONFIG_DIR": str(self.home / ".claude")}

    def install(self, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), "install"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return json.loads(result.stdout)

    def test_fresh_codex_gets_single_canonical_skill_and_repeat_is_safe(self):
        (self.home / ".codex").mkdir()
        self.install()
        for skill in enact.SKILLS:
            target = enact.ENACT_ROOT / "skills" / skill
            self.assertEqual((self.home / ".agents/skills" / skill).resolve(), target)
        self.assertFalse((self.home / ".codex/skills").exists())
        self.assertFalse((self.home / ".claude").exists())
        self.assertEqual(set(self.install()["skills"].values()), {"present"})

    def test_owned_legacy_links_migrate_but_unrelated_entries_survive(self):
        self.env["CODEX_HOME"] = str(self.home / "custom-codex")
        legacy = self.home / "custom-codex/skills"
        for skill in enact.SKILLS:
            enact.link_dir(enact.ENACT_ROOT / "skills" / skill, legacy / skill)
        unrelated = legacy / "personal"
        unrelated.mkdir()
        (unrelated / "SKILL.md").write_text("personal skill")
        self.install()
        self.assertFalse((legacy / "enact").exists())
        self.assertFalse((legacy / "enact-setup").exists())
        self.assertEqual((unrelated / "SKILL.md").read_text(), "personal skill")
        # The same names can belong to someone else; migration must not
        # remove either a real directory or a link to another checkout.
        copied = legacy / "enact"
        copied.mkdir()
        (copied / "SKILL.md").write_text("user's enact")
        other = self.home / "other-checkout"
        other.mkdir()
        (other / "SKILL.md").write_text("other setup")
        enact.link_dir(other, legacy / "enact-setup")
        self.install()
        self.assertEqual((copied / "SKILL.md").read_text(), "user's enact")
        self.assertEqual((legacy / "enact-setup").resolve(), other.resolve())
        self.assertEqual((other / "SKILL.md").read_text(), "other setup")

    def test_conflicting_canonical_content_is_preserved(self):
        conflict = self.home / ".agents/skills/enact"
        conflict.mkdir(parents=True)
        (conflict / "SKILL.md").write_text("user content")
        self.install(success=False)
        self.assertEqual((conflict / "SKILL.md").read_text(), "user content")

    def test_shared_claude_legacy_directory_keeps_claude_access(self):
        shared = self.home / "shared"
        shared.mkdir()
        (self.home / ".claude").mkdir()
        (self.home / ".codex").mkdir()
        enact.link_dir(shared, self.home / ".claude/skills")
        enact.link_dir(shared, self.home / ".codex/skills")
        self.install()
        for skill in enact.SKILLS:
            target = enact.ENACT_ROOT / "skills" / skill
            self.assertEqual((shared / skill).resolve(), target)
            self.assertEqual((self.home / ".agents/skills" / skill).resolve(), target)


@unittest.skipIf(enact.WINDOWS, "symlinks need privileges on Windows")
class InstallTest(unittest.TestCase):
    def test_unlink_retired_removes_only_dangling_links_to_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, other = root / "skills" / "old", root / "elsewhere"
            other.mkdir()
            dangling, unrelated = root / "dangling", root / "unrelated"
            dangling.symlink_to(target, target_is_directory=True)
            unrelated.symlink_to(other, target_is_directory=True)
            self.assertTrue(enact.unlink_retired(target, dangling))
            self.assertFalse(dangling.is_symlink())
            self.assertFalse(enact.unlink_retired(target, unrelated))
            self.assertFalse(enact.unlink_retired(target, root / "missing"))
            target.mkdir(parents=True)
            live = root / "live"
            live.symlink_to(target, target_is_directory=True)
            self.assertFalse(enact.unlink_retired(target, live))


class ParallelSuiteTest(unittest.TestCase):
    def test_isolated_lanes_replay_all_test_outcomes(self):
        from tests import test_integration as integration

        barrier = threading.Barrier(4)
        fixtures = []

        class Scenario(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cls.marker = object()
                fixtures.append(cls.marker)
                barrier.wait(timeout=10)

            def test_0_success(self):
                self.assertIn(self.marker, fixtures)

            def test_1_failure(self):
                self.fail("intentional failure")

            def test_2_error(self):
                raise RuntimeError("intentional error")

            @unittest.skip("intentional skip")
            def test_3_skip(self):
                pass

            @unittest.expectedFailure
            def test_4_expected_failure(self):
                self.fail("expected")

            @unittest.expectedFailure
            def test_5_unexpected_success(self):
                pass

            def test_6_subtest(self):
                with self.subTest(value="failure"):
                    self.fail("subtest failure")

            def test_7_success(self):
                self.assertIsNotNone(self.marker)

        result = unittest.TestResult()
        with mock.patch.object(integration, "EnactTest", Scenario), \
                mock.patch.object(integration.os, "cpu_count", return_value=4):
            suite = integration.ParallelScenarios(
                unittest.defaultTestLoader.loadTestsFromTestCase(Scenario))
            self.assertEqual(suite.countTestCases(), 8)
            suite.run(result)
        self.assertEqual(len(set(fixtures)), 4)
        self.assertEqual(result.testsRun, 8)
        self.assertEqual(len(result.failures), 2)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(len(result.expectedFailures), 1)
        self.assertEqual(len(result.unexpectedSuccesses), 1)
        self.assertIn("subtest failure", result.failures[1][1] + result.failures[0][1])

    def test_fixture_failures_are_reported(self):
        from tests import test_integration as integration

        barrier = threading.Barrier(2)

        class Scenario(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                barrier.wait(timeout=10)
                raise RuntimeError("fixture failure")

            def test_first(self):
                pass

            def test_second(self):
                pass

        result = unittest.TestResult()
        with mock.patch.object(integration, "EnactTest", Scenario), \
                mock.patch.object(integration.os, "cpu_count", return_value=2):
            integration.ParallelScenarios(
                unittest.defaultTestLoader.loadTestsFromTestCase(Scenario)).run(result)
        self.assertEqual(result.testsRun, 0)
        self.assertEqual(len(result.errors), 2)
        self.assertTrue(all("fixture failure" in error for _, error in result.errors))


class CiWatchTest(unittest.TestCase):
    ROUTE = {"enact_vizier_route_abc": {"session": "v"}}

    def test_only_an_unfinished_unheld_registered_run_counts_as_a_vizier(self):
        complete = {"complete": True}
        with mock.patch.object(enact, "run_delivery", return_value=complete):
            self.assertFalse(enact.vizier_active(None, [{"id": "e", "issue_type": "epic"}]))
            self.assertTrue(enact.vizier_active(None, [
                {"id": "e", "issue_type": "epic", "metadata": self.ROUTE}]))
            self.assertFalse(enact.vizier_active(None, [
                {"id": "e", "issue_type": "epic", "status": "closed", "metadata": self.ROUTE}]))
            held = {**self.ROUTE, "enact_control": {"state": "paused", "epoch": "1"}}
            self.assertFalse(enact.vizier_active(None, [
                {"id": "e", "issue_type": "epic", "metadata": held}]))
        with mock.patch.object(enact, "run_delivery", return_value={"complete": False}):
            self.assertTrue(enact.vizier_active(None, [
                {"id": "e", "issue_type": "epic", "status": "closed", "metadata": self.ROUTE}]))

    def wait(self, graph, ancestors, status=None):
        """ancestors: (sha, ref) pairs that are ancestors."""
        repo = types.SimpleNamespace(
            config=enact.Config(), remote_ref=lambda branch: f"origin/{branch}",
            git=lambda *args: "tip",
            git_ok=lambda *args: args[0] == "merge-base" and (args[2], args[3]) in ancestors)
        beads = mock.Mock()
        beads.all.return_value = graph
        status = status or {"running": True, "last_tested": "old"}
        with mock.patch.object(enact, "sync"), mock.patch.object(enact, "fetch"), \
                mock.patch.object(enact, "Beads", return_value=beads), \
                mock.patch.object(enact, "vizier_active", return_value=False), \
                mock.patch.object(enact, "ci_status", return_value=status), \
                mock.patch.object(enact, "start_ci", return_value={"started": True}) as start, \
                mock.patch.dict(os.environ, {"ENACT_SESSION": "me"}):
            return enact.ci_wait(repo, "sha", 0, 1), start

    def steward(self, assignee=None):
        return {"id": "s", "labels": ["role:steward"], "assignee": assignee,
                "metadata": {"enact_ci_range": "green..tip", "enact_ci_os": enact.ci_os()}}

    def test_wait_reports_promotion(self):
        result, _ = self.wait([], {("sha", "origin/master")})
        self.assertEqual(result["state"], "passed")

    def test_wait_reports_a_covering_steward_unless_another_session_holds_it(self):
        covered = {("sha", "tip")}
        result, _ = self.wait([self.steward()], covered)
        self.assertEqual((result["state"], result["stewards"]), ("failed", ["s"]))
        result, _ = self.wait([self.steward("me")], covered)
        self.assertEqual(result["state"], "failed")
        result, _ = self.wait([self.steward("other")], covered)
        self.assertEqual(result["state"], "waiting")
        elsewhere = self.steward()
        elsewhere["metadata"].update(enact_ci_os="plan9", enact_ci_host="bell")
        result, _ = self.wait([elsewhere], covered)
        self.assertEqual(result["state"], "waiting")
        # The pushing session still hears that its commits broke another OS.
        self.assertEqual(result["other_os_stewards"],
                         [{"id": "s", "os": "plan9", "host": "bell", "assignee": None}])
        result, _ = self.wait([elsewhere], covered | {("sha", "origin/master")})
        self.assertEqual(result["state"], "passed")
        self.assertEqual(result["other_os_stewards"][0]["id"], "s")
        result, _ = self.wait([], covered | {("sha", "origin/master")})
        self.assertNotIn("other_os_stewards", result)
        # Already-green work is not blamed for a later failure.
        result, _ = self.wait([self.steward()], covered | {("sha", "green")})
        self.assertEqual(result["state"], "waiting")

    def superseding(self, graph, newer):
        repo = types.SimpleNamespace(
            config=enact.Config(), main=Path("."), remote_ref=lambda branch: f"origin/{branch}",
            git=lambda *args, **kwargs: newer)
        beads = mock.Mock()
        beads.all.return_value = graph
        with mock.patch.object(enact, "sync"), mock.patch.object(enact, "fetch"), \
                mock.patch.object(enact, "Beads", return_value=beads):
            return enact.superseding_repair(repo, "tip")

    def test_a_failure_older_than_a_landed_repair_is_not_filed(self):
        repair = self.steward()
        repair["metadata"]["enact_commits"] = "fix"
        self.assertEqual(self.superseding([repair], "later fix"), "s")
        # The failing tip already contains the repair, or nothing landed since.
        self.assertIsNone(self.superseding([repair], "later"))
        self.assertIsNone(self.superseding([repair], ""))
        elsewhere = self.steward()
        elsewhere["metadata"].update(enact_ci_os="plan9", enact_commits="fix")
        self.assertIsNone(self.superseding([elsewhere], "fix"))

    def test_wait_restarts_a_dead_runner_only_for_untested_staging(self):
        _, start = self.wait([], set(), {"running": False, "last_tested": "old"})
        start.assert_called_once()
        _, start = self.wait([], set(), {"running": False, "last_tested": "tip"})
        start.assert_not_called()


class StewardRoutingTest(unittest.TestCase):
    def steward(self, hours_old, **ci):
        created = enact.dt.datetime.now(enact.dt.timezone.utc) - enact.dt.timedelta(hours=hours_old)
        return {"id": "s", "labels": ["role:steward"], "created_at": created.isoformat(),
                "metadata": {f"enact_ci_{k}": v for k, v in ci.items()}}

    def targeted(self, bead, host="jarvis", os_name="macos"):
        with mock.patch.object(enact, "host_name", return_value=host), \
                mock.patch.object(enact, "ci_os", return_value=os_name), \
                mock.patch.object(enact, "provider", return_value="claude"):
            return enact.targeted_here(bead)

    def test_a_failure_goes_to_its_os_first_then_to_any_machine(self):
        fresh = self.steward(0.1, os="windows", host="cygnus")
        self.assertTrue(self.targeted(fresh, "cygnus", "windows"))
        self.assertTrue(self.targeted(fresh, "other-pc", "windows"))
        self.assertFalse(self.targeted(fresh))
        stale = self.steward(enact.STEWARD_HANDOFF_HOURS + 0.1, os="windows", host="cygnus")
        self.assertTrue(self.targeted(stale))

    def test_explicit_targets_and_legacy_stewards_keep_their_routing(self):
        self.assertTrue(self.targeted(self.steward(0)))
        pinned = self.steward(9, os="windows", host="cygnus")
        pinned["metadata"]["enact_host"] = "cygnus"
        self.assertFalse(self.targeted(pinned))
        self.assertTrue(self.targeted(pinned, "cygnus", "windows"))
        executor = {"id": "e", "metadata": {"enact_ci_os": "windows"}}
        self.assertTrue(self.targeted(executor))


class StrandedPinTest(unittest.TestCase):
    def test_a_pin_is_stranded_only_when_no_registered_vizier_serves_it(self):
        epic = {"id": "e", "metadata": {
            "enact_vizier_route_a": json.dumps({"host": "jarvis", "provider": "claude"})}}
        def pinned(**want):
            return {"id": "b", "metadata": {f"enact_{k}": v for k, v in want.items()}}
        self.assertFalse(enact.stranded(pinned(), epic))
        self.assertFalse(enact.stranded(pinned(host="jarvis"), epic))
        self.assertFalse(enact.stranded(pinned(provider="claude"), epic))
        self.assertTrue(enact.stranded(pinned(host="cygnus"), epic))
        self.assertTrue(enact.stranded(pinned(host="jarvis", provider="codex"), epic))
        self.assertTrue(enact.stranded(pinned(host="jarvis"), {"id": "e", "metadata": {}}))

class StableCheckoutTest(unittest.TestCase):
    def test_prompts_name_role_files_in_the_stable_checkout(self):
        self.assertEqual(enact.ROLES_DIR,
                         enact.stable_checkout(enact.ENACT_ROOT) / "skills" / "enact" / "roles")
        self.assertNotIn(".worktrees", enact.ROLES_DIR.parts)
        self.assertIn(str(enact.ROLES_DIR / "steward.md"), enact.watch_ci_notice("abc"))

    def test_self_update_targets_the_stable_checkout_not_a_worktree(self):
        with mock.patch.object(enact, "ENACT_ROOT", Path("/x/enact/.worktrees/feature")):
            self.assertEqual(enact.enact_checkout(), Path("/x/enact"))
        with mock.patch.object(enact, "ENACT_ROOT", Path("/x/enact")):
            self.assertEqual(enact.enact_checkout(), Path("/x/enact"))


class WaitTest(unittest.TestCase):
    def wait(self, statuses, *ids):
        beads = mock.Mock()
        beads.show.side_effect = lambda i: {"id": i, "status": statuses[i]}
        args = types.SimpleNamespace(beads=list(ids), timeout=0, interval=1)
        with mock.patch.object(enact, "find_repo"), mock.patch.object(enact, "sync"), \
                mock.patch.object(enact, "Beads", return_value=beads):
            return enact.cmd_wait(args)

    def test_wait_reports_closed_only_when_every_bead_is_closed(self):
        result = self.wait({"a": "closed", "b": "closed"}, "a", "b")
        self.assertEqual(result, {"state": "closed", "beads": ["a", "b"]})
        result = self.wait({"a": "closed", "b": "in_progress"}, "a", "b")
        self.assertEqual(result["state"], "waiting")
        self.assertEqual([b["id"] for b in result["open"]], ["b"])

class FetchTest(unittest.TestCase):
    def test_fetch_retries_ref_lock_contention_only(self):
        repo = types.SimpleNamespace(config=enact.Config(), main=Path("."))
        lost = subprocess.CompletedProcess([], 1, "", "error: cannot lock ref 'x'")
        ok = subprocess.CompletedProcess([], 0, "", "")
        other = subprocess.CompletedProcess([], 128, "", "fatal: no remote")
        with mock.patch.object(enact, "track_local_branches", return_value={}), \
                mock.patch.object(enact.time, "sleep"):
            with mock.patch.object(enact, "run", side_effect=[lost, lost, ok]) as run:
                enact.fetch(repo)
            self.assertEqual(run.call_count, 3)
            with mock.patch.object(enact, "run", side_effect=[other]) as run:
                with self.assertRaises(enact.Fail):
                    enact.fetch(repo)
            self.assertEqual(run.call_count, 1)
            with mock.patch.object(enact, "run", side_effect=[lost] * 5):
                self.assertEqual(enact.fetch(repo, check=False), {})


class JusticiarPerMachineTest(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(holder.cleanup)
        tmp = Path(holder.name)
        git = lambda *a, cwd=tmp: subprocess.run(["git", *a], cwd=cwd, check=True,
                                                  capture_output=True)
        git("init", "-q", "--bare", "remote.git")
        git("clone", "-q", str(tmp / "remote.git"), "work")
        self.work = tmp / "work"
        for key, value in (("user.name", "t"), ("user.email", "t@example.com")):
            git("config", key, value, cwd=self.work)

    def justiciar(self, host, action, actor="sess", code=None):
        args = types.SimpleNamespace(action=action, reason="r", actor=actor)
        env = {"ENACT_HOST": host}
        with mock.patch.dict(os.environ, env), contextlib.chdir(self.work):
            if code is None:
                return enact.cmd_justiciar(args)
            with self.assertRaises(enact.Fail):
                enact.cmd_justiciar(args)

    def test_each_machine_holds_its_own_lock(self):
        self.justiciar("host-a", "acquire", "sess-a")
        self.justiciar("host-a", "acquire", "sess-x", code=3)
        held = self.justiciar("host-b", "acquire", "sess-b")["acquired"]
        self.assertEqual((held["host"], held["ref"]),
                         ("host-b", "refs/enact/justiciars/host-b"))
        self.justiciar("host-a", "release", "sess-a")
        self.assertIsNone(self.justiciar("host-a", "status")["justiciar"])
        self.assertEqual(self.justiciar("host-b", "status")["justiciar"]["session"], "sess-b")

    def test_legacy_lock_binds_only_its_host(self):
        repo = enact.find_repo(self.work)
        payload = {"state": "active", "host": "host-a", "session": "old"}
        sha = enact.justiciar_commit(repo, payload)
        self.assertTrue(enact.justiciar_push(repo, sha, "", enact.LEGACY_JUSTICIAR_REF))
        self.justiciar("host-b", "acquire", "sess-b")
        self.justiciar("host-a", "acquire", "sess-a", code=3)
        self.assertEqual(self.justiciar("host-a", "release", "old")["released"]["ref"],
                         enact.LEGACY_JUSTICIAR_REF)
        self.justiciar("host-a", "acquire", "sess-a")


class OwnershipLockWaitTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.repo = enact.Repo(root, root, root / ".git", enact.Config())
        self.repo.state.mkdir(parents=True, exist_ok=True)
        self.path = self.repo.state / "ownership.lock"

    def hold(self):
        """Hold the lock the way another process would, with its holder record."""
        guard = enact.try_lock(self.path)
        self.assertIsNotNone(guard)
        self.path.with_name("ownership.lock.holder").write_text(json.dumps(
            {"pid": 4242, "command": "enact sync", "since": time.time() - 200}))
        return guard

    def test_a_waiter_that_gives_up_names_the_holder(self):
        guard = self.hold()
        self.addCleanup(enact.release_lock, self.path, guard)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(enact.Fail) as raised:
            with enact.ownership_lock(self.repo, wait=0.2):
                pass
        self.assertEqual(raised.exception.code, 3)
        for text in (str(raised.exception), stderr.getvalue()):
            self.assertIn("pid 4242 (enact sync)", text)

    def test_a_patient_waiter_outlasts_a_short_wait(self):
        guard = self.hold()
        # The holder lets go only after a short wait has already given up.
        release = threading.Timer(0.5, enact.release_lock, (self.path, guard))
        release.start()
        self.addCleanup(release.join)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(enact.Fail):
            with enact.ownership_lock(self.repo, wait=0.1):
                pass
        with enact.ownership_lock(self.repo, wait=60):
            self.assertTrue(self.path.exists())

    def test_the_holder_record_is_removed_on_release(self):
        with enact.ownership_lock(self.repo):
            self.assertIn(str(os.getpid()), self.path.with_name(
                "ownership.lock.holder").read_text())
        self.assertFalse(self.path.with_name("ownership.lock.holder").exists())


class BeadsReadTest(unittest.TestCase):
    def test_writes_sync_and_unlock_refresh_cached_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".beads").mkdir()
            (root / ".beads" / "metadata.json").write_text(json.dumps({
                "dolt_mode": "embedded", "dolt_database": "test",
            }))
            repo = enact.Repo(root, root, root / ".git", enact.Config())
            state, calls = {"title": "initial"}, []

            def command(args, cwd, **kwargs):
                calls.append(args[1:])
                if args[0] == "dolt":
                    if args[1] == "pull":
                        state["title"] = "remote"
                    out = {"config": "user.name = Test\nuser.email = t@example.com",
                           "remote": "origin", "commit": "", "pull": ""}[args[1]]
                    return subprocess.CompletedProcess(args, 0, out, "")
                if args[1] == "update":
                    state["title"] = args[-1]
                out = (json.dumps([{"id": "work", **state}])
                       if args[1] in ("show", "list") else "origin")
                return subprocess.CompletedProcess(args, 0, out, "")

            first, second = enact.Beads(repo), enact.Beads(repo)
            with mock.patch.object(enact, "run", side_effect=command), \
                    mock.patch.object(enact.shutil, "which", return_value="dolt"), \
                    mock.patch.object(enact, "dolt_json", side_effect=[
                        [{"b": "main"}], [{"local_tip": "sha", "remote_tip": "sha"}],
                    ]):
                with enact.ownership_lock(repo):
                    for reader in (first, second):
                        self.assertEqual(reader.show("work")["title"], "initial")
                        self.assertEqual(reader.all()[0]["title"], "initial")
                    first.show("work")["title"] = "unwritten"
                    self.assertEqual(second.show("work")["title"], "initial")
                    self.assertEqual(sum(call[0] == "show" for call in calls), 1)
                    self.assertEqual(sum(call[0] == "list" for call in calls), 1)
                    second.run("update", "work", "--title", "local")
                    self.assertEqual(first.show("work")["title"], "local")
                    self.assertEqual(first.all()[0]["title"], "local")
                    enact.sync(repo)
                    self.assertEqual(first.show("work")["title"], "remote")
                    self.assertEqual(first.all()[0]["title"], "remote")
                for title in ("outside", "outside again"):
                    state["title"] = title
                    self.assertEqual(first.show("work")["title"], title)
                    self.assertEqual(first.all()[0]["title"], title)


class SyncTest(unittest.TestCase):
    def test_embedded_commit_failure_cannot_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".beads").mkdir()
            (root / ".beads" / "metadata.json").write_text(json.dumps({
                "dolt_mode": "embedded", "dolt_database": "test",
            }))
            repo = enact.Repo(root, root, root / ".git", enact.Config())
            calls = []

            def command(args, cwd, **kwargs):
                calls.append(args)
                out = {"config": "user.name = Test\nuser.email = t@example.com",
                       "remote": "origin", "commit": ""}[args[1]]
                return subprocess.CompletedProcess(
                    args, 1 if args[1] == "commit" else 0, out, "disk full")

            with mock.patch.object(enact.shutil, "which", return_value="dolt"), \
                    mock.patch.object(enact, "run", side_effect=command), \
                    mock.patch.object(enact, "dolt_json", return_value=[{"b": "topic"}]):
                with self.assertRaisesRegex(enact.Fail, "disk full"):
                    enact.sync(repo)
            self.assertFalse(any(args[1] in ("pull", "push") for args in calls))

    def test_equal_published_tip_needs_no_push(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".beads").mkdir()
            (root / ".beads" / "metadata.json").write_text(json.dumps({
                "dolt_mode": "embedded", "dolt_database": "test",
            }))
            repo = enact.Repo(root, root, root / ".git", enact.Config())
            calls = []

            def command(args, cwd, **kwargs):
                calls.append(args)
                out = {"config": "user.name = Test\nuser.email = t@example.com",
                       "remote": "origin", "commit": "no changes added to commit",
                       "pull": "up to date"}[args[1]]
                return subprocess.CompletedProcess(args, args[1] == "commit", out, "")

            with mock.patch.object(enact.shutil, "which", return_value="dolt"), \
                    mock.patch.object(enact, "run", side_effect=command), \
                    mock.patch.object(enact, "dolt_json", side_effect=[
                        [{"b": "main"}], [{"local_tip": "sha", "remote_tip": "sha"}],
                    ]):
                self.assertTrue(enact.sync(repo)["remote"])
            self.assertFalse(any(args[1] == "push" for args in calls))

    def test_read_only_dolt_store_waits_for_other_processes(self):
        busy = subprocess.CompletedProcess([], 1, "", "cannot update manifest: database is read only")
        done = subprocess.CompletedProcess([], 0, "ok", "")
        with mock.patch.object(enact.time, "sleep") as sleep, \
                mock.patch.object(enact, "run", side_effect=[busy, busy, done]) as run:
            self.assertEqual(enact.dolt(["pull"], Path(".")).stdout, "ok")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        # On Windows the store cannot be opened at all while it is busy.
        locked = subprocess.CompletedProcess([], 1, "", (
            "level=warning msg=\"failed to load database with error: fslock new "
            "C:\\db\\.dolt\\noms\\LOCK: The process cannot access the file because "
            "it is being used by another process.\""))
        with mock.patch.object(enact.time, "sleep"), \
                mock.patch.object(enact, "run", side_effect=[locked, done]) as run:
            self.assertEqual(enact.dolt(["remote"], Path(".")).stdout, "ok")
        self.assertEqual(run.call_count, 2)
        # Other failures are not retried, and a store that stays busy fails.
        with mock.patch.object(enact, "run", return_value=busy) as run, \
                mock.patch.object(enact, "DOLT_BUSY_SECONDS", 0):
            with self.assertRaisesRegex(enact.Fail, "dolt pull failed.*read only"):
                enact.dolt(["pull"], Path("."))
        failed = subprocess.CompletedProcess([], 1, "", "disk full")
        with mock.patch.object(enact, "run", return_value=failed) as run:
            self.assertEqual(enact.dolt(["push"], Path("."), check=False).returncode, 1)
        self.assertEqual(run.call_count, 1)

    def test_sync_compacts_a_grown_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".beads").mkdir()
            (root / ".beads" / "metadata.json").write_text(
                '{"dolt_mode":"embedded","dolt_database":"db"}')
            noms = root / ".beads" / "embeddeddolt" / "db" / ".dolt" / "noms"
            noms.mkdir(parents=True)
            journal = noms / enact.DOLT_JOURNAL
            repo = enact.Repo(root, root, root / ".git", enact.Config())
            done = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(enact.shutil, "which", return_value="dolt"), \
                    mock.patch.object(enact, "dolt", return_value=done) as dolt:
                journal.write_bytes(b"x" * 10)
                self.assertFalse(enact.compact_beads(repo))
                with journal.open("r+b") as f:
                    f.truncate(enact.COMPACT_JOURNAL_BYTES)
                self.assertTrue(enact.compact_beads(repo))
            dolt.assert_called_once_with(["gc"], noms.parent.parent, check=False)
            (root / ".beads" / "metadata.json").write_text('{"dolt_mode":"server"}')
            self.assertFalse(enact.compact_beads(repo))

    def test_server_mode_retains_beads_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".beads").mkdir()
            (root / ".beads" / "metadata.json").write_text('{"dolt_mode":"server"}')
            repo = enact.Repo(root, root, root / ".git", enact.Config())
            beads = mock.Mock()
            beads.raw.return_value = subprocess.CompletedProcess([], 0, "origin", "")
            with mock.patch.object(enact, "Beads", return_value=beads):
                self.assertTrue(enact.sync(repo)["remote"])
            self.assertEqual(beads.raw.call_args_list, [
                mock.call("dolt", "remote", "list"),
                mock.call("dolt", "commit", "-m", "enact sync"),
                mock.call("dolt", "pull"), mock.call("dolt", "push"),
            ])


class CommandPolicyTest(unittest.TestCase):
    def setUp(self):
        from tests.policy_fixture import PolicyWorld

        self.a = PolicyWorld(enact)
        self.addCleanup(self.a.close)

    def start_worker(self, machine, bead, session):
        token = machine.show(bead)["metadata"]["enact_reservation"]
        return machine.enact("start", bead, "--reservation", token, session=session)

    def test_desktop_worker_answers_to_its_host_session_id(self):
        # Claude Desktop's start_session returns the host id; the CLI id differs.
        epic = self.a.create("desktop ids", type="epic")
        bead = self.a.create("desktop child", parent=epic)
        token = self.a.enact("next", "--epic", epic)["reservation"]
        self.a.enact("meta", bead, "enact_session=local_host", "--reservation", token)
        desktop = {"ENACT_SESSION": "", "CLAUDE_CODE_SESSION_ID": "cli-uuid",
                   "CLAUDE_CODE_HOST_SESSION_ID": "local_host"}
        other = {**desktop, "CLAUDE_CODE_HOST_SESSION_ID": "local_other"}
        self.a.enact("start", bead, "--reservation", token, env=other, code=3)
        self.a.enact("start", bead, "--reservation", token, env=desktop)
        saved = self.a.show(bead)
        self.assertEqual(saved["assignee"], "local_host")
        self.assertEqual(saved["metadata"]["enact_session"], "local_host")
        self.a.enact("close", bead, "--reason", "done", env=other, code=3)
        self.a.enact("close", bead, "--reason", "done", env=desktop)

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
                     "unsupported lookup", session="observer", code=3)
        self.assertFalse(self.a.enact("status")["working"][0]["routing_known"])
        self.a.bd("update", bead, "--set-metadata", "enact_spawn_provider=claude")
        # A provider alone does not prove which app/project can inspect it.
        self.a.enact("release", bead, "--reason", "unknown app", "--expected-owner", "legacy",
                     "--expected-session", "legacy", "--inactive", "missing", "--evidence",
                     "unsupported lookup", session="observer", code=3)
        self.a.enact("close", bead, "--reason", "legacy completed", session="legacy")

    def test_codex_vizier_starts_and_archives_fresh_chats_even_with_pool_state(self):
        epic = self.a.create("epic", type="epic")
        first = self.a.create("first", parent=epic, priority=1)
        second = self.a.create("second", parent=epic, labels="role:warden")
        self.a.bd("dep", "add", second, first)
        self.a.enact("pool", "add", "exec-1", "unused-member")

        def codex(*args, thread="sess-a"):
            return self.a.enact(*args, env={"ENACT_PROVIDER": "", "ENACT_APP": "codex",
                                          "ENACT_SESSION": "", "CODEX_THREAD_ID": thread})

        step = codex("next", "--epic", epic)
        self.assertEqual((step["action"], step["bead"], step["role"]), ("spawn", first, "executor"))
        self.assertIn("executor.md", step["prompt"])
        self.assertIn("foreground", step["reminder"])
        self.assertNotIn("pool_notice", step)
        self.assertNotIn("session", step)
        codex("meta", first, "enact_session=exec-1", "--reservation", step["reservation"])
        codex("start", first, "--reservation", step["reservation"], thread="exec-1")
        codex("close", first, "--reason", "done", thread="exec-1")
        step = codex("next", "--epic", epic)
        self.assertEqual(step["archive"], [{"bead": first, "session": "exec-1"}])
        self.assertEqual((step["action"], step["bead"], step["role"]), ("spawn", second, "warden"))
        codex("start", second, "--reservation", step["reservation"], thread="exec-2")
        codex("close", second, "--reason", "done", thread="exec-2")
        self.assertEqual(self.a.show(epic)["status"], "closed")
        step = codex("next", "--epic", epic)
        self.assertEqual(step["action"], "done")
        self.assertNotIn("pool_archive", step)

    def test_one_session_works_a_lane_and_is_archived_only_after_it(self):
        epic = self.a.create("lanes", type="epic")
        first = self.a.create("first", parent=epic, priority=1, labels="lane:cli,tier:light")
        second = self.a.create("second", parent=epic, priority=2, labels="lane:cli")
        other = self.a.create("other", parent=epic, priority=3)
        self.a.repo = self.a.repo.__class__(
            self.a.path, self.a.path, self.a.path / ".git",
            enact.Config(tiers={"light": {"claude": {"model": "sonnet"}}}))
        with mock.patch.object(enact, "find_repo", return_value=self.a.repo):
            step = self.a.enact("next", "--epic", epic, "--parallel", "3")
        self.assertEqual((step["bead"], step["settings"]), (first, {"model": "sonnet"}))
        self.assertIn("set_session_model", step["model_required"])
        self.assertIn("must run on the `sonnet` model", step["prompt"])
        self.a.enact("meta", first, "enact_session=worker", "--reservation", step["reservation"])
        self.start_worker(self.a, first, "worker")
        # The rest of the lane waits for the session that holds it.
        step = self.a.enact("next", "--epic", epic, "--parallel", "3")
        self.assertEqual(step["bead"], other)
        done = self.a.enact("close", first, "--reason", "done", "--continue", session="worker")
        self.assertEqual(done["next"]["id"], second)
        saved = self.a.show(second)
        self.assertEqual((saved["assignee"], saved["metadata"]["enact_session"]),
                         ("worker", "worker"))
        self.assertEqual(saved["metadata"]["enact_vizier"], "sess-a")
        step = self.a.enact("next", "--epic", epic, "--parallel", "3")
        self.assertEqual((step["action"], step["archive"]), ("wait", []))
        self.assertEqual(sorted(step["inflight"]), [second, other])
        last = self.a.enact("close", second, "--reason", "done", "--continue", session="worker")
        self.assertIsNone(last["next"])
        step = self.a.enact("next", "--epic", epic, "--parallel", "3")
        self.assertEqual(step["archive"], [{"bead": first, "session": "worker"},
                                           {"bead": second, "session": "worker"}])

    def test_pool_vizier_reuses_members_instead_of_starting_sessions(self):
        # Claude Desktop caps a session at ten starts, all in user-started
        # turns, so a pool vizier hands beads to idle members it started.
        epic = self.a.create("pool", type="epic")
        beads = [self.a.create(f"b{i}", parent=epic, priority=i) for i in range(3)]
        self.a.enact("pool", "add", *[f"local_{i}" for i in range(11)], code=3)
        pool = self.a.enact("pool", "add", "local_a", "local_b", "local_c")
        self.assertEqual((pool["started"], pool["idle"]), (3, ["local_a", "local_b", "local_c"]))
        self.a.enact("pool", "add", *[f"local_{i}" for i in range(8)], code=3)

        def desktop(session):
            return {"ENACT_SESSION": "", "CLAUDE_CODE_SESSION_ID": f"cli-{session}",
                    "CLAUDE_CODE_HOST_SESSION_ID": session}

        step = self.a.enact("next", "--epic", epic, "--parallel", "2")
        self.assertEqual((step["action"], step["bead"], step["session"]),
                         ("spawn", beads[0], "local_a"))
        self.assertIn("never call start_session", step["pool_notice"])
        self.assertIn("background", step["reminder"])
        self.a.enact("meta", beads[0], "enact_session=local_a", "--reservation", step["reservation"])
        self.a.enact("start", beads[0], "--reservation", step["reservation"], env=desktop("local_a"))
        # A reused member answers at once and may start before the vizier records it.
        step = self.a.enact("next", "--epic", epic, "--parallel", "2")
        self.assertEqual((step["bead"], step["session"]), (beads[1], "local_b"))
        self.a.enact("start", beads[1], "--reservation", step["reservation"],
                     env=desktop("local_c"), code=3)
        self.a.enact("start", beads[1], "--reservation", step["reservation"], env=desktop("local_b"))
        self.a.enact("meta", beads[1], "enact_session=local_b", "--reservation", step["reservation"])
        self.assertEqual(self.a.show(beads[1])["assignee"], "local_b")
        self.assertEqual(self.a.enact("next", "--epic", epic, "--parallel", "2")["reason"],
                         "capacity")
        self.a.enact("close", beads[0], "--reason", "done", env=desktop("local_a"))
        # Members rotate, finished members stay unarchived, and a refusal rests one.
        self.a.enact("pool", "rest", "local_c")
        step = self.a.enact("next", "--epic", epic, "--parallel", "2")
        self.assertEqual((step["bead"], step["session"], step["archive"]),
                         (beads[2], "local_a", []))
        self.a.enact("release", beads[2], "--reservation", step["reservation"],
                     "--reason", "clear refused")
        self.assertNotIn("enact_pool_session", self.a.show(beads[2])["metadata"])
        self.a.enact("pool", "rest", "local_a")
        self.assertEqual(self.a.enact("next", "--epic", epic, "--parallel", "2")["reason"],
                         "pool busy")
        self.a.enact("pool", "drop", "local_a", "local_c")
        self.a.enact("close", beads[1], "--reason", "done", env=desktop("local_b"))
        step = self.a.enact("next", "--epic", epic, "--parallel", "2")
        self.assertEqual((step["bead"], step["session"]), (beads[2], "local_b"))
        self.a.enact("start", beads[2], "--reservation", step["reservation"], env=desktop("local_b"))
        self.a.enact("close", beads[2], "--reason", "done", env=desktop("local_b"))
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual(step["action"], "done")
        self.assertEqual(sorted(i["bead"] for i in step["archive"]), sorted(beads))
        self.assertEqual(step["pool_archive"], [])

    def test_lost_pool_and_justiciar_use_members(self):
        epic = self.a.create("pool", type="epic")
        self.a.create("work", parent=epic)
        self.a.enact("pool", "add", "local_a")
        with mock.patch.object(enact, "justiciar_state",
                               return_value={"state": "requested", "host": "host-a",
                                             "reason": "deadlock", "sha": "x"}), \
                mock.patch.object(enact, "justiciar_transition", return_value=True) as move:
            step = self.a.enact("next", "--epic", epic)
        self.assertEqual((step["action"], step["session"]), ("spawn-justiciar", "local_a"))
        self.assertEqual(move.call_args.args[2]["pool_session"], "local_a")
        # The Justiciar holds its member until it releases the lock.
        with mock.patch.object(enact, "justiciar_state",
                               return_value={"state": "active", "pool_session": "local_a"}):
            self.assertEqual(self.a.enact("next", "--epic", epic)["reason"], "pool busy")
        # A pool vizier never falls back to starting sessions.
        self.a.enact("pool", "drop", "--all")
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual((step["action"], step["started"]), ("pool-empty", 1))
        self.assertEqual(self.a.enact("next", "--epic", epic, session="other-vizier")["action"],
                         "spawn")

    def test_brief_gathers_epics_dependency_handoffs_and_lane(self):
        epic = self.a.create("run", type="epic", description="code map: cli.py")
        done = self.a.create("done", parent=epic, labels="lane:cli")
        work = self.a.create("work", parent=epic, labels="lane:cli",
                             acceptance_criteria="it works")
        self.a.bd("dep", "add", work, done)
        self.a.graph[done].update(status="closed", close_reason="shipped")
        self.a.bd("comments", "add", done, "Handoff: touched cli.py; beware X")
        self.a.bd("comments", "add", done, "unrelated later note")
        self.a.bd("comments", "add", work, "Warden lead: check Y")
        brief = self.a.enact("brief", work)
        self.assertEqual(brief["bead"]["acceptance_criteria"], "it works")
        self.assertEqual(brief["bead"]["comments"], ["Warden lead: check Y"])
        self.assertEqual([e["description"] for e in brief["epics"]], ["code map: cli.py"])
        [dep] = brief["dependencies"]
        self.assertEqual((dep["id"], dep["close_reason"], dep["handoff"]),
                         (done, "shipped", "Handoff: touched cli.py; beware X"))
        self.assertEqual([b["id"] for b in brief["lane"]], [done])

    def test_run_holds_and_explicit_resume(self):
        epic = self.a.create("acceptance needs new access", type="epic",
                             acceptance_criteria="Required access is available")
        task = self.a.create("implementation", parent=epic, priority=1)
        other = self.a.create("later", parent=epic)
        route = self.a.enact("run", "register", epic, "--authorized", "--parallel", "2")
        self.assertEqual(route["route"]["parallel"], 2)
        step = self.a.enact("next", "--epic", epic)
        self.assertEqual(step["bead"], task)
        self.start_worker(self.a, task, "worker")
        self.a.enact("run", "pause", epic, "--reason", "user stop", env={"ENACT_HOST": "host-b"})
        self.a.enact("release", task, "--expected-owner", "worker",
                     "--expected-session", "worker", "--inactive", "stopped",
                     "--evidence", "successful lookup: stopped", "--reason", "recovery",
                     session="observer", code=3)
        self.a.enact("claim", other, code=3)
        self.a.enact("start", task, "--reservation", step["reservation"],
                     session="worker", code=3)
        status = self.a.enact("status")
        self.assertFalse(status["active_runs"][0]["recoverable"])
        self.assertEqual(self.a.enact("next", "--epic", epic)["action"], "paused")
        self.a.enact("meta", epic, 'enact_control={"state":"active"}', code=3)
        self.a.enact("run", "resume", epic, "--reason", "automatic retry", code=3)
        self.a.enact("run", "resume", epic, "--explicit", "--reason", "user says resume")
        self.a.enact("close", task, "--reason", "finished", session="worker")
        # Exhausted Justiciar recovery blocks the whole run until explicit resume.
        self.a.enact("run", "block", epic, "--exhausted", "--reason", "Justiciar: access unavailable")
        self.a.enact("claim", other, code=3)
        self.assertEqual(self.a.enact("next", "--epic", epic)["action"], "blocked")
        self.a.bd("close", other, "--reason", "finished elsewhere")
        self.assertEqual(self.a.enact("next", "--epic", epic)["action"], "blocked")
        self.assertNotEqual(self.a.show(epic)["status"], "closed")
        self.a.enact("run", "resume", epic, "--explicit", "--reason", "user supplied access")
        self.assertEqual(self.a.enact("next", "--epic", epic)["action"], "done")

    def test_handoff_release_needs_evidence_and_fences_stale_workers(self):
        epic = self.a.create("local recovery", type="epic")
        bead = self.a.create("unfinished", parent=epic)
        self.a.enact("next", "--epic", epic)
        old_token = self.a.show(bead)["metadata"]["enact_reservation"]
        self.start_worker(self.a, bead, "old")
        # Lookup failure, a stale observation, and a different provider cannot
        # establish inactivity. None may discard this worker's assignment.
        base = ["release", bead, "--reason", "recovery", "--expected-owner", "old",
                "--expected-session", "old"]
        self.a.enact(*base, session="observer", code=3)
        self.a.enact(*base, "--inactive", "stopped", session="observer", code=3)
        self.a.enact(*base, "--inactive", "stopped", "--evidence", "read confirmed idle",
                     "--expected-owner", "stale", session="observer", code=3)
        self.a.enact(*base, "--inactive", "stopped", "--evidence", "wrong provider",
                     session="observer", env={"ENACT_PROVIDER": "codex"}, code=3)
        self.a.enact(*base, "--inactive", "stopped", "--evidence", "read confirmed idle",
                     session="observer")
        self.a.enact("start", bead, "--reservation", old_token, session="old", code=3)
        self.a.enact("next", "--epic", epic, session="replacement-vizier")
        self.a.enact("start", bead, "--reservation", old_token, session="old", code=3)
        self.start_worker(self.a, bead, "new")
        for args in (("close", bead, "--reason", "stale"),
                     ("meta", bead, "enact_commits=stale"),
                     ("release", bead, "--reason", "stale")):
            self.a.enact(*args, session="old", code=3)
        self.assertEqual(self.a.show(bead)["assignee"], "new")


if __name__ == "__main__":
    unittest.main()
