import json
import tempfile
import unittest
from pathlib import Path

from arteries import setup_cli


class SetupCliTests(unittest.TestCase):
    def test_generic_install_check_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(setup_cli.main(["generic", "--cwd", str(root), "--project", "demo"]), 0)
            self.assertTrue((root / ".arteries/hooks/observe.sh").exists())
            config = json.loads((root / ".arteries/config.json").read_text())
            self.assertEqual(config["project"], "demo")
            self.assertEqual(config["agent_id"], "demo-hook")
            self.assertEqual(config["cli"], "generic")
            observe = (root / ".arteries/hooks/observe.sh").read_text(encoding="utf-8")
            activate = (root / ".arteries/hooks/activate.sh").read_text(encoding="utf-8")
            self.assertIn("ARTERIES_CLI", observe)
            self.assertIn("ARTERIES_REPO", observe)
            self.assertIn("arteries.runs start", activate)
            self.assertEqual(setup_cli.main(["generic", "--cwd", str(root), "--check"]), 0)
            self.assertEqual(setup_cli.main(["generic", "--cwd", str(root), "--remove"]), 0)
            self.assertFalse((root / ".arteries").exists())

    def test_claude_install_check_remove_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / ".claude/settings.local.json"
            settings.parent.mkdir()
            settings.write_text(json.dumps({"permissions": {"allow": ["Bash(*)"]}}) + "\n", encoding="utf-8")

            self.assertEqual(setup_cli.main(["claude", "--cwd", str(root)]), 0)
            data = json.loads(settings.read_text())
            self.assertEqual(data["permissions"]["allow"], ["Bash(*)"])
            self.assertIn("SessionStart", data["hooks"])
            self.assertIn("UserPromptSubmit", data["hooks"])
            config = json.loads((root / ".arteries/config.json").read_text())
            self.assertEqual(config["cli"], "claude")
            self.assertEqual(setup_cli.main(["claude", "--cwd", str(root), "--check"]), 0)
            self.assertEqual(setup_cli.main(["claude", "--cwd", str(root), "--remove"]), 0)
            data = json.loads(settings.read_text())
            self.assertNotIn("hooks", data)
            self.assertEqual(data["permissions"]["allow"], ["Bash(*)"])

    def test_codex_install_is_marker_managed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("# Existing\n", encoding="utf-8")

            self.assertEqual(setup_cli.main(["codex", "--cwd", str(root)]), 0)
            agents = (root / "AGENTS.md").read_text()
            self.assertIn(setup_cli.MARKER_START, agents)
            self.assertIn("# Existing", agents)
            self.assertIn(setup_cli.CODEX_MARKER_START, (root / ".codex/config.toml").read_text())
            config = json.loads((root / ".arteries/config.json").read_text())
            self.assertEqual(config["cli"], "codex")
            self.assertEqual(setup_cli.main(["codex", "--cwd", str(root), "--check"]), 0)
            self.assertEqual(setup_cli.main(["codex", "--cwd", str(root), "--remove"]), 0)
            self.assertNotIn(setup_cli.MARKER_START, (root / "AGENTS.md").read_text())
            self.assertFalse((root / ".arteries").exists())


if __name__ == "__main__":
    unittest.main()
