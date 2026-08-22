"""Inspect current arteries memory and runlog state."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from arteries import graph, runlog, scope, storage
from arteries.config import AGENT_PROCESS_ID


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect current arteries state.")
    parser.add_argument("--project", default=None,
                        help="defaults to the scope-resolved project for this directory")
    parser.add_argument("--agent", default=AGENT_PROCESS_ID)
    parser.add_argument("--events", type=int, default=10)
    ns = parser.parse_args(argv)
    ns.project = ns.project or scope.current_project()

    summary = {
        "project_id": ns.project,
        "agent_id": ns.agent,
        "ephemeral": _safe(lambda: storage.get_ephemeral(ns.project, ns.agent, limit=10)),
        "persistent": _safe(lambda: storage.get_persistent(ns.project, limit=10)),
        "entities": _safe(lambda: _entities(ns.project)),
        "edges": _safe(lambda: graph.stats(ns.project)["edges"]),
        "recent_events": runlog.recent_events(ns.project, limit=ns.events),
    }
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


def _safe(fn):
    try:
        return fn()
    except Exception as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    raise SystemExit(main())


def _entities(project: str, limit: int = 10) -> list[dict]:
    """Top entities for this project's scope, by how often claims mention them."""
    import psycopg2
    import psycopg2.extras

    from arteries.config import DB_CONFIG

    with psycopg2.connect(**DB_CONFIG) as conn, \
         conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT e.name, e.kind, e.ontology_valid, count(m.id) AS mentions
            FROM arteries.entities e
            LEFT JOIN arteries.memory_edges m
              ON m.dst_kind = 'entity' AND m.dst_id = e.id::text AND m.valid_until IS NULL
            WHERE e.scope_id = %s
            GROUP BY e.id ORDER BY mentions DESC, e.name LIMIT %s
            """,
            (scope.scope_for(project) or project, limit),
        )
        return [dict(r) for r in cur.fetchall()]
