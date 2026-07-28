"""
Storage layer for arteries memory tiers.

All three tiers live in the arteries schema of the shared capillaries
Postgres instance. Queries return dicts — the frame module converts
them to MemoryFrame types.
"""

from __future__ import annotations

from typing import Any

import psycopg2
import psycopg2.extras

from arteries.config import DB_CONFIG

# Confidence is read back as stored. Age-based decay lived here and was removed —
# age alone was the wrong signal (a stale-but-still-true fact decayed like a
# wrong one, and a re-confirmed fact didn't recover). A usefulness-driven method
# will replace it later; until then, no decay.


def _conn():
    return psycopg2.connect(**DB_CONFIG)


# -- Ephemeral ----------------------------------------------------------------

def get_ephemeral(
    project_id: str,
    agent_process_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, fact, domains, confidence, source_ts, status
            FROM arteries.ephemeral
            WHERE project_id = %s
              AND agent_process_id = %s
              AND status = 'uncompiled'
              AND valid_until IS NULL
            ORDER BY source_ts DESC
            LIMIT %s
            """,
            (project_id, agent_process_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def insert_ephemeral(
    project_id: str,
    agent_process_id: str,
    fact: str,
    domains: list[str],
    confidence: float = 1.0,
    parent_agent_id: str | None = None,
    embedding: list[float] | None = None,
    source: str = "user",
) -> str:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arteries.ephemeral
                (fact, embedding, domains, confidence, project_id,
                 agent_process_id, parent_agent_id, source)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                fact,
                embedding,
                psycopg2.extras.Json(domains),
                confidence,
                project_id,
                agent_process_id,
                parent_agent_id,
                source,
            ),
        )
        conn.commit()
        return str(cur.fetchone()[0])


# -- Persistent ---------------------------------------------------------------

def get_persistent(
    project_id: str,
    limit: int = 50,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if scope:
            cur.execute(
                """
                SELECT id, fact, domains, confidence, source_ts, scope
                FROM arteries.persistent
                WHERE project_id = %s
                  AND valid_until IS NULL
                  AND (scope IS NULL OR scope = %s)
                ORDER BY source_ts DESC
                LIMIT %s
                """,
                (project_id, scope, limit),
            )
        else:
            cur.execute(
                """
                SELECT id, fact, domains, confidence, source_ts, scope
                FROM arteries.persistent
                WHERE project_id = %s
                  AND valid_until IS NULL
                ORDER BY source_ts DESC
                LIMIT %s
                """,
                (project_id, limit),
            )
        return [dict(r) for r in cur.fetchall()]


def get_persistent_by_relevance(
    project_id: str,
    query_embedding: list[float],
    limit: int = 20,
    threshold: float = 0.3,
) -> list[dict[str, Any]]:
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, fact, domains, confidence, source_ts,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM arteries.persistent
            WHERE project_id = %s
              AND valid_until IS NULL
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> %s::vector) >= %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (query_embedding, project_id, query_embedding, threshold, query_embedding, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def has_embeddings(project_id: str) -> bool:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS(
                SELECT 1 FROM arteries.persistent
                WHERE project_id = %s AND valid_until IS NULL AND embedding IS NOT NULL
            )
            """,
            (project_id,),
        )
        return cur.fetchone()[0]


