"""
Storage layer for arteries memory tiers.

All three tiers live in the arteries schema of the shared capillaries
Postgres instance. Queries return dicts — the frame module converts
them to MemoryFrame types.
"""

from __future__ import annotations

import os
from typing import Any

import psycopg2
import psycopg2.extras

from arteries.config import DB_CONFIG
from arteries import normalize
from arteries.scope import SCOPE_CTE


def _env_episode_id() -> str | None:
    """Same two variables actionlog and journal already read. Read here rather
    than imported from actionlog, which imports journal, which would make the
    lowest layer depend on two above it."""
    return os.getenv("ARTERIES_EPISODE_ID") or None


def _env_task_id() -> str | None:
    return os.getenv("ARTERIES_TASK_ID") or None


def _env_session_id() -> str | None:
    """The session this turn belongs to.

    `cli_normalize.apply_event_env` has exported this all along; nothing in the
    memory tiers read it. It is the key `agent_process_id` should have been:
    stable across a session's turns, and still meaningful once the process that
    wrote the row is gone.
    """
    return os.getenv("ARTERIES_SESSION_ID") or None

# Confidence is read back as stored. Age-based decay lived here and was removed —
# age alone was the wrong signal (a stale-but-still-true fact decayed like a
# wrong one, and a re-confirmed fact didn't recover). A usefulness-driven method
# will replace it later; until then, no decay.


def _conn():
    return psycopg2.connect(**DB_CONFIG)


# -- Ephemeral ----------------------------------------------------------------

# How long a row stays in the working set. Not a compile status: a promoted fact
# is still the thing this session was just talking about, and hiding it the
# instant it reaches persistent is what made in-session recall work only while
# the compiler was broken (finding 8).
#
# 48h rather than 24: a session gets returned to across a couple of days. Beyond
# that the context is stale on any clock, and persistent has it anyway.
EPHEMERAL_VISIBLE_HOURS = int(os.getenv("ARTERIES_EPHEMERAL_VISIBLE_HOURS", "48"))

# One rule, used by every read of the tier. Two rules is how the coverage gate
# and the frame ended up disagreeing about what "in the working set" means.
_VISIBLE = """
    valid_until IS NULL
    AND source_ts > now() - (%(visible_hours)s || ' hours')::interval
"""

