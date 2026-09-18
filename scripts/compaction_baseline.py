#!/usr/bin/env python3
"""One-off measurement: how often does a session redo work a packet already
surfaced. Run before any v3 change lands (planning/compaction_v3.md §8) --
v1 and v3 packets aren't comparable by content, so this ratio, re-measured on
the same kind of sessions after v3 ships, is the only valid before/after.

Method: for each packet, resolve the facts it surfaced (join member_ids
against ephemeral/persistent/evergreen), then look at the tool.result events
in the following window and check whether the tool's target already appears
in that surfaced text. No `files` field exists on packets yet (that's v3), so
this is a substring-over-surfaced-facts proxy, not an exact match against a
rendered files list -- close this gap when v3's `files` field ships and
member_ids stops being the only record of what a packet contained.

ponytail: one file, stdlib + psycopg2 already a dependency, no new table.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from arteries import storage  # noqa: E402

WINDOW = timedelta(minutes=30)
TIERS = ("ephemeral", "persistent", "evergreen")


def _surfaced_text(cur, member_ids: list[str]) -> str:
    if not member_ids:
        return ""
    parts = []
    for tier in TIERS:
        cur.execute(
            f"SELECT fact FROM arteries.{tier} WHERE id = ANY(%s::uuid[])",
            (member_ids,),
        )
        parts.extend(row[0] for row in cur.fetchall() if row[0])
    return " ".join(parts).lower()


def main() -> int:
    with storage._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT project_id, session_id, member_ids, created_at "
            "FROM arteries.packets ORDER BY created_at"
        )
        packets = cur.fetchall()

        results = []
        for project_id, session_id, member_ids, created_at in packets:
            surfaced = _surfaced_text(cur, member_ids or [])
            if not surfaced:
                continue

            cur.execute(
                "SELECT payload FROM arteries.agent_events "
                "WHERE project_id = %s AND event_type = 'tool.result' "
                "AND created_at > %s AND created_at <= %s "
                "ORDER BY created_at",
                (project_id, created_at, created_at + WINDOW),
            )
            events = [row[0] for row in cur.fetchall()]
            if not events:
                continue

            repeats = sum(
                1 for e in events
                if (target := str(e.get("target") or "").lower())
                and len(target) > 6 and target in surfaced
            )
            results.append((project_id, session_id, created_at, len(events), repeats))

        total_events = sum(r[3] for r in results)
        total_repeats = sum(r[4] for r in results)

        print(f"packets measured: {len(results)} (of {len(packets)} total, "
              f"rest had no surfaced text or no follow-up tool calls)")
        print(f"tool.result events in window: {total_events}")
        print(f"apparent repeats: {total_repeats}")
        if total_events:
            print(f"re-derivation rate: {total_repeats / total_events:.3f}")
        print()
        print("by session:")
        for project_id, session_id, created_at, n_events, repeats in results:
            rate = f"{repeats / n_events:.2f}" if n_events else "n/a"
            print(f"  {project_id:12} {session_id or '(none)':24} "
                  f"{created_at} events={n_events:3} repeats={repeats:3} rate={rate}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
