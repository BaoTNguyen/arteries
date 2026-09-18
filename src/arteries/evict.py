"""Read-side decay: forget what nothing reads.

Finding 7. `access_count` was tracked and never acted on, so nothing ever left
persistent and write-side filtering was the only thing bounding the store.
Write-side filtering is never perfect -- 12.5% of live rows were transient
session intent even after the compiler was asked not to write them -- so decay is
what bounds the error the filter misses.

Tombstoned, never deleted. A retired claim is the answer to "what did we used to
think", and `memory_edges` still points at it.

Three exemptions, and they are the whole design:

* **preference and constraint.** Age and disuse are not evidence that a
  preference stopped being true. "Prefers tabs over spaces" is read once a month
  and true every day in between. These leave only by being superseded by a newer
  statement of the same preference, and only by one of equal or higher evidence
  class -- an inferred preference must not overwrite a stated one.
* **core.** A project's specification is not a claim that has to keep earning
  its place.
* **anything with a live edge.** A claim another claim refines or contradicts is
  load-bearing even if nobody read it directly.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import psycopg2
import psycopg2.extras

from arteries import activity, runlog
from arteries.config import AGENT_PROCESS_ID, DB_CONFIG, PROJECT_ID

# Activity days, not calendar days. Thirty days of actual work is a long time for
# a claim nobody needed.
UNUSED_ACTIVITY_DAYS = int(os.getenv("ARTERIES_UNUSED_DAYS", "30"))

# Never evicted by age. `kind` is the compiler's own label and unreliable in
# general -- the audit found a one-session instruction filed as a `constraint` --
# but unreliability cuts the safe way here: a mislabelled row survives, and
# survival is the failure this list is willing to have.
KEEP_FOREVER = ("preference", "constraint")


def candidates(conn, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """Live claims that have earned nothing and are holding nothing up."""
    today = activity.current_day(project_id)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT p.id, p.fact, p.kind, p.access_count, p.last_activity_day
            FROM arteries.persistent p
            WHERE p.project_id = %(project)s
              AND p.valid_until IS NULL
              AND p.kind <> ALL(%(keep)s)
              AND p.access_count = 0
              -- A row with no activity day predates the clock. Excluded rather
              -- than assumed stale: evicting the entire backlog the first time
              -- this runs is not decay, it is a deletion.
              AND p.last_activity_day IS NOT NULL
              AND %(today)s - p.last_activity_day >= %(unused)s
              -- Load-bearing even if unread: something refines or contradicts it.
              AND NOT EXISTS (
                  SELECT 1 FROM arteries.memory_edges e
                  WHERE e.valid_until IS NULL
                    AND ((e.src_kind = 'persistent' AND e.src_id = p.id::text)
                      OR (e.dst_kind = 'persistent' AND e.dst_id = p.id::text))
                    AND e.rel IN ('refines', 'contradicts', 'supports', 'supersedes')
              )
              -- Promoted to evergreen: the claim graduated, and tombstoning its
              -- parent would orphan the row that came from it.
              AND NOT EXISTS (
                  SELECT 1 FROM arteries.evergreen g
                  WHERE g.valid_until IS NULL AND p.id = ANY(g.parent_ids)
              )
            ORDER BY p.last_activity_day ASC
            LIMIT %(limit)s
            """,
            {"project": project_id, "keep": list(KEEP_FOREVER), "today": today,
             "unused": UNUSED_ACTIVITY_DAYS, "limit": limit},
        )
        return [dict(r) for r in cur.fetchall()]


def run(project_id: str | None = None, limit: int = 100,
        dry_run: bool = False) -> dict[str, Any]:
    """Tombstone what nothing reads. Returns what it did."""
    project_id = project_id or PROJECT_ID
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        rows = candidates(conn, project_id, limit)
        if dry_run or not rows:
            return {"status": "ok", "evicted": 0, "candidates": len(rows),
                    "dry_run": dry_run}
        ids = [str(r["id"]) for r in rows]
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE arteries.persistent SET valid_until = now() "
                "WHERE id = ANY(%s::uuid[]) AND valid_until IS NULL",
                (ids,),
            )
            evicted = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    runlog.log_event("memory.evicted", "arteries",
                     {"count": evicted, "unused_days": UNUSED_ACTIVITY_DAYS,
                      "facts": [r["fact"][:80] for r in rows[:5]]},
                     project_id=project_id, agent_id=AGENT_PROCESS_ID)
    return {"status": "ok", "evicted": evicted, "candidates": len(rows),
            "dry_run": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="art evict", description=__doc__)
    parser.add_argument("--project", default=None)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--write", action="store_true",
                        help="without this, only report what would go")
    args = parser.parse_args(argv)

    result = run(args.project, limit=args.limit, dry_run=not args.write)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
