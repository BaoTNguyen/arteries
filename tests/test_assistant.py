import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arteries import assistant


class AssistantCaptureTests(unittest.TestCase):
    def test_event_messages_returns_last_assistant_text(self):
        event = {"messages": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "The build failed because the schema was missing source."},
        ]}

        self.assertEqual(
            assistant.assistant_text_from_event(event),
            "The build failed because the schema was missing source.",
        )

    def test_transcript_path_reads_last_assistant_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            path.write_text(
                json.dumps({"role": "assistant", "content": "First reply"}) + "\n" +
                json.dumps({"role": "user", "content": "next"}) + "\n" +
                json.dumps({"role": "assistant", "content": [{"type": "text", "text": "Latest reply"}]}) + "\n",
                encoding="utf-8",
            )

            self.assertEqual(assistant.read_last_assistant(str(path)), "Latest reply")

    def test_main_stores_and_logs_assistant_response(self):
        with patch.object(assistant, "store_assistant_response", return_value=1) as store, \
             patch.object(assistant.runlog, "log_event") as log_event:
            result = assistant.main(["Project decision: use hook-assistant-observe for assistant replies."])

        self.assertEqual(result, 0)
        store.assert_called_once()
        self.assertEqual(log_event.call_args_list[0].args[0], "assistant.response")
        self.assertEqual(log_event.call_args_list[1].args[0], "memory.assistant.stored")


if __name__ == "__main__":
    unittest.main()
