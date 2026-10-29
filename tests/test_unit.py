"""Fast tests: pure logic in enact.py that needs no git or Beads."""

import importlib.util
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

    def test_role_defaults_to_executor(self):
        self.assertEqual(enact.role_of({"labels": ["role:warden"]}), "warden")
        self.assertEqual(enact.role_of({"labels": ["ui"]}), "executor")

    def test_spawn_prompt_names_role_files(self):
        prompt = enact.spawn_prompt("warden", {"id": "x-1", "title": "Review"}, "x-0", "viz")
        self.assertIn(str(enact.ROLES_DIR / "common.md"), prompt)
        self.assertIn(str(enact.ROLES_DIR / "warden.md"), prompt)
        for role in enact.SPAWNED_ROLES:
            self.assertTrue((enact.ROLES_DIR / f"{role}.md").is_file(), role)

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