def insert_persistent(
    project_id: str,
    fact: str,
    domains: list[str],
    confidence: float = 1.0,
    scope: str | None = None,
    embedding: list[float] | None = None,
) -> str:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arteries.persistent
                (fact, embedding, domains, confidence, project_id, scope)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s)
            RETURNING id
            """,
            (
                fact,
                embedding,
                psycopg2.extras.Json(domains),
                confidence,
                project_id,
                scope,
            ),
        )
        conn.commit()
        return str(cur.fetchone()[0])


def update_persistent(
    persistent_id: str,
    project_id: str,
    fact: str | None = None,
    domains: list[str] | None = None,
    confidence: float | None = None,
) -> bool:
    sets, params = [], []
    if fact is not None:
        sets.append("fact = %s")
        params.append(fact)
    if domains is not None:
        sets.append("domains = %s::jsonb")
        params.append(psycopg2.extras.Json(domains))
    if confidence is not None:
        sets.append("confidence = %s")
        params.append(confidence)
    if not sets:
        return False
    params.extend([persistent_id, project_id])
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE arteries.persistent SET {', '.join(sets)} WHERE id = %s AND project_id = %s AND valid_until IS NULL",
            params,
        )
        conn.commit()
        return cur.rowcount > 0


def remove_persistent(persistent_id: str, project_id: str) -> bool:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE arteries.persistent SET valid_until = now() WHERE id = %s AND project_id = %s AND valid_until IS NULL",
            (persistent_id, project_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_active_domains(project_id: str) -> list[str]:
    """Domains from recent persistent memories — proxy for what the user is working on."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT d.value
            FROM arteries.persistent,
                 jsonb_array_elements_text(domains) AS d(value)
            WHERE project_id = %s
              AND valid_until IS NULL
              AND source_ts > now() - INTERVAL '24 hours'
            """,
            (project_id,),
        )
        return [r[0] for r in cur.fetchall()]


# -- Evergreen ----------------------------------------------------------------

def get_evergreen(limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, fact, domains, confidence, source_ts, source_meta
            FROM arteries.evergreen
            WHERE superseded_by IS NULL
            ORDER BY access_count DESC, source_ts DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def touch_evergreen(ids: list[str]) -> None:
    """Bump access_count for evergreen rows surfaced into a frame.

    get_evergreen orders by access_count DESC, but nothing incremented it, so the
    reinforcement was dead and ordering collapsed to source_ts. This closes that:
    a fact that keeps getting surfaced floats up. Best-effort — never breaks the
    read path that calls it.
    # ponytail: counts surfacings, not usefulness — a usefulness signal (e.g.
    # from arteries.rewards) would be better but is deferred. Rich-get-richer is
    # bounded by the limit on how many rows a frame ever surfaces.
    """
    if not ids:
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE arteries.evergreen SET access_count = access_count + 1 "
                "WHERE id = ANY(%s::uuid[])",
                (ids,),
            )
            conn.commit()
    except Exception:
        pass


def insert_evergreen(
    fact: str,
    domains: list[str],
    confidence: float = 1.0,
    parent_ids: list[str] | None = None,
    embedding: list[float] | None = None,
    source_meta: dict[str, Any] | None = None,
) -> str:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arteries.evergreen
                (fact, embedding, domains, confidence, parent_ids, source_meta)
            VALUES (%s, %s, %s::jsonb, %s, %s::uuid[], %s::jsonb)
            RETURNING id
            """,
            (
                fact,
                embedding,
                psycopg2.extras.Json(domains),
                confidence,
                parent_ids or [],
                psycopg2.extras.Json(source_meta or {}),
            ),
        )
        conn.commit()
        return str(cur.fetchone()[0])


def update_evergreen(
    evergreen_id: str,
    fact: str | None = None,
    domains: list[str] | None = None,
    confidence: float | None = None,
) -> bool:
    sets, params = [], []
    if fact is not None:
        sets.append("fact = %s")
        params.append(fact)
    if domains is not None:
        sets.append("domains = %s::jsonb")
        params.append(psycopg2.extras.Json(domains))
    if confidence is not None:
        sets.append("confidence = %s")
        params.append(confidence)
    if not sets:
        return False
    params.append(evergreen_id)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE arteries.evergreen SET {', '.join(sets)} WHERE id = %s AND superseded_by IS NULL",
            params,
        )
        conn.commit()
        return cur.rowcount > 0


def remove_evergreen(evergreen_id: str) -> bool:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM arteries.evergreen WHERE id = %s",
            (evergreen_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def get_recurring_domains() -> list[str]:
    """Domains that appear in evergreen — user's cross-project patterns."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.value, COUNT(*) AS cnt
            FROM arteries.evergreen,
                 jsonb_array_elements_text(domains) AS d(value)
            WHERE superseded_by IS NULL
            GROUP BY d.value
            ORDER BY cnt DESC
            LIMIT 10
            """,
        )
        return [r[0] for r in cur.fetchall()]


# -- Retrievals ---------------------------------------------------------------

def get_recent_retrievals(
    project_id: str,
    agent_process_id: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT prompt_id, situation, score, relevance, created_at
            FROM arteries.retrievals
            WHERE project_id = %s
              AND agent_process_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (project_id, agent_process_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def log_retrieval(
    project_id: str,
    agent_process_id: str,
    prompt_id: str,
    situation: str,
    score: float,
) -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arteries.retrievals
                (project_id, agent_process_id, prompt_id, situation, score)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (project_id, agent_process_id, prompt_id, situation, score),
        )
        conn.commit()
