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

from arteries import degrade, normalize, runlog, scope as scope_mod
from arteries.config import DB_CONFIG
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
                   task_id, compiled_at, seen_count
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
    # Reads the new home, falls back to the old column while it still exists.
    # Both, because `main` writes `scope` and nothing else for as long as it runs
    # the old code -- dropping the fallback before then would hide its rows
    # rather than migrate them.
    # The column is gone (migration 016). The fallback that read it had to be
    # removed *before* the drop, not after -- naming a dropped column is an
    # immediate UndefinedColumn on every read, which is what happened.
    origin_filter = "AND p.source_meta->>'origin' = %(origin)s" if scope else ""
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            SCOPE_CTE + f"""
            SELECT p.id, p.fact, p.domains, p.confidence, p.source_ts,
                   p.source_meta->>'origin' AS scope, p.project_id,
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


def get_persistent_by_kind(
    project_id: str,
    kinds: tuple[str, ...],
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Live persistent rows of the given kinds, newest first. Enumerated, not
    ranked -- for continuity-packet fields (constraints, decisions) that ask
    "what do we hold of this kind", not "what matches this query"
    (planning/compaction_v3.md §3)."""
    with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            SCOPE_CTE + """
            SELECT p.id, p.fact, p.domains, p.confidence, p.kind, p.source_ts
            FROM arteries.persistent p
            WHERE p.project_id IN (SELECT project_id FROM scope)
              AND p.valid_until IS NULL
              AND p.kind = ANY(%(kinds)s)
            ORDER BY p.source_ts DESC
            LIMIT %(limit)s
            """,
            {"project": project_id, "kinds": list(kinds), "limit": limit},
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
    episode_id: str | None = None,
    task_id: str | None = None,
    kind: str = "fact",
) -> str:
    # episode_id/task_id are columns the table has always had and nothing set:
    # evergreen.candidates selects them, retrieval can filter on them, and every
    # row written through here carried NULL. A fact that came out of a run
    # should say which run.
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arteries.persistent
                (fact, embedding, domains, confidence, project_id, source_meta,
                 episode_id, task_id, kind)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING id
            """,
            (
                fact,
                embedding,
                psycopg2.extras.Json(domains),
                confidence,
                project_id,
                # `scope` is still the parameter name because callers use it --
                # `art remember --scope user`. Only its storage moved.
                psycopg2.extras.Json({**(source_meta or {}),
                                      **({"origin": scope} if scope else {})}),
                episode_id,
                task_id,
                kind,
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
                  agent_process_id: str | None = None,
                  previous_id: str | None = None,
                  covers_from: Any = None,
                  covers_to: Any = None,
                  resume_from: str | None = None,
                  body: str | None = None) -> None:
    """Remember what went into a packet, so the next one can differ from it.

    The chaining columns (`previous_id`, `covers_from`/`covers_to`,
    `resume_from`, `body`) are unused until the renderer split
    (planning/compaction_v3.md §2) writes real values -- `record_packet` just
    accepts and stores them from here on so that landing costs no second
    migration and no signature change.

    Best effort: a packet that fails to record itself is a packet the next turn
    repeats, which is the old behaviour and not worth failing a turn over.

    A state packet (planning/compaction_v3.md §2) has no ranked members -- its
    fields are enumerated, not selected -- so `body` alone is enough reason to
    record the row; chaining needs the row to exist even when member_ids is
    empty.
    """
    if not member_ids and not body:
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO arteries.packets "
                "(project_id, session_id, agent_process_id, member_ids, "
                " previous_id, covers_from, covers_to, resume_from, body) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (project_id,
                 session_id if session_id is not None else _env_session_id(),
                 agent_process_id, member_ids,
                 previous_id, covers_from, covers_to, resume_from, body),
            )
            conn.commit()
    except Exception as exc:
        degrade.note(exc, "packet chaining")


def latest_packet(project_id: str, session_id: str | None = None) -> dict[str, Any] | None:
    """Most recent packet for this (project, session), or None on a cold
    start. `covers_to` is what the next packet's `covers_from` chains against
    (planning/compaction_v3.md §5)."""
    session_id = session_id if session_id is not None else _env_session_id()
    if not session_id:
        return None
    try:
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, covers_to FROM arteries.packets "
                "WHERE project_id = %s AND session_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (project_id, session_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as exc:
        degrade.note(exc, "packet history")
        return None


def tool_results_since(project_id: str, session_id: str | None,
                       since: Any, until: Any) -> list[dict[str, Any]]:
    """`tool.result` events (hooks/arteries-tool.js) in (since, until] for this
    session. Joins through agent_runs because the event carries `run_id`, not
    `session_id` -- `runlog._session_run` uses the same join.

    No session known -> no events, rather than every session's: a state field
    scoped to "everyone, ever" is not a continuity field."""
    if not session_id:
        return []
    try:
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT e.payload, e.created_at
                FROM arteries.agent_events e
                JOIN arteries.agent_runs r ON r.id = e.run_id
                WHERE e.project_id = %s
                  AND r.metadata->>'session_id' = %s
                  AND e.event_type = 'tool.result'
                  AND e.created_at > %s AND e.created_at <= %s
                ORDER BY e.created_at
                """,
                (project_id, session_id, since, until),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        degrade.note(exc, "tool result window")
        return []


