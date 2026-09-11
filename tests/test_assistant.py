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
    def test_read_last_exchange_names_the_question_across_tool_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            rows = [
                {"type": "user", "uuid": "u1", "parentUuid": None,
                 "message": {"content": "why is the build slow"}},
                {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
                 "message": {"content": [{"type": "tool_use", "id": "t1"}]}},
                {"type": "user", "uuid": "u2", "parentUuid": "a1",
                 "message": {"content": [{"type": "tool_result", "content": "logs"}]}},
                {"type": "assistant", "uuid": "a2", "parentUuid": "u2",
                 "message": {"content": [{"type": "text", "text": "The cache is cold."}]}},
            ]
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

            text, answers = assistant.read_last_exchange(str(path))

        self.assertEqual(text, "The cache is cold.")
        self.assertEqual(answers, "why is the build slow")

    def test_read_last_exchange_returns_no_key_when_the_chain_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            path.write_text(json.dumps(
                {"type": "assistant", "uuid": "a1", "parentUuid": "gone",
                 "message": {"content": [{"type": "text", "text": "orphan reply"}]}}) + "\n",
                encoding="utf-8")

            text, answers = assistant.read_last_exchange(str(path))

        self.assertEqual(text, "orphan reply")
        self.assertEqual(answers, "")

    def test_capture_response_carries_the_join_key(self):
        with patch.object(assistant, "store_assistant_response", return_value=1), \
             patch.object(assistant.runlog, "log_event") as log_event:
            assistant.capture_response("A reply.", turn_id="turn-2", prior_turn=True,
                                       answers="the original question")

        payload = log_event.call_args_list[0].args[2]
        self.assertEqual(payload["answers_preview"], "the original question")

    def test_stop_capture_tags_the_open_turn(self):
        # the Stop hook is a fresh process, so the turn_id has to come back out
        # of the store or the response lands on no turn at all
        events = [
            {"event_type": "assistant.response", "turn_id": "turn-9", "payload": {}},
            {"event_type": "turn.observed", "turn_id": "turn-7", "payload": {}},
            {"event_type": "turn.observed", "turn_id": "turn-6", "payload": {}},
        ]

        with patch.object(assistant, "store_assistant_response", return_value=1), \
             patch.object(assistant.runlog, "recent_events", return_value=events), \
             patch.object(assistant.runlog, "log_event") as log_event:
            self.assertEqual(assistant.main(["A reply long enough to be stored by the capture path."]), 0)

        self.assertEqual(log_event.call_args_list[0].kwargs["turn_id"], "turn-7")
        self.assertNotIn("prior_turn", log_event.call_args_list[0].args[2])

    def test_open_turn_id_survives_an_unreadable_store(self):
        with patch.object(assistant.runlog, "recent_events", side_effect=RuntimeError("no db")):
            self.assertIsNone(assistant._open_turn_id())


class CaptureFromTranscriptTests(unittest.TestCase):
    """The capture path itself. Its except-Exception-pass swallows anything that
    goes wrong inside, so nothing short of exercising it end to end proves it
    runs at all -- a missing import sat there silently until ruff found it."""

    def _transcript(self, tmp: str) -> str:
        path = Path(tmp) / "transcript.jsonl"
        rows = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "message": {"content": "why is the build slow"}},
            {"type": "user", "uuid": "u2", "parentUuid": "u1",
             "message": {"content": [{"type": "tool_result", "content": "logs"}]}},
            {"type": "assistant", "uuid": "a2", "parentUuid": "u2",
             "message": {"content": [{"type": "text",
                                      "text": "The cache is cold, so every build recompiles."}]}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return str(path)

    def test_capture_stores_the_answer_with_the_question_it_answers(self):
        from arteries import eval as arteries_eval

        with tempfile.TemporaryDirectory() as tmp:
            env = {"ARTERIES_TRANSCRIPT": self._transcript(tmp)}
            with patch.dict("os.environ", env), \
                    patch.object(arteries_eval, "capture_response") as capture:
                arteries_eval._capture_last_response("turn-2")

        capture.assert_called_once()
        self.assertEqual(capture.call_args.kwargs["answers"], "why is the build slow")
        self.assertEqual(capture.call_args.kwargs["turn_id"], "turn-2")
        self.assertTrue(capture.call_args.kwargs["prior_turn"])

    def test_capture_skips_a_reply_too_short_to_be_worth_storing(self):
        from arteries import eval as arteries_eval

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(json.dumps(
                {"type": "assistant", "uuid": "a1", "parentUuid": None,
                 "message": {"content": [{"type": "text", "text": "Done."}]}}) + "\n",
                encoding="utf-8")
            with patch.dict("os.environ", {"ARTERIES_TRANSCRIPT": str(path)}), \
                    patch.object(arteries_eval, "capture_response") as capture:
                arteries_eval._capture_last_response("turn-2")

        capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
