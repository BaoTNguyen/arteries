"""Small health check for arteries runtime wiring."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg2

from arteries import runlog, scope
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
    # Hooks set ARTERIES_PROJECT; a human running `art doctor` does not, so
    # resolve the project from the cwd rather than trusting the env.
    ns.project = ns.project or scope.current_project()

    report = check(ns.project, ns.agent, ns.cli, ns.repo)
    report["integrity"] = integrity(ns.project)
    if ns.fix:
        report["fixed"] = fix(ns.project)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


# Ephemeral is a working set: a row that was compiled and produced nothing is
# spent. Rows that *did* produce a memory are kept regardless of age, because a
# derived_from edge points at them and provenance back to the raw turn is the
# whole reason those edges exist.
EPHEMERAL_RETENTION_DAYS = int(os.getenv("ARTERIES_EPHEMERAL_RETENTION_DAYS", "14"))
# Longer than ephemeral: a retired claim is the answer to "what did we used to
# think", and that question outlives a working set by a good margin.
CLAIM_RETENTION_DAYS = int(os.getenv("ARTERIES_CLAIM_RETENTION_DAYS", "90"))

_COLLECTABLE_SQL = """
    SELECT {select} FROM arteries.ephemeral e
    WHERE e.project_id = %s
      AND e.status = 'cleared'
      AND e.source_ts < now() - (%s || ' days')::interval
      AND NOT EXISTS (
          SELECT 1 FROM arteries.memory_edges m
          WHERE m.dst_kind = 'ephemeral' AND m.dst_id = e.id::text
            AND m.valid_until IS NULL
      )
"""


# A tombstoned claim is filtered from every read, but it is still the target of
# `supersedes` edges that say what replaced it. Deleting one that something
# cites destroys the audit trail; deleting one that nothing cites frees a row.
# Same rule as ephemeral collection.
_COLLECTABLE_CLAIMS = """
    SELECT {select} FROM arteries.persistent p
    WHERE p.project_id = %s
      AND p.valid_until IS NOT NULL
      AND p.valid_until < now() - (%s || ' days')::interval
      AND NOT EXISTS (
          SELECT 1 FROM arteries.memory_edges m
          WHERE m.valid_until IS NULL
            AND ((m.dst_kind = 'persistent' AND m.dst_id = p.id::text)
              OR (m.src_kind = 'persistent' AND m.src_id = p.id::text))
      )
"""

# An entity nothing live mentions any more. Cheap to recreate if it comes back,
# and it otherwise sits in the uniqueness index forever holding a name.
# Entities are keyed by scope_id, not project_id -- one namespace shared by
# every repo in the group -- so this is the one collector that scopes rather
# than projects.
_ORPHAN_ENTITIES = """
    SELECT {select} FROM arteries.entities e
    WHERE e.scope_id = %s
      AND NOT EXISTS (
        SELECT 1 FROM arteries.memory_edges m
        JOIN arteries.persistent p ON p.id::text = m.src_id
        WHERE m.dst_kind = 'entity' AND m.dst_id = e.id::text
          AND m.valid_until IS NULL AND p.valid_until IS NULL
    )
