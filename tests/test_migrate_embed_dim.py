"""migrate_embed_dim finds every vector column itself and puts its indexes back.

Runs --apply against a throwaway schema, never the real arteries tables.
"""
import unittest
import uuid

import psycopg2
from dbprobe import DB_REACHABLE

from arteries import migrate_embed_dim
from arteries.config import DB_CONFIG


@unittest.skipUnless(DB_REACHABLE, "Postgres not reachable")
class MigrateEmbedDimTests(unittest.TestCase):
    def setUp(self):
        self.schema = f"migtest_{uuid.uuid4().hex[:8]}"
        self.conn = psycopg2.connect(**DB_CONFIG)
        self.conn.autocommit = True
        cur = self.conn.cursor()
        s = self.schema
        cur.execute(f"CREATE SCHEMA {s}")
        # indexed, unindexed, and already the right width
        cur.execute(f"CREATE TABLE {s}.indexed (id int, embedding vector(3))")
        cur.execute(f"CREATE INDEX idx_mt ON {s}.indexed USING hnsw (embedding vector_cosine_ops)")
        cur.execute(f"INSERT INTO {s}.indexed VALUES (1, '[1,2,3]')")
        cur.execute(f"CREATE TABLE {s}.added_later (embedding vector(3))")
        cur.execute(f"CREATE TABLE {s}.current (embedding vector(4))")

    def tearDown(self):
        self.conn.cursor().execute(f"DROP SCHEMA {self.schema} CASCADE")
        self.conn.close()

    def _width(self, table):
        cur = self.conn.cursor()
        cur.execute("SELECT a.atttypmod FROM pg_attribute a WHERE a.attrelid = %s::regclass "
                    "AND a.attname = 'embedding'", (f"{self.schema}.{table}",))
        return cur.fetchone()[0]

    def test_report_names_every_vector_column_and_changes_nothing(self):
        report = migrate_embed_dim.migrate(schema=self.schema, dim=4)
        status = {r["table"].split(".")[1]: r["status"] for r in report}
        self.assertEqual(status, {"indexed": "resize", "added_later": "resize", "current": "ok"})
        self.assertEqual(self._width("indexed"), 3)

    def test_apply_resizes_and_rebuilds_the_index(self):
        migrate_embed_dim.migrate(apply=True, schema=self.schema, dim=4)
        self.assertEqual([self._width(t) for t in ("indexed", "added_later", "current")], [4, 4, 4])
        cur = self.conn.cursor()
        cur.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = %s", (self.schema,))
        (indexdef,) = cur.fetchone()
        self.assertIn("idx_mt", indexdef)
        self.assertIn("hnsw (embedding vector_cosine_ops)", indexdef)
        cur.execute(f"SELECT embedding FROM {self.schema}.indexed")
        self.assertIsNone(cur.fetchone()[0], "old-model vectors must not survive")


if __name__ == "__main__":
    unittest.main()
