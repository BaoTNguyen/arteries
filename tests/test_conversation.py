import json
import tempfile
import unittest
from pathlib import Path

from arteries.conversation import recent_assistant_turns, recent_turns


class ConversationTests(unittest.TestCase):
    def test_recent_turns_reads_only_user_and_assistant_messages(self):
        records = [
            {"role": "system", "content": "ignore"},
            {"role": "user", "content": "Define Tier A labels."},
            {"role": "assistant", "content": "Tier A is the first label set."},
            {"role": "tool", "content": "ignore"},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as handle:
            handle.write("\n".join(json.dumps(record) for record in records))
            path = Path(handle.name)
        try:
            self.assertEqual(recent_turns(str(path)), [
                "Define Tier A labels.", "Tier A is the first label set.",
            ])
            self.assertEqual(recent_assistant_turns(str(path)), [
                "Tier A is the first label set.",
            ])
        finally:
            path.unlink()