def get_ephemeral(
    project_id: str,
    agent_process_id: str,
    limit: int = 50,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """This session's working set, falling back to this process's.

    Keyed on the session where one is known. `agent_process_id` defaults to the
    pid, so a row written by a process that has since exited was reachable by
    nothing -- 84 rows in the live store, 83 distinct ids, one row each. Matching
    on the session recovers them, because the session outlives the turn.

    The process clause stays as an OR rather than being replaced: rows written
    before this column existed have a NULL session_id, and `main` writes NULL
    for as long as it runs. Dropping the fallback would hide them.
    """
    session_id = session_id if session_id is not None else _env_session_id()
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, fact, domains, source_ts, status, source, episode_id,
                   task_id, compiled_at
            FROM arteries.ephemeral
            WHERE project_id = %(project)s
              AND (agent_process_id = %(agent)s
                   OR (%(session)s::text IS NOT NULL AND session_id = %(session)s))
              AND """ + _VISIBLE + """
            ORDER BY source_ts DESC
            LIMIT %(limit)s
            """,
            {"project": project_id, "agent": agent_process_id, "session": session_id,
             "limit": limit, "visible_hours": EPHEMERAL_VISIBLE_HOURS},
        )
        return [dict(r) for r in cur.fetchall()]


def insert_ephemeral(
    project_id: str,
    agent_process_id: str,
    fact: str,
    domains: list[str],
    parent_agent_id: str | None = None,
    embedding: list[float] | None = None,
    source: str = "user",
    episode_id: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
) -> str:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arteries.ephemeral
                (fact, embedding, domains, project_id,
                 agent_process_id, parent_agent_id, source, episode_id, task_id,
                 session_id, fact_hash, last_seen)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, now())
            -- The dedupe. Two sessions racing the same sentence resolve inside
            -- idx_eph_dedupe: no lock, no read-then-write, and the loser gets a
            -- counter bump rather than a second row. The predicate has to repeat
            -- the index's own, or Postgres cannot tell which index arbitrates.
            ON CONFLICT (project_id, coalesce(session_id, ''), fact_hash)
                WHERE valid_until IS NULL AND fact_hash IS NOT NULL
            DO UPDATE SET seen_count = arteries.ephemeral.seen_count + 1,
                          last_seen  = now()
            RETURNING id
            """,
            (
                fact,
                embedding,
                psycopg2.extras.Json(domains),
                project_id,
                agent_process_id,
                parent_agent_id,
                source,
                episode_id if episode_id is not None else _env_episode_id(),
                task_id if task_id is not None else _env_task_id(),
                session_id if session_id is not None else _env_session_id(),
                normalize.fact_hash(fact),
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
    """Live persistent memories for this project's whole scope, newest first."""
    origin_filter = "AND p.scope = %(origin)s" if scope else ""
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            SCOPE_CTE + f"""
            SELECT p.id, p.fact, p.domains, p.confidence, p.source_ts, p.scope, p.project_id,
                   p.episode_id, p.task_id
            FROM arteries.persistent p
            WHERE p.project_id IN (SELECT project_id FROM scope)
              AND p.valid_until IS NULL
              {origin_filter}
            ORDER BY p.source_ts DESC
            LIMIT %(limit)s
            """,
            {"project": project_id, "origin": scope, "limit": limit},
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
                   p.episode_id, p.task_id,
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


def _or_tsquery(cur, query: str) -> str:
    """Turn a message into a tsquery, quoting every token.

    Ported from capillaries, which learned it the hard way: an unquoted token
    carrying punctuation -- `textkit/__init__.py`, `slugify('')`, a bare `&` --
    is read as tsquery *syntax*, and the whole search dies with "syntax error in
    tsquery". Agent prompts are made of such tokens, so that is the common case
    rather than an edge one.

    Pure-punctuation tokens are dropped: quoted, they produce empty lexemes and
    Postgres rejects those too. An empty result means "skip sparse", and dense
    retrieval still stands on its own.
    """
    cur.execute(
        "SELECT array_to_string("
        "  array_agg(DISTINCT quote_literal(token)), ' | '"
        ") FROM ts_parse('default', %s) "
        "WHERE tokid != 12 "              # whitespace
        "  AND token ~ '[[:alnum:]]'",    # anything with no lexeme in it
        [query],
    )
    return cur.fetchone()[0] or ""


def get_persistent_by_text(project_id: str, query: str,
                           limit: int = 20) -> list[dict[str, Any]]:
    """Lexical retrieval over the same rows the cosine query reads.

    The channel dense retrieval cannot provide: an exact identifier. A query
    naming `UndefinedColumn` or `EMBED_DIM` matches the row containing it, where
    an embedding puts both into the same technical-prose band as everything else.
    """
    with _conn() as conn:
        # A plain cursor to build the tsquery. `_or_tsquery` reads column 0, and
        # a RealDictCursor returns a dict, so sharing one cursor between the two
        # raised KeyError: 0 on every query -- caught by `degrade` as a BUG
        # rather than an outage, which is the distinction that module is for.
        with conn.cursor() as plain:
            terms = _or_tsquery(plain, query)
        if not terms:
            return []
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                SCOPE_CTE + """
                SELECT p.id, p.fact, p.domains, p.confidence, p.source_ts,
                       p.project_id, p.episode_id, p.task_id,
                       ts_rank_cd(p.search_tsv,
                                  to_tsquery('english', %(terms)s), 1|4|32)
                           AS lexical_rank
                FROM arteries.persistent p
                WHERE p.project_id IN (SELECT project_id FROM scope)
                  AND p.valid_until IS NULL
                  AND p.search_tsv @@ to_tsquery('english', %(terms)s)
                ORDER BY lexical_rank DESC
                LIMIT %(limit)s
                """,
                {"terms": terms, "project": project_id, "limit": limit},
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
    scope: str | None = None,
    embedding: list[float] | None = None,
    source_meta: dict[str, Any] | None = None,
) -> str:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arteries.persistent
                (fact, embedding, domains, confidence, project_id, scope, source_meta)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (
                fact,
                embedding,
                psycopg2.extras.Json(domains),
                confidence,
                project_id,
                scope,
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
            SELECT coalesce(max(1 - (embedding <=> %(q)s::vector)), 0.0)
            FROM arteries.ephemeral
            WHERE project_id = %(project)s
              AND agent_process_id = %(agent)s
              AND embedding IS NOT NULL
              AND """ + _VISIBLE + """
            """,
            {"q": query_embedding, "project": project_id, "agent": agent_process_id,
             "visible_hours": EPHEMERAL_VISIBLE_HOURS},
        )
        return float(cur.fetchone()[0])


def get_evergreen_by_relevance(project_id: str, query_embedding: list[float],
                               limit: int = 10,
                               threshold: float = 0.0) -> list[dict[str, Any]]:
    """Cosine-ranked evergreen claims for this project's scope group.

    The third retrieval arm. Evergreen is scope-wide, so this is the only read
    that can surface a constraint recorded in arteries while someone is working
    in heart.
    """
    from arteries import scope as scope_mod

    scope_id = scope_mod.scope_for(project_id) or project_id
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, fact, domains, confidence, kind, core, incrementality,
                   episode_id, task_id, valid_from AS source_ts,
                   1 - (embedding <=> %(q)s::vector) AS similarity
            FROM arteries.evergreen
            WHERE scope_id = %(scope)s
              AND valid_until IS NULL
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> %(q)s::vector) >= %(threshold)s
            ORDER BY
              -- Core first at equal relevance: a project's own specification
              -- outranks a claim that merely scored well against it.
              core DESC, embedding <=> %(q)s::vector
            LIMIT %(limit)s
            """,
            {"q": query_embedding, "scope": scope_id,
             "threshold": threshold, "limit": limit},
        )
        return [dict(r) for r in cur.fetchall()]


def record_packet(project_id: str, member_ids: list[str],
                  session_id: str | None = None,
                  agent_process_id: str | None = None) -> None:
    """Remember what went into a packet, so the next one can differ from it.

    Best effort: a packet that fails to record itself is a packet the next turn
    repeats, which is the old behaviour and not worth failing a turn over.
    """
    if not member_ids:
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO arteries.packets "
                "(project_id, session_id, agent_process_id, member_ids) "
                "VALUES (%s, %s, %s, %s)",
                (project_id,
                 session_id if session_id is not None else _env_session_id(),
                 agent_process_id, member_ids),
            )
            conn.commit()
    except Exception as exc:
        from arteries import degrade

        degrade.note(exc, "packet chaining")


