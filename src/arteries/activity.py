"""The clock retention runs on: days this project was worked, not days that passed.

Wall clock punishes being away. Take three weeks off and come back to a working
set that was never wrong but aged out on a calendar nobody was reading -- the
memory system would have forgotten precisely the context needed to pick the work
back up.

An activity day is a day with at least one observed turn. "Unused for 30 days"
becomes "unused across 30 days of actual work", which is what the sentence was
always trying to mean.

One row per project per day, inserted idempotently, so the cost is one upsert per
turn against a primary key.
"""

from __future__ import annotations

import psycopg2

from arteries import degrade
from arteries.config import DB_CONFIG


def touch(project_id: str, db_config: dict | None = None) -> None:
    """Record that today is an activity day. Idempotent and best effort.

    Never raises: a turn that fails to mark the day is a turn whose memory still
    works, and the only cost is retention counting one day fewer.
    """
    try:
        with psycopg2.connect(**(db_config or DB_CONFIG)) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO arteries.project_activity (project_id, day) "
                "VALUES (%s, current_date) ON CONFLICT DO NOTHING",
                (project_id,),
            )
            conn.commit()
    except Exception as exc:
        degrade.note(exc, "activity clock")


def current_day(project_id: str, db_config: dict | None = None) -> int:
    """How many activity days this project has had, today included.

    An ordinal rather than a date, so "last useful on day 40, now day 75" is a
    subtraction instead of a join.

    There is no `days_since` helper to go with this. One was written and
    `doctor.unreached` caught it before it shipped: `evict.candidates` does the
    subtraction in SQL, where the rows already are. Its one real idea -- that a
    NULL activity day means "predates the clock" and must not be read as day
    zero, or the first eviction run deletes the entire backlog -- lives in that
    query's WHERE clause instead.
    """
    with psycopg2.connect(**(db_config or DB_CONFIG)) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM arteries.project_activity WHERE project_id = %s",
            (project_id,),
        )
        return int(cur.fetchone()[0])
