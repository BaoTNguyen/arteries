"""Concurrency at the generation boundary.

The database was already safe: intake resolves in `idx_eph_dedupe`, claims use
FOR UPDATE SKIP LOCKED plus a lease, consolidation is arbitrated by
`idx_evergreen_parents`, migrations hold an advisory lock. Nothing there can
corrupt.

The generator was not. Every turn spawns a detached compile process and nothing
bounded them, so five agents finishing at once meant five processes posting to a
two-slot server -- and llama.cpp queues the surplus invisibly, where a queued
request is indistinguishable from a slow one. heart caps agents that reach the
local endpoint, but these bypass its runner entirely.
"""

import unittest
from unittest.mock import patch

import psycopg2
import pytest

from arteries import compile as compile_mod
from arteries.config import DB_CONFIG


class SlotCountTests(unittest.TestCase):
    def test_an_explicit_setting_wins(self):
        with patch.object(compile_mod, "COMPILE_SLOTS", 5):
            self.assertEqual(compile_mod._generation_slots(), 5)

    def test_the_server_is_asked(self):
        """Matched to the server's real parallelism rather than hardcoded in two
        repos -- llama.cpp returns one object per slot on /slots."""
        class _Resp:
            @staticmethod
            def json():
                return [{"id": 0}, {"id": 1}, {"id": 2}]

        with patch.object(compile_mod, "COMPILE_SLOTS", 0), \
             patch("httpx.get", return_value=_Resp()):
            self.assertEqual(compile_mod._generation_slots(), 3)

    def test_an_unreachable_server_falls_back(self):
        """Unknown parallelism is better served by a guess than by 'unlimited'."""
        with patch.object(compile_mod, "COMPILE_SLOTS", 0), \
             patch("httpx.get", side_effect=OSError("no route")):
            self.assertEqual(compile_mod._generation_slots(), 2)


class SlotHoldingTests(unittest.TestCase):
    """Against a real database: an advisory lock is the only thing two checkouts
    can both see, which is the whole reason it is not a semaphore."""

    @classmethod
    def setUpClass(cls):
        pytest.importorskip("psycopg2")

    def setUp(self):
        self.conns = []

    def tearDown(self):
        for conn in self.conns:
            conn.close()

    def _conn(self):
        conn = psycopg2.connect(**DB_CONFIG)
        self.conns.append(conn)
        return conn

    def test_exactly_n_passes_get_a_slot(self):
        with patch.object(compile_mod, "COMPILE_SLOTS", 2):
            granted = [compile_mod._acquire_slot(self._conn()) for _ in range(4)]
        self.assertEqual(sorted(g for g in granted if g is not None), [0, 1])
        self.assertEqual(granted.count(None), 2)

    def test_releasing_frees_the_slot_for_the_next_pass(self):
        with patch.object(compile_mod, "COMPILE_SLOTS", 1):
            first_conn = self._conn()
            first = compile_mod._acquire_slot(first_conn)
            self.assertIsNone(compile_mod._acquire_slot(self._conn()))
            compile_mod._release_slot(first_conn, first)
            self.assertIsNotNone(compile_mod._acquire_slot(self._conn()))

    def test_a_dead_holder_does_not_wedge_a_slot(self):
        """Postgres drops a session lock when its holder dies, which is why a
        killed compile cannot leak one -- the same property heart's flock pool
        relies on."""
        with patch.object(compile_mod, "COMPILE_SLOTS", 1):
            doomed = psycopg2.connect(**DB_CONFIG)
            self.assertIsNotNone(compile_mod._acquire_slot(doomed))
            doomed.close()                      # stands in for the process dying
            self.assertIsNotNone(compile_mod._acquire_slot(self._conn()))

    def test_releasing_nothing_is_safe(self):
        compile_mod._release_slot(self._conn(), None)


class BackPressureTests(unittest.TestCase):
    """A refused pass must not lose work."""

    def test_no_slot_claims_nothing(self):
        import asyncio

        with patch.object(compile_mod, "_generator_reachable", return_value=True), \
             patch.object(compile_mod.psycopg2, "connect"), \
             patch.object(compile_mod, "_acquire_slot", return_value=None), \
             patch.object(compile_mod, "_claim_ephemeral",
                          side_effect=AssertionError("claimed without a slot")):
            result = asyncio.run(compile_mod.compile_once())
        self.assertEqual(result["status"], "no_generation_slot")
        self.assertEqual(result["claimed"], 0)
