"""Fast tests: helper behavior that needs no git or Beads."""

import importlib.util
import multiprocessing
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

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
        self.assertFalse(enact.spawned_here(bead(), "other", "claude"))

    def test_labeled_beads_spawn_with_their_role(self):
        repo = types.SimpleNamespace(config=enact.Config.parse(""))
        for role in enact.SPAWNED_ROLES:
            bead = {"id": "x-2", "title": "Work", "labels": [f"role:{role}"]}
            action = enact.spawn_action(repo, bead, "x-0", "viz")
            self.assertEqual(action["role"], role)
            self.assertTrue(action["title"].startswith(enact.EMOJI[role]), role)
            self.assertIn(str(enact.ROLES_DIR / f"{role}.md"), action["prompt"])


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


if __name__ == "__main__":
    unittest.main()
