import json
import re
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from arteries import setup_cli


class SetupCliTests(unittest.TestCase):
    def test_list_supported_providers(self):
        self.assertEqual(setup_cli.PROVIDERS, ("generic", "pi", "codex", "claude", "opencode", "hermes", "cursor"))

    def test_pi_install_check_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(setup_cli.main(["pi", "--cwd", str(root), "--project", "demo"]), 0)
            self.assertTrue((root / ".arteries/hooks/observe.sh").exists())
            self.assertTrue((root / ".arteries/hooks/compact-packet.sh").exists())
            self.assertTrue((root / ".arteries/hooks/hook-observe.sh").exists())
            self.assertTrue((root / ".arteries/hooks/hook-event.sh").exists())
            self.assertTrue((root / ".arteries/hooks/assistant-observe.sh").exists())
            self.assertTrue((root / ".arteries/hooks/hook-assistant-observe.sh").exists())
            self.assertTrue((root / ".arteries/hooks/hook-compact-packet.sh").exists())
            self.assertTrue((root / ".arteries/hooks/pi-compact-json.sh").exists())
            self.assertTrue((root / ".pi/extensions/arteries.ts").exists())
            config = json.loads((root / ".arteries/config.json").read_text())
            self.assertEqual(config["project"], "demo")
            self.assertEqual(config["agent_id"], "demo-hook")
            self.assertEqual(config["cli"], "pi")
            extension = (root / ".pi/extensions/arteries.ts").read_text(encoding="utf-8")
            self.assertIn("session_before_compact", extension)
            self.assertIn("pi-compact-json.sh", extension)
            self.assertIn("hook-assistant-observe.sh", extension)
            hook_assistant = (root / ".arteries/hooks/hook-assistant-observe.sh").read_text(encoding="utf-8")
            self.assertIn("python3 -m arteries.assistant --stdin-json", hook_assistant)
            self.assertNotIn("python3 -m arteries.cli_normalize", hook_assistant)
            self.assertEqual(setup_cli.main(["pi", "--cwd", str(root), "--check"]), 0)
            self.assertEqual(setup_cli.main(["pi", "--cwd", str(root), "--remove"]), 0)
            self.assertFalse((root / ".arteries").exists())
            self.assertFalse((root / ".pi/extensions/arteries.ts").exists())

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
            self.assertIn("PreCompact", data["hooks"])
            self.assertIn("PostCompact", data["hooks"])
            self.assertNotIn("Stop", data["hooks"])
            self.assertIn("SubagentStart", data["hooks"])
            self.assertIn("SubagentStop", data["hooks"])
            self.assertIn("hook-observe.sh", data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"])
            hook_observe = (root / ".arteries/hooks/hook-observe.sh").read_text(encoding="utf-8")
            self.assertIn("python3 -m arteries.hook_observe", hook_observe)
            self.assertNotIn("python3 -m arteries.cli_normalize", hook_observe)
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
            config_toml = (root / ".codex/config.toml").read_text()
            self.assertIn(setup_cli.MARKER_START, agents)
            self.assertIn("# Existing", agents)
            self.assertIn(setup_cli.CODEX_MARKER_START, config_toml)
            parsed_config = tomllib.loads(config_toml)
            self.assertEqual(
                parsed_config["experimental_compact_prompt_file"],
                "../.arteries/codex/compact_prompt.txt",
            )
            self.assertTrue(parsed_config["features"]["hooks"])
            self.assertTrue(all(isinstance(value, bool) for value in parsed_config["features"].values()))
            self.assertIn("hooks.PreCompact", config_toml)
            self.assertIn("hooks.SubagentStart", config_toml)
            self.assertIn("hook-compact-packet.sh", config_toml)
            self.assertIn("hook-assistant-observe.sh", agents)
            compact_path = root / ".codex" / parsed_config["experimental_compact_prompt_file"]
            self.assertTrue(compact_path.resolve().exists())
            config = json.loads((root / ".arteries/config.json").read_text())
            self.assertEqual(config["cli"], "codex")
            self.assertEqual(setup_cli.main(["codex", "--cwd", str(root), "--check"]), 0)
            self.assertEqual(setup_cli.main(["codex", "--cwd", str(root), "--remove"]), 0)
            self.assertNotIn(setup_cli.MARKER_START, (root / "AGENTS.md").read_text())
            self.assertFalse((root / ".arteries").exists())


    def test_codex_install_merges_existing_features_and_removes_legacy_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / ".codex/config.toml"
            config.parent.mkdir(parents=True)
            config.write_text(
                """[features]
memories = true
codex_hooks = true

# arteries:start - managed by `python3 -m arteries.setup_cli codex`
[features]
codex_hooks = true
# arteries:end
""",
                encoding="utf-8",
            )

            self.assertEqual(setup_cli.main(["codex", "--cwd", str(root)]), 0)
            config_toml = config.read_text(encoding="utf-8")
            parsed_config = tomllib.loads(config_toml)

            self.assertNotIn("python3 -m arteries.setup_cli", config_toml)
            self.assertEqual(config_toml.count("[features]"), 1)
            self.assertTrue(parsed_config["features"]["memories"])
            self.assertTrue(parsed_config["features"]["hooks"])
            self.assertNotIn("codex_hooks", parsed_config["features"])
            self.assertEqual(
                parsed_config["experimental_compact_prompt_file"],
                "../.arteries/codex/compact_prompt.txt",
            )

    def test_opencode_install_check_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(setup_cli.main(["add", "opencode", "--cwd", str(root), "--project", "demo"]), 0)
            plugin = root / ".opencode/plugins/arteries.ts"
            self.assertTrue(plugin.exists())
            plugin_text = plugin.read_text(encoding="utf-8")
            self.assertIn("experimental.session.compacting", plugin_text)
            self.assertIn("hook-assistant-observe.sh", plugin_text)
            self.assertEqual(setup_cli.main(["check", "opencode", "--cwd", str(root)]), 0)
            self.assertEqual(setup_cli.main(["remove", "opencode", "--cwd", str(root)]), 0)
            self.assertFalse(plugin.exists())
            self.assertFalse((root / ".arteries").exists())

    def test_cursor_install_check_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(setup_cli.main(["cursor", "--cwd", str(root), "--project", "demo"]), 0)
            rule = root / ".cursor/rules/arteries.mdc"
            mcp = root / ".cursor/mcp.json"
            self.assertTrue(rule.exists())
            self.assertTrue(mcp.exists())
            rule_text = rule.read_text(encoding="utf-8")
            self.assertIn("generic-observe.sh", rule_text)
            self.assertIn("assistant-observe.sh", rule_text)
            self.assertIn("capillaries", json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"])
            self.assertEqual(setup_cli.main(["cursor", "--cwd", str(root), "--check"]), 0)
            self.assertEqual(setup_cli.main(["cursor", "--cwd", str(root), "--remove"]), 0)
            self.assertFalse(rule.exists())
            self.assertFalse((root / ".arteries").exists())

    def test_hermes_install_check_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(setup_cli.main(["hermes", "--cwd", str(root), "--project", "demo"]), 0)
            doc = root / "HERMES.md"
            mcp = root / ".hermes/mcp.json"
            self.assertTrue(doc.exists())
            doc_text = doc.read_text(encoding="utf-8")
            self.assertIn(setup_cli.HERMES_MARKER_START, doc_text)
            self.assertIn("assistant-observe.sh", doc_text)
            self.assertIn("capillaries", json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"])
            self.assertEqual(setup_cli.main(["hermes", "--cwd", str(root), "--check"]), 0)
            self.assertEqual(setup_cli.main(["hermes", "--cwd", str(root), "--remove"]), 0)
            self.assertNotIn(setup_cli.HERMES_MARKER_START, doc.read_text(encoding="utf-8"))
            self.assertFalse((root / ".arteries").exists())

    def test_multiple_adapters_are_additive_and_remove_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(setup_cli.main(["add", "codex", "--cwd", str(root), "--project", "demo"]), 0)
            self.assertEqual(setup_cli.main(["add", "cursor", "--cwd", str(root), "--project", "demo"]), 0)
            config = json.loads((root / ".arteries/config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["installed_clis"], ["codex", "cursor"])

            self.assertEqual(setup_cli.main(["remove", "cursor", "--cwd", str(root)]), 0)
            self.assertTrue((root / ".arteries").exists())
            self.assertTrue((root / ".codex/config.toml").exists())
            self.assertFalse((root / ".cursor/rules/arteries.mdc").exists())

            self.assertEqual(setup_cli.main(["remove", "codex", "--cwd", str(root)]), 0)
            self.assertFalse((root / ".arteries").exists())

    def test_setup_defaults_to_art_wrapper_caller_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"ARTERIES_CALLER_CWD": str(root)}):
                self.assertEqual(setup_cli.main(["pi"]), 0)

            config = json.loads((root / ".arteries/config.json").read_text())
            self.assertEqual(config["project"], root.name)
            self.assertEqual(config["cli"], "pi")

    def test_setup_accepts_capillaries_root_for_supported_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "target"
            cap = Path(tmp) / "capillaries"
            self.assertEqual(setup_cli.main([
                "pi",
                "--cwd", str(root),
                "--project", "demo",
                "--capillaries-root", str(cap),
            ]), 0)

            config = json.loads((root / ".arteries/config.json").read_text())
            self.assertEqual(config["cli"], "pi")
            self.assertEqual(config["capillaries_root"], str(cap.resolve()))
            observe = (root / ".arteries/hooks/observe.sh").read_text(encoding="utf-8")
            self.assertIn("CAPILLARIES_ROOT", observe)
            self.assertIn("$CAPILLARIES_ROOT/src", observe)


if __name__ == "__main__":
    unittest.main()


class GeneratedAdapterTests(unittest.TestCase):
    """Every adapter file an install writes must be runnable as written.

    Two classes of bug live here, and both are silent — the adapter installs,
    `--check` passes, and nothing is ever observed:
      1. an unsubstituted `{hooks}` placeholder (the JS/TS bodies cannot be
         f-strings, so the .replace() is easy to forget), which makes every
         exec target a literal path named "{hooks}"
      2. a hook path written relative, which resolves against whatever cwd the
         host hands the hook
    """

    ADAPTER_FILES = {
        "pi": [".pi/extensions/arteries.ts"],
        "opencode": [".opencode/plugins/arteries.ts"],
        "cursor": [".cursor/rules/arteries.mdc"],
        "codex": ["AGENTS.md", ".codex/config.toml"],
        "hermes": ["HERMES.md"],
        "claude": [".claude/settings.local.json"],
    }

    def test_no_unsubstituted_placeholders_and_paths_are_absolute(self):
        for provider, relatives in self.ADAPTER_FILES.items():
            for relative in relatives:
                with self.subTest(provider=provider, file=relative), \
                        tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.assertEqual(
                        setup_cli.main(
                            [provider, "--cwd", str(root), "--project", "demo"]), 0)
                    text = (root / relative).read_text(encoding="utf-8")
                    self.assertNotIn("{hooks}", text,
                                     f"{provider} ships a literal placeholder")
                    absolute = str(root / ".arteries" / "hooks")
                    self.assertIn(absolute, text)
                    # blank the absolute references; anything still naming the
                    # hooks dir is doing it relative to an unknown cwd
                    self.assertNotIn(".arteries/hooks", text.replace(absolute, ""),
                                     f"{provider} references hooks by relative path")


class PiExtensionTests(unittest.TestCase):
    def _extension(self) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            setup_cli.main(["pi", "--cwd", str(root), "--project", "demo"])
            return (root / ".pi/extensions/arteries.ts").read_text(encoding="utf-8")

    def test_registers_only_events_pi_actually_emits(self):
        """The adapter once listened for "message_updated" and
        "assistant_response". Pi emits neither, so it observed nothing for its
        entire life while installing and checking clean."""
        text = self._extension()
        # names from ExtensionAPI.on() in @earendil-works/pi-coding-agent
        valid = {
            "session_start", "session_before_compact", "session_compact",
            "session_shutdown", "context", "before_provider_request",
            "after_provider_response", "before_agent_start", "agent_start",
            "agent_end", "turn_start", "turn_end", "message_start",
            "message_update", "message_end", "tool_execution_start",
            "tool_execution_end", "model_select", "user_bash", "input",
            "tool_call", "tool_result",
        }
        registered = set(re.findall(r'pi\.on\("([a-z_]+)"', text))
        self.assertTrue(registered, "extension registers no handlers at all")
        self.assertEqual(registered - valid, set())

    def test_observes_prompts_and_reports_usage(self):
        text = self._extension()
        # the prompt hook `pi` claims in PROVIDER_LEVELS
        self.assertIn("before_agent_start", text)
        self.assertIn("hook-observe.sh", text)
        # Pi hands usage over directly, so it never needs a transcript parser
        self.assertIn("ARTERIES_USAGE_TOKENS_IN", text)
        self.assertIn("ARTERIES_USAGE_MODEL", text)
