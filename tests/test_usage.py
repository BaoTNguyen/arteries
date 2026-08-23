"""Usage extraction from CLI transcripts.

Three ways this goes wrong silently, each producing a plausible wrong number:
  1. counting transcript lines instead of API calls (Claude writes one record
     per content block) -> ~1.8x over
  2. treating Codex's cache-inclusive input as uncached -> ~6x over at real
     cache-hit rates
  3. re-reading the whole transcript every turn -> compounds without bound
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from arteries import usage  # noqa: E402


def write(path: Path, records: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def counts(u: dict) -> dict:
    """Just the token counts. `model` and `usage_source` ride along on the same
    dict but describe the measurement rather than being part of it."""
    return {k: v for k, v in u.items() if k in usage.USAGE_KEYS}


def claude_call(mid: str, blocks: int = 1, **counts) -> list[dict]:
    """One API response as the transcript actually stores it: `blocks` lines
    sharing one message id and one usage object."""
    u = {"input_tokens": counts.get("tin", 0),
         "output_tokens": counts.get("tout", 0),
         "cache_read_input_tokens": counts.get("read", 0),
         "cache_creation": {"ephemeral_5m_input_tokens": counts.get("w5", 0),
                            "ephemeral_1h_input_tokens": counts.get("w1", 0)}}
    return [{"type": "assistant", "uuid": f"{mid}-{i}", "requestId": f"req-{mid}",
             "message": {"id": mid, "usage": u}} for i in range(blocks)]


class TestUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(self.root / "state")
        self.t = self.root / "transcript.jsonl"

    def tearDown(self):
        os.environ.pop("XDG_STATE_HOME", None) if self._old is None \
            else os.environ.__setitem__("XDG_STATE_HOME", self._old)
        self.tmp.cleanup()

    def test_dedupes_content_blocks_of_one_call(self):
        # four lines, one API call: counting lines would quadruple the bill
        write(self.t, claude_call("m1", blocks=4, tin=10, tout=20, read=1000, w5=50))
        u = usage.turn_usage(str(self.t))
        self.assertEqual(counts(u), {"tokens_in": 10, "tokens_out": 20,
                                     "cache_read": 1000, "cache_write_5m": 50})

    def test_reports_each_call_only_once(self):
        write(self.t, claude_call("m1", blocks=2, tin=10, tout=20))
        first = usage.turn_usage(str(self.t))
        self.assertEqual(first["tokens_in"], 10)
        # same transcript, nothing new appended -> nothing to report
        self.assertEqual(usage.turn_usage(str(self.t)), {})
        # a second call appended reports only the new one
        write(self.t, claude_call("m2", blocks=3, tin=7, tout=3))
        self.assertEqual(counts(usage.turn_usage(str(self.t))),
                         {"tokens_in": 7, "tokens_out": 3})

    def test_partial_trailing_line_is_not_lost(self):
        write(self.t, claude_call("m1", tin=5, tout=5))
        with open(self.t, "a", encoding="utf-8") as f:
            f.write('{"message": {"id": "m2", "usage": {"input_toke')  # mid-write
        self.assertEqual(usage.turn_usage(str(self.t))["tokens_in"], 5)
        # the truncated record completes on the next flush and is counted then,
        # rather than being skipped because the offset ran past it
        with open(self.t, "a", encoding="utf-8") as f:
            f.write('ns": 99, "output_tokens": 1}}}\n')
        self.assertEqual(counts(usage.turn_usage(str(self.t))),
                         {"tokens_in": 99, "tokens_out": 1})

    def test_flat_cache_creation_total_when_tiers_absent(self):
        write(self.t, [{"message": {"id": "m1", "usage": {
            "input_tokens": 1, "output_tokens": 2,
            "cache_creation_input_tokens": 300}}}])
        u = usage.turn_usage(str(self.t))
        self.assertEqual(u["cache_write_5m"], 300)
        self.assertNotIn("cache_write_1h", u)

    def test_codex_cached_is_carved_out_of_input(self):
        # their arithmetic: input includes cached, and input + output == total
        write(self.t, [{"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 4293, "cached_input_tokens": 3200,
                                  "output_tokens": 520, "total_tokens": 4813}}}}])
        self.assertEqual(counts(usage.turn_usage(str(self.t))),
                         {"tokens_in": 1093, "tokens_out": 520, "cache_read": 3200})

    def test_codex_running_total_is_differenced_not_summed(self):
        def event(tin, cached, tout):
            return [{"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": tin, "cached_input_tokens": cached,
                                      "output_tokens": tout}}}}]
        write(self.t, event(1000, 900, 100))
        self.assertEqual(counts(usage.turn_usage(str(self.t))),
                         {"tokens_in": 100, "tokens_out": 100, "cache_read": 900})
        # cumulative: the next total includes the first, so the turn is the
        # delta of the *normalised* values — uncached input goes 100 -> 200, so
        # this turn sent 100. Summing the raw totals would claim 2500 in.
        write(self.t, event(2500, 2300, 250))
        self.assertEqual(counts(usage.turn_usage(str(self.t))),
                         {"tokens_in": 100, "tokens_out": 150, "cache_read": 1400})

    def test_missing_transcript_is_flagged_unavailable_not_free(self):
        """A CLI with no transcript we can read must be distinguishable from one
        that made no API call. Both used to return `{}`, so a whole provider
        with no adapter looked identical to a quiet turn and the gap stayed
        invisible on the cost panel."""
        self.assertEqual(usage.turn_usage(None),
                         {"usage_source": "unavailable"})
        self.assertEqual(usage.turn_usage(str(self.root / "nope.jsonl")),
                         {"usage_source": "unavailable"})
        # a transcript we *can* read that holds nothing countable is not the
        # same thing: the parser worked, there was just nothing to report
        self.t.write_text("not json\n\n{broken\n")
        self.assertEqual(usage.turn_usage(str(self.t)), {})

    def test_model_is_captured_for_pricing(self):
        """Counts nobody can price are half a measurement — Opus and Haiku on
        the same CLI differ by more than an order of magnitude."""
        write(self.t, [{"message": {"id": "m1", "model": "claude-opus-5",
                                    "usage": {"input_tokens": 5, "output_tokens": 5}}}])
        u = usage.turn_usage(str(self.t))
        self.assertEqual(u["model"], "claude-opus-5")
        self.assertEqual(u["usage_source"], "claude_transcript")

    def test_codex_model_comes_from_thread_settings(self):
        """Codex puts no model on token_count; the only source is the settings
        record, which is re-emitted whenever the model changes mid-session."""
        write(self.t, [
            {"payload": {"type": "thread_settings_applied",
                         "thread_settings": {"model": "gpt-5.6-sol"}}},
            {"payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10}}}},
        ])
        u = usage.turn_usage(str(self.t))
        self.assertEqual(u["model"], "gpt-5.6-sol")
        self.assertEqual(u["usage_source"], "codex_rollout")

        # a later chunk carrying only counts inherits the model rather than
        # dropping it — the settings record appears once, the counts every turn
        write(self.t, [{"payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 200, "cached_input_tokens": 0,
                                  "output_tokens": 30}}}}])
        self.assertEqual(usage.turn_usage(str(self.t))["model"], "gpt-5.6-sol")

    def test_fast_mode_speed_is_carried_through(self):
        """Fast mode bills Opus at 2x. It lives inside `usage`, not on the
        message, so a fast turn is indistinguishable from a standard one unless
        it is carried through — and the session under-reports by half."""
        write(self.t, [{"message": {"id": "m1", "model": "claude-opus-5", "usage": {
            "input_tokens": 10, "output_tokens": 5, "speed": "fast"}}}])
        u = usage.turn_usage(str(self.t))
        self.assertEqual(u["speed"], "fast")
        self.assertEqual(u["model"], "claude-opus-5")

    def test_standard_speed_is_recorded_not_dropped(self):
        """"standard" is a measurement, not a default. Keeping it lets a
        consumer tell "this ran standard" from "this host never reported
        speed at all"."""
        write(self.t, [{"message": {"id": "m2", "usage": {
            "input_tokens": 10, "output_tokens": 5, "speed": "standard"}}}])
        self.assertEqual(usage.turn_usage(str(self.t))["speed"], "standard")

    def test_speed_absent_when_the_host_never_reported_it(self):
        write(self.t, [{"message": {"id": "m3", "usage": {
            "input_tokens": 10, "output_tokens": 5}}}])
        self.assertNotIn("speed", usage.turn_usage(str(self.t)))

    def test_reported_usage_carries_model_only_alongside_counts(self):
        env = {"ARTERIES_USAGE_TOKENS_IN": "10", "ARTERIES_USAGE_MODEL": "pi-model"}
        self.assertEqual(usage.reported_usage(env),
                         {"tokens_in": 10, "model": "pi-model"})
        # a model with no counts is not a measurement, and must not read as one
        self.assertEqual(usage.reported_usage({"ARTERIES_USAGE_MODEL": "pi-model"}), {})

    def test_no_tokens_reports_nothing_rather_than_zeros(self):
        """`{}` and `{"tokens_in": 0}` must not look alike: one means the turn
        was never measured, the other that it genuinely cost nothing."""
        write(self.t, [{"message": {"id": "m1", "usage": {
            "input_tokens": 0, "output_tokens": 0}}}])
        self.assertEqual(usage.turn_usage(str(self.t)), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
