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
from arteries.scope import SCOPE_CTE

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
    origin: str | None = None,
) -> list[dict[str, Any]]:
    """Live persistent memories for this project's whole scope, newest first."""
    origin_filter = "AND p.origin = %(origin)s" if origin else ""
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            SCOPE_CTE + f"""
            SELECT p.id, p.fact, p.domains, p.confidence, p.source_ts, p.origin, p.project_id
            FROM arteries.persistent p
            WHERE p.project_id IN (SELECT project_id FROM scope)
              AND p.valid_until IS NULL
              {origin_filter}
            ORDER BY p.source_ts DESC
            LIMIT %(limit)s
            """,
            {"project": project_id, "origin": origin, "limit": limit},
        )
        return [dict(r) for r in cur.fetchall()]


def get_persistent_by_relevance(
    project_id: str,
    query_embedding: list[float],
    limit: int = 20,
    threshold: float = 0.3,
) -> list[dict[str, Any]]:
    """Cosine-ranked persistent memories across this project's scope."""
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            SCOPE_CTE + """
            SELECT p.id, p.fact, p.domains, p.confidence, p.source_ts, p.project_id,
                   1 - (p.embedding <=> %(q)s::vector) AS similarity
            FROM arteries.persistent p
            WHERE p.project_id IN (SELECT project_id FROM scope)
              AND p.valid_until IS NULL
              AND p.embedding IS NOT NULL
              AND 1 - (p.embedding <=> %(q)s::vector) >= %(threshold)s
            ORDER BY p.embedding <=> %(q)s::vector
            LIMIT %(limit)s
            """,
            {"q": query_embedding, "project": project_id,
             "threshold": threshold, "limit": limit},
        )
        return [dict(r) for r in cur.fetchall()]


def has_embeddings(project_id: str) -> bool:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            SCOPE_CTE + """
            SELECT EXISTS(
                SELECT 1 FROM arteries.persistent
                WHERE project_id IN (SELECT project_id FROM scope)
                  AND valid_until IS NULL AND embedding IS NOT NULL
            )
            """,
            {"project": project_id},
        )
        return cur.fetchone()[0]


def insert_persistent(
    project_id: str,
    fact: str,
    domains: list[str],
    confidence: float = 1.0,
    origin: str | None = None,
    embedding: list[float] | None = None,
    source_meta: dict[str, Any] | None = None,
) -> str:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arteries.persistent
                (fact, embedding, domains, confidence, project_id, origin, source_meta)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (
                fact,
                embedding,
                psycopg2.extras.Json(domains),
                confidence,
                project_id,
                origin,
                psycopg2.extras.Json(source_meta or {}),
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
            SCOPE_CTE + """
            SELECT DISTINCT d.value
            FROM arteries.persistent p,
                 jsonb_array_elements_text(p.domains) AS d(value)
            WHERE p.project_id IN (SELECT project_id FROM scope)
              AND p.valid_until IS NULL
              AND p.source_ts > now() - INTERVAL '24 hours'
            """,
            {"project": project_id},
        )
        return [r[0] for r in cur.fetchall()]


def max_ephemeral_similarity(
    project_id: str,
    agent_process_id: str,
    query_embedding: list[float],
) -> float:
    """How well this turn is already covered by the current session's memory.

    Feeds the retrieval gate: if the situation is one we already have context
    for, calling capillaries again is wasted work. Returns 0.0 when nothing is
    embedded yet, which reads as "no coverage" and keeps retrieval on.
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT coalesce(max(1 - (embedding <=> %s::vector)), 0.0)
            FROM arteries.ephemeral
            WHERE project_id = %s
              AND agent_process_id = %s
              AND embedding IS NOT NULL
              AND status <> 'cleared'
            """,
            (query_embedding, project_id, agent_process_id),
        )
        return float(cur.fetchone()[0])


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