"""


def _retire_edges_to_dead_claims(conn) -> int:
    """Retire edges whose target has been tombstoned.

    Reads already filter on the claim's own validity, so these are not surfacing
    anything -- but a live edge to a dead claim is a lie about the graph's shape
    and it accumulates.
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE arteries.memory_edges m SET valid_until = now()
            WHERE m.valid_until IS NULL AND m.dst_kind = 'persistent'
              AND EXISTS (SELECT 1 FROM arteries.persistent p
                          WHERE p.id::text = m.dst_id AND p.valid_until IS NOT NULL)
        """)
        conn.commit()
        return cur.rowcount


def _collect_claims(conn, project: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM arteries.persistent p WHERE p.id IN ("
                    f"{_COLLECTABLE_CLAIMS.format(select='p.id')})",
                    (project, CLAIM_RETENTION_DAYS))
        conn.commit()
        return cur.rowcount


def _collect_entities(conn, scope_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM arteries.entities e WHERE e.id IN ("
                    f"{_ORPHAN_ENTITIES.format(select='e.id')})", (scope_id,))
        conn.commit()
        return cur.rowcount


def _collect_ephemeral(conn, project: str) -> int:
    """Delete spent ephemeral rows. Nothing that is cited is ever removed."""
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM arteries.ephemeral e WHERE e.id IN ("
                    f"{_COLLECTABLE_SQL.format(select='e.id')})",
                    (project, EPHEMERAL_RETENTION_DAYS))
        conn.commit()
        return cur.rowcount


# Public functions that are reached some way other than a direct call, so an
# absent caller is not evidence of anything.
_REACHED_INDIRECTLY = {
    "main",            # CLI dispatch and __main__ blocks
    "setup", "check",  # setup_cli recipe tables
    "observe",         # public API for other repos in the stack
    "ingest_episodes", "ingest_heart_episodes",
    "run", "choose",   # called through modules that import them qualified
}


def unreached(root: Path | None = None) -> list[str]:
    """Public functions nothing outside their own module and the tests calls.

    Six times in this rework something was written, looked right, passed tests,
    and was never invoked -- graph.expand, its edge traversal, the expansion
    gate, the benchmark's context, MemoryFrame.scope, max_ephemeral_similarity.
    Tests prove a function behaves when called. They say nothing about whether
    anything calls it.

    Deliberately crude: a name-level grep, not a call graph. It over-reports
    rather than under-reports, and _REACHED_INDIRECTLY carries the exceptions.
    """
    import re

    root = root or Path(__file__).resolve().parent
    sources = {p: p.read_text() for p in sorted(root.glob("*.py"))}
    orphans = []
    for path, text in sources.items():
        for match in re.finditer(r"^def ([a-z][a-z0-9_]*)\(", text, re.M):
            name = match.group(1)
            if name.startswith("_") or name in _REACHED_INDIRECTLY:
                continue
            # Count calls anywhere, including this module's own main() -- a
            # function its CLI entry point invokes is reached. Subtract the
            # definition itself, which the pattern also matches.
            calls = sum(len(re.findall(rf"\b{name}\s*\(", other))
                        for other in sources.values())
            if calls - 1 <= 0:
                orphans.append(f"{path.stem}.{name}")
    return orphans


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
            # Every referencing kind, not just persistent. Chunks are deleted
            # and rewritten on re-ingest, which is where these actually come
            # from. `literal` endpoints hold text, not ids, and are skipped.
            cur.execute("""
                SELECT count(*) FROM arteries.memory_edges e
                WHERE e.valid_until IS NULL
                  AND ((e.dst_kind = 'persistent' AND NOT EXISTS
                          (SELECT 1 FROM arteries.persistent p WHERE p.id::text = e.dst_id))
                    OR (e.dst_kind = 'chunk' AND NOT EXISTS
                          (SELECT 1 FROM arteries.chunks c WHERE c.id::text = e.dst_id))
                    OR (e.dst_kind = 'entity' AND NOT EXISTS
                          (SELECT 1 FROM arteries.entities n WHERE n.id::text = e.dst_id))
                    -- src, not just dst. Checking one end only reported 0 while
                    -- 49 edges hung off claims that had been deleted -- the
                    -- `literal` chose/over edges never had a dst to check, so
                    -- the src side is the only place they can be caught.
                    OR (e.src_kind = 'persistent' AND NOT EXISTS
                          (SELECT 1 FROM arteries.persistent p WHERE p.id::text = e.src_id)))
            """)
            out["dangling_edges"] = cur.fetchone()[0]

            cur.execute("""
                SELECT count(*) FROM arteries.ephemeral
                WHERE status = 'compiling'
                  AND source_ts < now() - INTERVAL '10 minutes'
            """)
            out["stranded_claims"] = cur.fetchone()[0]

            cur.execute(_COLLECTABLE_SQL.format(select="count(*)"),
                        (project, EPHEMERAL_RETENTION_DAYS))
            out["collectable_ephemeral"] = cur.fetchone()[0]

            cur.execute("SELECT count(*) FROM arteries.scope_members WHERE project_id = %s",
                        (project,))
            out["scope_registered"] = bool(cur.fetchone()[0])

            cur.execute(_COLLECTABLE_CLAIMS.format(select="count(*)"),
                        (project, CLAIM_RETENTION_DAYS))
            out["collectable_claims"] = cur.fetchone()[0]

            cur.execute(_ORPHAN_ENTITIES.format(select="count(*)"),
                        (scope.scope_for(project) or project,))
            out["orphan_entities"] = cur.fetchone()[0]

            cur.execute("""
                SELECT count(*) FROM arteries.memory_edges m
                WHERE m.valid_until IS NULL AND m.dst_kind = 'persistent'
                  AND EXISTS (SELECT 1 FROM arteries.persistent p
                              WHERE p.id::text = m.dst_id AND p.valid_until IS NOT NULL)
            """)
            out["edges_to_dead_claims"] = cur.fetchone()[0]

            # Both sides of a contradiction still live. Nothing resolves these
            # automatically -- the compiler flagged the conflict without picking
            # a winner -- so they are surfaced for a human.
            cur.execute("""
                SELECT count(*) FROM arteries.memory_edges m
                JOIN arteries.persistent a ON a.id::text = m.src_id
                JOIN arteries.persistent b ON b.id::text = m.dst_id
                WHERE m.rel = 'contradicts' AND m.valid_until IS NULL
                  AND a.valid_until IS NULL AND b.valid_until IS NULL
            """)
            out["unresolved_contradictions"] = cur.fetchone()[0]
    except Exception as exc:
        out["error"] = exc.__class__.__name__

    # Static, so it still reports even when Postgres is unreachable.
    out["unreached_functions"] = unreached()
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
        # Order matters. Deletions orphan the edges that pointed at what they
        # removed, so the dangling sweep runs last -- running it first left
        # eight fresh dangling edges behind the entity collection.
        retired = _retire_edges_to_dead_claims(conn)
        collected = _collect_ephemeral(conn, project)
        claims = _collect_claims(conn, project)
        entities = _collect_entities(conn, scope.scope_for(project) or project)
        retired += _retire_dangling(conn)
        if not rows:
            return {"embedded": 0, "dangling_edges_retired": retired,
                    "ephemeral_collected": collected, "claims_collected": claims,
                    "orphan_entities_removed": entities}
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
        return {"embedded": done, "of": len(rows),
                "dangling_edges_retired": retired, "ephemeral_collected": collected,
                "claims_collected": claims, "orphan_entities_removed": entities}
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


def _retire_dangling(conn) -> int:
    """Tombstone edges whose endpoint no longer exists.

    memory_edges can carry no foreign key -- src and dst span tables with
    different key types -- so referential integrity is swept rather than
    enforced. Retired, not deleted: the same convention every other tier uses,
    and a dangling edge is still evidence of what once pointed where.
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE arteries.memory_edges e SET valid_until = now()
            WHERE e.valid_until IS NULL
              AND ((e.dst_kind = 'persistent' AND NOT EXISTS
                      (SELECT 1 FROM arteries.persistent p WHERE p.id::text = e.dst_id))
                OR (e.dst_kind = 'chunk' AND NOT EXISTS
                      (SELECT 1 FROM arteries.chunks c WHERE c.id::text = e.dst_id))
                OR (e.dst_kind = 'entity' AND NOT EXISTS
                      (SELECT 1 FROM arteries.entities n WHERE n.id::text = e.dst_id)))
        """)
        conn.commit()
        return cur.rowcount
