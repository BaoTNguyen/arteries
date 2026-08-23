import asyncio
import unittest
from unittest.mock import MagicMock, Mock, patch

from arteries import compile as compiler


class CompileOnceTests(unittest.TestCase):
    def test_cancelled_compile_releases_claimed_rows(self):
        conn = Mock()
        claimed = [{"id": "00000000-0000-0000-0000-000000000001"}]

        async def cancelled(_claimed, _persistent):
            raise asyncio.CancelledError()

        with patch.object(compiler.psycopg2, "connect", return_value=conn),              patch.object(compiler, "_release_stale_claims"),              patch.object(compiler, "_claim_ephemeral", return_value=claimed),              patch.object(compiler, "_load_persistent_context", return_value=[]),              patch.object(compiler, "_llm_compile", side_effect=cancelled),              patch.object(compiler, "_release_claimed") as release_claimed:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(compiler.compile_once())

        release_claimed.assert_called_once_with(conn, ["00000000-0000-0000-0000-000000000001"])
        conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class DedupeTests(unittest.TestCase):
    """Stage one of promotion: a restatement never reaches the store.

    The LLM pass is a decomposer -- 53 ephemeral rows produced 76 persistent
    facts -- so something deterministic has to bound growth. A cosine at or
    above DUPLICATE_SIM against anything live in scope is a restatement, and no
    model opinion is needed to say so.
    """

    def test_near_identical_fact_is_rejected_before_write(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("an existing fact about pgvector", 0.97)

        kept, vecs, rejected = compiler._reject_duplicates(
            conn, [{"fact": "an existing fact about pgvector, restated"}], [[0.1] * 8]
        )
        self.assertEqual(kept, [])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["similarity"], 0.97)

    def test_merely_related_fact_is_admitted(self):
        """0.75-0.93 is the band where the LLM decides; only >= 0.93 is refused
        outright."""
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ("a related fact", 0.81)

        kept, vecs, rejected = compiler._reject_duplicates(
            conn, [{"fact": "a related but distinct fact"}], [[0.1] * 8]
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(rejected, [])

    def test_unembeddable_fact_is_admitted_rather_than_dropped(self):
        """A down embedding server must not silently discard memories."""
        conn = MagicMock()
        kept, vecs, rejected = compiler._reject_duplicates(
            conn, [{"fact": "no vector for this one"}], [None]
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(rejected, [])