def recent_supersede_edges(project_id: str, since: Any, limit: int = 20) -> list[dict[str, Any]]:
    """`supersedes`/`contradicts` edges over persistent rows, written at
    promotion (compile.py) for previous sessions -- detector 1 of
    planning/compaction_v3.md §4.2. `since` bounds it to edges new since the
    last packet, which is what makes a retraction line render once rather than
    resurfacing every compaction for the rest of the project's life."""
    try:
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT e.rel, e.metadata, e.created_at,
                       new_p.fact AS new_fact, old_p.fact AS old_fact
                FROM arteries.memory_edges e
                JOIN arteries.persistent new_p ON new_p.id::text = e.src_id
                JOIN arteries.persistent old_p ON old_p.id::text = e.dst_id
                WHERE e.project_id = %s
                  AND e.src_kind = 'persistent' AND e.dst_kind = 'persistent'
                  AND e.rel IN ('supersedes', 'contradicts')
                  AND e.created_at > %s
                ORDER BY e.created_at DESC
                LIMIT %s
                """,
                (project_id, since, limit),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        degrade.note(exc, "supersede edges")
        return []


def near_duplicate_ephemeral_pairs(project_id: str, session_id: str | None,
                                   threshold: float = 0.85,
                                   limit: int = 20) -> list[dict[str, Any]]:
    """Pairs of this session's ephemeral atoms similar enough that a differing
    literal between them is a value overwrite rather than two unrelated facts
    (planning/compaction_v3.md §4.2 detector 3). Cosine in SQL rather than
    fetching embeddings into Python -- pgvector already does this comparison
    for every other ranked query here."""
    if not session_id:
        return []
    try:
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT a.fact AS fact_a, a.source_ts AS ts_a, a.source AS source_a,
                       b.fact AS fact_b, b.source_ts AS ts_b, b.source AS source_b,
                       1 - (a.embedding <=> b.embedding) AS similarity
                FROM arteries.ephemeral a
                JOIN arteries.ephemeral b ON a.id < b.id
                WHERE a.project_id = %(project)s AND b.project_id = %(project)s
                  AND a.session_id = %(session)s AND b.session_id = %(session)s
                  AND a.valid_until IS NULL AND b.valid_until IS NULL
                  AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
                  AND 1 - (a.embedding <=> b.embedding) >= %(threshold)s
                ORDER BY similarity DESC
                LIMIT %(limit)s
                """,
                {"project": project_id, "session": session_id,
                 "threshold": threshold, "limit": limit},
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        degrade.note(exc, "near-duplicate ephemeral pairs")
        return []


def open_episodes(project_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Episodes still `running` for this project -- `state.in_progress`."""
    try:
        with _conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, agent_id, task_id, created_at FROM arteries.episodes "
                "WHERE project_id = %s AND status = 'running' "
                "ORDER BY created_at DESC LIMIT %s",
                (project_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        degrade.note(exc, "open episodes")
        return []


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
