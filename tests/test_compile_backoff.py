"""Failing without churning.

Findings 10, 13 and 25: a pass that could not reach llama-server claimed rows,
waited out the connection error, released them, and wrote two queue events --
then the next turn did it again. At a measured 44% failure rate that is most of
a turn's memory budget spent rediscovering the same outage.
"""

import inspect
import re
import unittest
from unittest.mock import patch

from arteries import compile as compile_mod


class HealthProbeTests(unittest.TestCase):
    def test_an_unreachable_generator_claims_nothing(self):
        import asyncio

        with patch.object(compile_mod, "_generator_reachable", return_value=False), \
             patch.object(compile_mod.psycopg2, "connect") as connect:
            result = asyncio.run(compile_mod.compile_once())
        self.assertEqual(result["status"], "generator_unreachable")
        self.assertEqual(result["claimed"], 0)
        connect.assert_not_called()

    def test_the_probe_never_raises(self):
        """Memory must not fail a turn, and a health check least of all."""
        with patch("httpx.get", side_effect=OSError("no route to host")):
            self.assertFalse(compile_mod._generator_reachable())

    def test_the_probe_is_cheap(self):
        self.assertLessEqual(compile_mod.HEALTH_TIMEOUT, 2.0)


class AttemptCountingTests(unittest.TestCase):
    def setUp(self):
        self.release = re.sub(r"\s+", " ", inspect.getsource(compile_mod._release_claimed))
        self.claim = re.sub(r"\s+", " ", inspect.getsource(compile_mod._claim_ephemeral))

    def test_a_failed_pass_counts_against_the_batch(self):
        self.assertIn("attempts = attempts + 1", self.release)

    def test_a_batch_is_given_up_on_after_the_ceiling(self):
        self.assertIn("quarantined_at = CASE WHEN attempts + 1 >= %s THEN now() END",
                      self.release)

    def test_quarantined_rows_are_never_claimed_again(self):
        self.assertIn("quarantined_at IS NULL", self.claim)

    def test_the_ceiling_survives_a_restart_but_not_a_poison_batch(self):
        self.assertGreaterEqual(compile_mod.MAX_COMPILE_ATTEMPTS, 2)
        self.assertLessEqual(compile_mod.MAX_COMPILE_ATTEMPTS, 5)


class ColdStartTests(unittest.TestCase):
    """Finding 12: embedding a batch to compare it against an empty table buys a
    network round trip and an empty list."""

    def test_an_empty_store_returns_before_embedding(self):
        conn = _FakeConn(exists=False)
        with patch("arteries.embed.embed_text_sync",
                   side_effect=AssertionError("embedded on a cold store")):
            self.assertEqual(
                compile_mod._load_persistent_context(conn, [{"fact": "a fact"}]), [])


class _FakeConn:
    def __init__(self, exists: bool):
        self._exists = exists

    def cursor(self, **_kwargs):
        return _FakeCursor(self._exists)


class _FakeCursor:
    def __init__(self, exists: bool):
        self._exists = exists

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return (self._exists,)

    def fetchall(self):
        return []
