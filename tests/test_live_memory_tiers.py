import asyncio
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg2
import psycopg2.extras

from arteries import compile as compiler
from arteries import extract, frame, memory_select, storage
from arteries.config import DB_CONFIG


LIVE_TESTS = os.getenv("ARTERIES_LIVE_TESTS") == "1"


@unittest.skipUnless(LIVE_TESTS, "set ARTERIES_LIVE_TESTS=1 to run live Postgres tier tests")
class LiveMemoryTierTests(unittest.TestCase):
    def setUp(self):
        self.project_id = f"arteries-live-test-{uuid4()}"
        self.agent_id = f"agent-{uuid4()}"
        self._patches = [
            patch.object(frame, "PROJECT_ID", self.project_id),
            patch.object(frame, "AGENT_PROCESS_ID", self.agent_id),
            patch.object(compiler, "PROJECT_ID", self.project_id),
            patch.object(compiler, "AGENT_PROCESS_ID", self.agent_id),
            patch.object(extract, "PROJECT_ID", self.project_id),
            patch.object(extract, "AGENT_PROCESS_ID", self.agent_id),
            # frame delegates tier selection to memory_select, which binds its
            # own module-level copies of these at import. Patching only frame's
            # left selection pointed at the real project, so the frame came back
            # empty and swallowed by get_current_frame's except. The env patch
            # below cannot fix it either -- config reads os.environ at import.
            patch.object(memory_select, "PROJECT_ID", self.project_id),
            patch.object(memory_select, "AGENT_PROCESS_ID", self.agent_id),
            patch.dict(os.environ, {
                "ARTERIES_PROJECT": self.project_id,
                "ARTERIES_AGENT_ID": self.agent_id,
            }),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM arteries.retrievals WHERE project_id = %s", (self.project_id,))
            cur.execute("DELETE FROM arteries.persistent WHERE project_id = %s", (self.project_id,))
            cur.execute("DELETE FROM arteries.ephemeral WHERE project_id = %s", (self.project_id,))
            cur.execute("DELETE FROM arteries.evergreen WHERE fact LIKE 'ARTERIES_LIVE_TEST %'")
            conn.commit()

    def test_1_ephemeral_extracts_stores_and_enters_frame(self):
        message = "I prefer stdlib-first testing for ephemeral memory in arteries."

        inserted = extract.extract_and_store(message)
        rows = storage.get_ephemeral(self.project_id, self.agent_id, limit=5)
        current_frame = frame.get_current_frame(message)

        self.assertEqual(inserted, 1)
        self.assertEqual(rows[0]["fact"], "I prefer stdlib-first testing for ephemeral memory in arteries")
        self.assertEqual(rows[0]["domains"], ["technical"])
        self.assertIn(rows[0]["fact"], current_frame.ephemeral.recent_messages)

    def test_2_persistent_compiles_from_ephemeral_and_enters_frame(self):
        storage.insert_ephemeral(
            project_id=self.project_id,
            agent_process_id=self.agent_id,
            fact="User is testing persistent compilation for arteries memory tiers.",
            domains=["technical"],
            confidence=0.8,
        )

        async def fake_llm_compile(ephemeral, persistent):
            return {
                "new_memories": [{
                    "fact": "User tests persistent compilation for arteries memory tiers.",
                    "domains": ["technical"],
                    "confidence": 0.88,
                }],
                "superseded": [],
                "skipped": [],
            }

        async def run_compile():
            with patch.object(compiler, "_llm_compile", side_effect=fake_llm_compile):
                return await compiler.compile_once()

        result = asyncio.run(run_compile())
        rows = storage.get_persistent(self.project_id, limit=5)
        current_frame = frame.get_current_frame("testing persistent memory")

        self.assertEqual(result["status"], "compiled")
        self.assertEqual(result["new_persistent"], 1)
        self.assertEqual(rows[0]["fact"], "User tests persistent compilation for arteries memory tiers.")
        self.assertEqual(current_frame.persistent.session_insights[0].text, rows[0]["fact"])
        self.assertIn("technical", current_frame.persistent.active_domains)

    def test_3_evergreen_reads_global_memory_into_frame(self):
        fact = "ARTERIES_LIVE_TEST evergreen memory exposes user intent across projects."
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO arteries.evergreen (fact, domains, confidence)
                VALUES (%s, %s::jsonb, %s)
                """,
                (fact, psycopg2.extras.Json(["intent", "technical"]), 0.93),
            )
            conn.commit()

        rows = storage.get_evergreen(limit=10)
        current_frame = frame.get_current_frame("testing evergreen memory")

        self.assertTrue(any(row["fact"] == fact for row in rows))
        self.assertIn(fact, current_frame.evergreen.user_intent)
        self.assertTrue(any(insight.text == fact for insight in current_frame.evergreen.ground_truth_insights))
        self.assertIn("intent", current_frame.evergreen.recurring_domains)


if __name__ == "__main__":
    unittest.main()
