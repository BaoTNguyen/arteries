"""Small health check for arteries runtime wiring."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg2

from arteries import runlog
from arteries.config import AGENT_PROCESS_ID, DB_CONFIG


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check arteries project/run logging setup.")
    parser.add_argument("--project", default=None,
                        help="defaults to the scope-resolved project for this directory")
    parser.add_argument("--agent", default=os.getenv("ARTERIES_AGENT_ID") or AGENT_PROCESS_ID)
    parser.add_argument("--cli", default=os.getenv("ARTERIES_CLI") or os.getenv("AGENT_CLI") or "unknown")
    parser.add_argument("--repo", type=Path, default=Path(os.getenv("ARTERIES_REPO") or Path.cwd()))
    parser.add_argument("--fix", action="store_true",
                        help="repair what can be repaired: embed rows missing a vector")
    ns = parser.parse_args(argv)
    # Hooks set ARTERIES_PROJECT; a human running `art doctor` does not, and
    # PROJECT_ID would fall back to "default" and report on the wrong project.
    from arteries import scope
    ns.project = ns.project or scope.current_project()

    report = check(ns.project, ns.agent, ns.cli, ns.repo)
    report["integrity"] = integrity(ns.project)
    if ns.fix:
        report["fixed"] = fix(ns.project)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def integrity(project: str) -> dict[str, Any]:
    """Cheap consistency checks that need no repair to be worth reporting."""
    import psycopg2

    from arteries.config import DB_CONFIG

    out: dict[str, Any] = {}
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) FROM arteries.persistent
                WHERE project_id = %s AND valid_until IS NULL AND embedding IS NULL
            """, (project,))
            out["persistent_missing_embedding"] = cur.fetchone()[0]

            # No foreign key is possible: src/dst span tables with different key
            # types. A periodic sweep is the substitute.
            cur.execute("""
                SELECT count(*) FROM arteries.memory_edges e
                WHERE e.valid_until IS NULL AND e.dst_kind = 'persistent'
                  AND NOT EXISTS (SELECT 1 FROM arteries.persistent p
                                  WHERE p.id::text = e.dst_id)
            """)
            out["dangling_edges"] = cur.fetchone()[0]

            cur.execute("""
                SELECT count(*) FROM arteries.ephemeral
                WHERE status = 'compiling'
                  AND source_ts < now() - INTERVAL '10 minutes'
            """)
            out["stranded_claims"] = cur.fetchone()[0]

            cur.execute("SELECT count(*) FROM arteries.scope_members WHERE project_id = %s",
                        (project,))
            out["scope_registered"] = bool(cur.fetchone()[0])
    except Exception as exc:
        out["error"] = exc.__class__.__name__
    return out


def fix(project: str) -> dict[str, Any]:
    """Embed live persistent rows that have no vector.

    Was `art backfill-embeddings`. A repair belongs next to the check that
    reports it needs doing, not as its own top-level verb.
    """
    import psycopg2
    import psycopg2.extras

    from arteries.config import DB_CONFIG
    from arteries.embed import embed_texts_sync

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, fact FROM arteries.persistent
                WHERE project_id = %s AND valid_until IS NULL AND embedding IS NULL
                LIMIT 200
            """, (project,))
            rows = cur.fetchall()
        if not rows:
            return {"embedded": 0}
        vectors = embed_texts_sync([r["fact"] for r in rows])
        done = 0
        with conn.cursor() as cur:
            for row, vec in zip(rows, vectors):
                if vec is None:
                    continue
                cur.execute("UPDATE arteries.persistent SET embedding = %s::vector WHERE id = %s",
                            (vec, row["id"]))
                done += 1
            conn.commit()
        return {"embedded": done, "of": len(rows)}
    finally:
        conn.close()


def check(project: str, agent: str, cli: str, repo: Path) -> dict[str, Any]:
    fallback = repo / ".arteries" / "runs"
    checks: dict[str, Any] = {
        "project_id": project,
        "agent_id": str(agent),
        "cli": cli,
        "repo": str(repo.resolve()),
        "fallback_path": str(fallback),
    }

    # subagent attribution breaks silently when the agent id falls back to a PID:
    # parent/child ids never match and tagged records are never claimed
    checks["agent_id_pinned"] = bool(os.getenv("ARTERIES_AGENT_ID"))
    if not checks["agent_id_pinned"]:
        checks["agent_id_warning"] = (
            "ARTERIES_AGENT_ID is unset; agent id falls back to the process PID, "
            "so subagent records tagged with a parent id will never be claimed."
        )

    db_ok, schema_ok, db_error = _check_db()
    checks["db_ok"] = db_ok
    checks["schema_ok"] = schema_ok
    if db_error:
        checks["db_error"] = db_error

    try:
        fallback.mkdir(parents=True, exist_ok=True)
        checks["fallback_ok"] = fallback.exists() and os.access(fallback, os.W_OK)
    except Exception as exc:
        checks["fallback_ok"] = False
        checks["fallback_error"] = str(exc)

    event = runlog.log_event(
        "doctor.write_test",
        "arteries",
        {},
        project_id=project,
        agent_id=agent,
        cli=cli,
        repo_path=repo,
    )
    recent = runlog.recent_events(project, limit=10, repo_path=repo)
    checks["write_ok"] = bool(event.get("id"))
    checks["read_ok"] = any(row.get("id") == event.get("id") for row in recent)
    checks["run_id"] = event.get("run_id")
    checks["ok"] = bool(checks["fallback_ok"] and checks["write_ok"] and checks["read_ok"] and (not db_ok or schema_ok))
    return checks


def _check_db() -> tuple[bool, bool, str | None]:
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT to_regclass('arteries.agent_runs'),
                       to_regclass('arteries.agent_events')
                """
            )
            runs, events = cur.fetchone()
            return True, bool(runs and events), None
    except Exception as exc:
        return False, False, str(exc)[:300]


if __name__ == "__main__":
    raise SystemExit(main())
