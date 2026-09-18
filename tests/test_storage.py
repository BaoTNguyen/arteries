"""Packet chaining columns (planning/compaction_v3.md commit 1).

Additive only: record_packet writes them when given. Nothing reads them back
yet -- that lands in commit 2 with the renderer that first needs to, alongside
the read helper it will call.
"""

import uuid
from datetime import datetime, timezone

import pytest

from arteries import storage


class TestPacketChaining:
    @pytest.fixture(autouse=True)
    def _db(self, test_db):
        self.db = test_db
        self.project_id = f"test-{uuid.uuid4().hex[:8]}"
        self.session_id = f"sess-{uuid.uuid4().hex[:8]}"

    def test_chaining_columns_round_trip(self):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t1 = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
        storage.record_packet(
            self.project_id, ["m1"], session_id=self.session_id,
            covers_from=t0, covers_to=t1, resume_from="turn-9", body="hello",
        )
        with storage._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT covers_from, covers_to, resume_from, body FROM arteries.packets "
                "WHERE project_id=%s AND session_id=%s", (self.project_id, self.session_id),
            )
            row = cur.fetchone()
        assert row == (t0, t1, "turn-9", "hello")

    def test_second_packet_can_chain_off_the_first(self):
        storage.record_packet(self.project_id, ["m1"], session_id=self.session_id)
        with storage._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM arteries.packets WHERE project_id=%s AND session_id=%s",
                (self.project_id, self.session_id),
            )
            first_id = str(cur.fetchone()[0])

        storage.record_packet(
            self.project_id, ["m2"], session_id=self.session_id, previous_id=first_id,
        )
        with storage._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT previous_id FROM arteries.packets "
                "WHERE project_id=%s AND session_id=%s ORDER BY created_at DESC LIMIT 1",
                (self.project_id, self.session_id),
            )
            assert str(cur.fetchone()[0]) == first_id

    def test_chaining_columns_default_to_null(self):
        """A caller that doesn't pass them (every caller today) gets the old
        behaviour exactly -- this is what makes the migration a no-op until
        commit 2 starts populating them."""
        storage.record_packet(self.project_id, ["m1"], session_id=self.session_id)
        with storage._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT previous_id, covers_from, covers_to, resume_from, body "
                "FROM arteries.packets WHERE project_id=%s AND session_id=%s",
                (self.project_id, self.session_id),
            )
            assert cur.fetchone() == (None, None, None, None, None)
