"""Ephemeral keyed on the session, not on a pid that dies with the turn.

Finding 9. `AGENT_PROCESS_ID` falls back to `str(os.getpid())` when
ARTERIES_AGENT_ID is unset, so a row written by a hook that has exited is
matched by nothing: `get_ephemeral` cannot read it and `_claim_ephemeral` cannot
claim it. Measured in the live store: 84 of 717 rows carry a bare pid, across 83
distinct ids -- one row each, stranded permanently.
"""

import inspect
import os
import re
import unittest
from unittest.mock import patch

from arteries import compile as compile_mod
from arteries import storage


def _sql(fn) -> str:
    return re.sub(r"\s+", " ", inspect.getsource(fn))


class ReadPathTests(unittest.TestCase):
    def setUp(self):
        self.read = _sql(storage.get_ephemeral)

    def test_the_session_is_a_key(self):
        self.assertIn("session_id = %(session)s", self.read)

    def test_the_process_clause_is_kept_as_a_fallback(self):
        """Rows written before this column existed have a NULL session_id, and
        `main` writes NULL for as long as it runs. Dropping the older clause
        would hide them."""
        self.assertIn("agent_process_id = %(agent)s", self.read)
        self.assertIn("OR", self.read)

    def test_a_null_session_does_not_match_other_null_sessions(self):
        """`session_id = NULL` is never true, but a bare `session_id = %s` with
        None bound would still be a wasted comparison; the guard makes the
        intent explicit and lets the planner drop the clause."""
        self.assertIn("%(session)s::text IS NOT NULL", self.read)

    def test_the_session_is_read_from_the_environment_by_default(self):
        with patch.dict(os.environ, {"ARTERIES_SESSION_ID": "sess-1"}):
            self.assertEqual(storage._env_session_id(), "sess-1")

    def test_an_absent_session_is_none_not_empty_string(self):
        with patch.dict(os.environ, {"ARTERIES_SESSION_ID": ""}):
            self.assertIsNone(storage._env_session_id())


class WritePathTests(unittest.TestCase):
    def test_inserts_carry_the_session(self):
        self.assertIn("session_id", _sql(storage.insert_ephemeral))


class ClaimPathTests(unittest.TestCase):
    def setUp(self):
        self.claim = _sql(compile_mod._claim_ephemeral)

    def test_a_claim_can_reach_the_same_session(self):
        self.assertIn("session_id = %s", self.claim)

    def test_a_claim_can_reach_an_abandoned_row(self):
        """The only clause that can reach a row written under a dead pid."""
        self.assertIn("source_ts < now() - (%s || ' minutes')::interval", self.claim)

    def test_an_agent_drains_its_own_queue_first(self):
        self.assertIn("ORDER BY (agent_process_id = %s) DESC, source_ts ASC", self.claim)

    def test_abandonment_is_far_longer_than_a_compile_cycle(self):
        self.assertGreater(compile_mod.ABANDONED_AFTER_MINUTES,
                           compile_mod.STALE_CLAIM_MINUTES * 10)


class VisibilityTests(unittest.TestCase):
    """Finding 8: compilation marked a row 'cleared' and the read path filtered
    on status = 'uncompiled', so promoting a fact deleted it from the tier that
    serves in-session recall. Measured live: 599 cleared against 28 uncompiled,
    and promotion is instant when llama-server is healthy -- so in-session recall
    worked only while the compiler was broken."""

    def setUp(self):
        self.read = _sql(storage.get_ephemeral)
        self.coverage = _sql(storage.max_ephemeral_similarity)

    def test_the_read_path_no_longer_filters_on_compile_status(self):
        self.assertNotIn("status = 'uncompiled'", self.read)

    def test_visibility_is_a_time_window(self):
        self.assertIn("source_ts > now() -", storage._VISIBLE)
        self.assertIn("valid_until IS NULL", storage._VISIBLE)

    def test_both_readers_share_one_visibility_rule(self):
        """Two rules is how the coverage gate and the frame ended up
        disagreeing about what is in the working set."""
        self.assertIn("_VISIBLE", self.read)
        self.assertIn("_VISIBLE", self.coverage)
        self.assertNotIn("status <> 'cleared'", self.coverage)

    def test_the_claim_path_still_filters_on_status(self):
        """Visibility changed; what needs compiling did not."""
        self.assertIn("status = 'uncompiled'", _sql(compile_mod._claim_ephemeral))

    def test_a_promoted_row_stays_visible_for_a_couple_of_days(self):
        self.assertGreaterEqual(storage.EPHEMERAL_VISIBLE_HOURS, 24)
        self.assertLessEqual(storage.EPHEMERAL_VISIBLE_HOURS, 72)

    def test_promotion_records_when(self):
        self.assertIn("compiled_at = now()", _sql(compile_mod._write_results))
