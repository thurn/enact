"""Fast tests: helper behavior that needs no git or Beads."""

import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
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
            """
        )
        self.assertEqual(cfg.master, "main")
        self.assertEqual(cfg.staging, "staging")
        self.assertEqual((cfg.fast, cfg.slow), ("make fast", "make slow"))
        self.assertEqual(cfg.worktree_create, "hook create")
        self.assertIsNone(cfg.worktree_remove)
        self.assertEqual(cfg.role_settings("warden", "claude"), {"model": "opus", "effort": "max"})
        self.assertEqual(cfg.role_settings("warden", "codex"), {})


class GraphTest(unittest.TestCase):
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

    def test_recovery_is_bounded_and_only_explicit_epoch_change_resets(self):
        history = [{"epoch": "old", "at": "2020-01-01T00:00:00+00:00"}]
        self.assertEqual(enact.recovery_budget(history, "old", 2,
                                              "2020-01-01T00:59:59+00:00"), "cooldown")
        self.assertIsNone(enact.recovery_budget(history, "old", 2,
                                                "2020-01-01T01:00:00+00:00"))
        history.append({"epoch": "old", "at": "2020-01-01T01:00:00+00:00"})
        self.assertEqual(enact.recovery_budget(history, "old", 2,
                                              "2030-01-01T00:00:00+00:00"), "exhausted")
        self.assertIsNone(enact.recovery_budget(history, "explicit-resume", 2,
                                                "2030-01-01T00:00:00+00:00"))

        for limit in (1, 2, 3):
            with self.subTest(limit=limit):
                for count in range(limit + 1):
                    history = [{"epoch": "old", "at": "2020-01-01T00:00:00+00:00"}
                               for _ in range(count)]
                    self.assertEqual(enact.recovery_budget(
                        history, "old", limit, "2030-01-01T00:00:00+00:00"),
                        "exhausted" if count == limit else None)

    def test_marshal_names_distinguish_equal_project_basenames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = types.SimpleNamespace(main=root / "one" / "project")
            two = types.SimpleNamespace(main=root / "two" / "project")
            self.assertEqual(enact.marshal_name(one), enact.marshal_name(one))
            self.assertNotEqual(enact.marshal_name(one), enact.marshal_name(two))

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
            self.assertTrue((enact.ROLES_DIR / f"{role}.md").is_file(), role)

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
        for skill in ("enact", "enact-setup"):
            target = enact.ENACT_ROOT / "skills" / skill
            self.assertEqual((self.home / ".agents/skills" / skill).resolve(), target)
        self.assertFalse((self.home / ".codex/skills").exists())
        self.assertFalse((self.home / ".claude").exists())
        self.assertEqual(set(self.install()["skills"].values()), {"present"})

    def test_owned_legacy_links_migrate_but_unrelated_entries_survive(self):
        self.env["CODEX_HOME"] = str(self.home / "custom-codex")
        legacy = self.home / "custom-codex/skills"
        for skill in ("enact", "enact-setup"):
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
        for skill in ("enact", "enact-setup"):
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
                     "unsupported lookup", session="marshal", code=3)
        self.assertFalse(self.a.enact("status")["working"][0]["routing_known"])
        self.a.bd("update", bead, "--set-metadata", "enact_spawn_provider=claude")
        # A provider alone does not prove which app/project can inspect it.
        self.a.enact("release", bead, "--reason", "unknown app", "--expected-owner", "legacy",
                     "--expected-session", "legacy", "--inactive", "missing", "--evidence",
                     "unsupported lookup", session="marshal", code=3)
        self.a.enact("close", bead, "--reason", "legacy completed", session="legacy")

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


if __name__ == "__main__":
    unittest.main()
