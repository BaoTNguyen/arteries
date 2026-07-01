import unittest
from unittest.mock import patch

from arteries import memory_select
from arteries.cli_caps import CliCapabilities


class MemorySelectTests(unittest.TestCase):
    @patch.object(memory_select, "embed_text_sync", return_value=None)
    @patch.object(memory_select.storage, "get_persistent")
    @patch.object(memory_select.storage, "get_ephemeral")
    def test_default_selection_matches_existing_behavior(self, get_ephemeral, get_persistent, _embed):
        context = memory_select.AgentContext(
            cli="hermes",
            project_id="project",
            agent_id="agent",
            parent_agent_id=None,
            agent_role="parent",
            event="prompt",
            capabilities=CliCapabilities(name="hermes"),
        )
        get_ephemeral.return_value = [{"id": "e1", "fact": "current"}]
        get_persistent.return_value = [{"id": "p1", "fact": "persistent"}]

        ephemerals, persistents = memory_select.select_for_frame("message", context)

        self.assertEqual(ephemerals, [{"id": "e1", "fact": "current"}])
        self.assertEqual(persistents, [{"id": "p1", "fact": "persistent"}])
        get_ephemeral.assert_called_once_with("project", "agent", limit=20)
        get_persistent.assert_called_once_with("project", limit=20)

    @patch.object(memory_select, "embed_text_sync", return_value=None)
    @patch.object(memory_select.storage, "get_persistent", return_value=[])
    @patch.object(memory_select.storage, "get_ephemeral")
    def test_subagent_includes_parent_ephemeral_when_available(self, get_ephemeral, _persistent, _embed):
        context = memory_select.AgentContext(
            cli="claude",
            project_id="project",
            agent_id="child",
            parent_agent_id="parent",
            agent_role="subagent",
            event="prompt",
            capabilities=CliCapabilities(name="claude", observes_subagents=True),
        )
        get_ephemeral.side_effect = [
            [{"id": "child-1", "fact": "child memory"}],
            [{"id": "parent-1", "fact": "parent memory"}],
        ]

        ephemerals, _ = memory_select.select_for_frame("message", context)

        self.assertEqual([row["fact"] for row in ephemerals], ["child memory", "parent memory"])
        self.assertEqual(get_ephemeral.call_args_list[0].args, ("project", "child"))
        self.assertEqual(get_ephemeral.call_args_list[1].args, ("project", "parent"))

    @patch.object(memory_select, "embed_text_sync", return_value=None)
    @patch.object(memory_select.storage, "get_persistent", return_value=[])
    @patch.object(memory_select.storage, "get_ephemeral")
    def test_parent_ephemeral_is_not_included_without_parent_id(self, get_ephemeral, _persistent, _embed):
        context = memory_select.AgentContext(
            cli="pi",
            project_id="project",
            agent_id="agent",
            parent_agent_id=None,
            agent_role="subagent",
            event="prompt",
            capabilities=CliCapabilities(name="pi", observes_subagents=True),
        )
        get_ephemeral.return_value = [{"id": "e1", "fact": "current"}]

        ephemerals, _ = memory_select.select_for_frame("message", context)

        self.assertEqual(ephemerals, [{"id": "e1", "fact": "current"}])
        get_ephemeral.assert_called_once_with("project", "agent", limit=20)


if __name__ == "__main__":
    unittest.main()