def recent_packet_members(project_id: str, session_id: str | None = None,
                          packets: int = 2) -> set[str]:
    """Ids surfaced by the last few packets of this session.

    Bounded to the last two rather than the whole session: a claim shown once is
    worth not repeating immediately, and a claim shown twenty turns ago is worth
    showing again if it is still the best answer. Forgetting is the feature.
    """
    session_id = session_id if session_id is not None else _env_session_id()
    if not session_id:
        return set()
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT member_ids FROM arteries.packets
                WHERE project_id = %s AND session_id = %s
                ORDER BY created_at DESC LIMIT %s
                """,
                (project_id, session_id, packets),
            )
            return {member for (row,) in cur.fetchall() for member in (row or [])}
    except Exception as exc:
        from arteries import degrade

        degrade.note(exc, "packet history")
        return set()


def get_corpus_suggestion(project_id: str, key: str,
                          max_age_seconds: int) -> dict[str, Any] | None:
    """The cached suggestion for this question, if it is still fresh.

    Stored in `agent_events` rather than a table of its own. It is a cache with
    one row per question and a fifteen-minute life; a table, an index and a
    migration for that would be three things to maintain in exchange for
    nothing.
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload FROM arteries.agent_events
            WHERE event_type = 'corpus.suggestion.cached'
              AND project_id = %s
              AND payload->>'key' = %s
              AND created_at > now() - (%s || ' seconds')::interval
            ORDER BY created_at DESC LIMIT 1
            """,
            (project_id, key, max_age_seconds),
        )
        row = cur.fetchone()
    return (row[0] or {}).get("suggestion") if row else None


def put_corpus_suggestion(project_id: str, key: str, suggestion: dict) -> None:
    from arteries import runlog

    runlog.log_event("corpus.suggestion.cached", "arteries",
                     {"key": key, "suggestion": suggestion},
                     project_id=project_id)


def get_evergreen_count(project_id: str) -> int:
    """How many live evergreen rows this project's scope can see.

    `scripts/watch.sh` has called `storage.get_evergreen` since before the tier
    existed; the call sat behind a `2>/dev/null || echo "(db unavailable)"` and
    so reported a missing function as a missing database for months. A count is
    what the watch actually wanted.
    """
    from arteries import scope as scope_mod

    scope_id = scope_mod.scope_for(project_id) or project_id
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM arteries.evergreen "
            "WHERE scope_id = %s AND valid_until IS NULL",
            (scope_id,),
        )
        return int(cur.fetchone()[0])


def touch_persistent(ids: list[str]) -> None:
    """Bump access_count for claims surfaced into a frame.

    The only usage signal in the store. Without it every claim looks equally
    unused and nothing can be pruned by usefulness -- which is the state this
    branch was accidentally in after the column's writer was removed with the
    evergreen tier, leaving a comment in frame.py claiming otherwise.

    Best effort: reinforcement must never break a read.

    ponytail: counts surfacings, not usefulness. Outcome-weighted value needs
    the reward ledger, which is still empty.
    """
    if not ids:
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            # The activity day comes along, because "unread for 30 days" needs
            # to know when the row was last read, and this is the only place
            # that knows. Counted in one statement so a surfaced row cannot get
            # one half of the update and not the other.
            cur.execute(
                """
                UPDATE arteries.persistent p
                SET access_count = p.access_count + 1,
                    last_activity_day = (
                        SELECT count(*) FROM arteries.project_activity a
                        WHERE a.project_id = p.project_id)
                WHERE p.id = ANY(%s::uuid[])
                """,
                (ids,),
            )
            conn.commit()
    except Exception as exc:
        from arteries import degrade
        degrade.note(exc, "access_count reinforcement")


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
