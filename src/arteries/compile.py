"""
Async-track compilation: ephemeral → persistent.

Fires in the background after each turn. Reads uncompiled ephemeral records,
sends them to an LLM with existing persistent records as context, and writes
distilled persistent memories with contradiction resolution.

Lifecycle: uncompiled → compiling → cleared (ephemeral status field).
On failure: compiling → uncompiled (retry on next pass).

The LLM handles what heuristics can't:
- Multi-turn reasoning across ephemeral records
- Implied context extraction
- Contradiction detection against existing persistent memory
- Deduplication of semantically equivalent facts
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import psycopg2
import psycopg2.extras

from arteries import runlog
from arteries.config import AGENT_PROCESS_ID, COMPILE_MODEL, DB_CONFIG, GENERATE_URL, PROJECT_ID

MAX_EPHEMERAL_BATCH = 20
MAX_PERSISTENT_CONTEXT = 15
STALE_CLAIM_MINUTES = 2

COMPILE_SYSTEM = """You are a memory compiler. You receive raw conversation extracts (ephemeral memories) and existing long-term memories (persistent). Your job:

1. Distill the ephemeral records into concise, factual statements worth remembering long-term.
2. Tag each with relevant domains from this list: technical, AI, business, strategy, product, finance, career, learning, personal, writing.
3. If a new fact contradicts an existing persistent memory, mark the old one as superseded and note which new fact replaces it.
4. Deduplicate — if an ephemeral record says the same thing as an existing persistent memory, skip it.
5. Assign confidence 0.0-1.0 based on how certain the fact is (corrections and explicit statements = high, inferred context = lower).

Respond with JSON only:
{
  "new_memories": [
    {"fact": "...", "domains": ["..."], "confidence": 0.9}
  ],
  "superseded": [
    {"persistent_id": "uuid", "reason": "replaced by: ..."}
  ],
  "skipped": ["reason ephemeral record N was not worth keeping"]
}

Be aggressive about filtering. Only keep facts that would be useful in a future conversation about this project. Skip greetings, transient debugging steps, and anything too vague to act on."""


async def compile_once() -> dict[str, Any]:
    """
    Run one compilation pass. Returns stats about what happened.

    Safe to call concurrently — uses SELECT FOR UPDATE SKIP LOCKED
    to claim ephemeral records, so parallel agents won't double-compile.
    """
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        _release_stale_claims(conn)
        claimed = _claim_ephemeral(conn)
        if not claimed:
            return {"status": "nothing_to_compile", "claimed": 0}

        persistent_context = _load_persistent_context(conn)

        try:
            result = await _llm_compile(claimed, persistent_context)
        except asyncio.CancelledError:
            _release_claimed(conn, [r["id"] for r in claimed])
            raise
        except Exception as e:
            _release_claimed(conn, [r["id"] for r in claimed])
            stats = {"status": "llm_error", "error": str(e), "claimed": len(claimed)}
            runlog.log_failure("memory.compile.failed", "arteries", e, project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID)
            runlog.log_event("memory.compile.completed", "arteries", stats, project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID)
            return stats

        written = _write_results(conn, result, [r["id"] for r in claimed])
        stats = {
            "status": "compiled",
            "claimed": len(claimed),
            "new_persistent": written["new"],
            "superseded": written["superseded"],
        }
        runlog.log_event("memory.compile.completed", "arteries", stats, project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID)
        return stats
    finally:
        conn.close()


def _release_stale_claims(conn) -> int:
    """Return old compiling records to uncompiled after a cancelled worker."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE arteries.ephemeral
            SET status = 'uncompiled'
            WHERE project_id = %s
              AND agent_process_id = %s
              AND status = 'compiling'
              AND source_ts < now() - (%s || ' minutes')::interval
            """,
            (PROJECT_ID, AGENT_PROCESS_ID, STALE_CLAIM_MINUTES),
        )
        released = cur.rowcount
        conn.commit()
        return released


def _claim_ephemeral(conn) -> list[dict]:
    """Atomically claim uncompiled ephemeral records for this compilation pass."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE arteries.ephemeral
            SET status = 'compiling'
            WHERE id IN (
                SELECT id FROM arteries.ephemeral
                WHERE project_id = %s
                  AND agent_process_id = %s
                  AND status = 'uncompiled'
                ORDER BY source_ts ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, fact, domains, confidence, source_ts
            """,
            (PROJECT_ID, AGENT_PROCESS_ID, MAX_EPHEMERAL_BATCH),
        )
        conn.commit()
        return [dict(r) for r in cur.fetchall()]


def _release_claimed(conn, ids: list) -> None:
    """Roll back claimed records to uncompiled on failure."""
    if not ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE arteries.ephemeral SET status = 'uncompiled' WHERE id = ANY(%s::uuid[])",
            (ids,),
        )
        conn.commit()


def _load_persistent_context(conn) -> list[dict]:
    """Load recent persistent memories for contradiction detection."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, fact, domains, confidence, source_ts
            FROM arteries.persistent
            WHERE project_id = %s AND valid_until IS NULL
            ORDER BY source_ts DESC
            LIMIT %s
            """,
            (PROJECT_ID, MAX_PERSISTENT_CONTEXT),
        )
        return [dict(r) for r in cur.fetchall()]


async def _llm_compile(
    ephemeral: list[dict],
    persistent: list[dict],
) -> dict:
    """Call the LLM to compile ephemeral records into persistent memories."""
    eph_text = "\n".join(
        f"[{i+1}] (conf={r['confidence']}, domains={r['domains']}) {r['fact']}"
        for i, r in enumerate(ephemeral)
    )

    per_text = "None yet." if not persistent else "\n".join(
        f"[{r['id']}] (domains={r['domains']}) {r['fact']}"
        for r in persistent
    )

    prompt = f"""Ephemeral records to compile:
{eph_text}

Existing persistent memories:
{per_text}"""

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GENERATE_URL,
            json={
                "model": COMPILE_MODEL,
                "messages": [
                    {"role": "system", "content": COMPILE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    return json.loads(content)


def _write_results(conn, result: dict, claimed_ids: list) -> dict[str, int]:
    """Write compiled persistent records and mark ephemeral as cleared."""
    from arteries.embed import embed_text_sync

    new_count = 0
    superseded_count = 0

    with conn.cursor() as cur:
        for mem in result.get("new_memories", []):
            cur.execute(
                """
                INSERT INTO arteries.persistent
                    (fact, domains, confidence, project_id, parent_ids)
                VALUES (%s, %s::jsonb, %s, %s, %s::uuid[])
                RETURNING id
                """,
                (
                    mem["fact"],
                    json.dumps(mem.get("domains", [])),
                    mem.get("confidence", 0.8),
                    PROJECT_ID,
                    claimed_ids,
                ),
            )
            row_id = cur.fetchone()[0]
            vec = embed_text_sync(mem["fact"])
            if vec:
                cur.execute(
                    "UPDATE arteries.persistent SET embedding = %s::vector WHERE id = %s",
                    (vec, row_id),
                )
            new_count += 1

        for sup in result.get("superseded", []):
            pid = sup.get("persistent_id")
            if pid:
                cur.execute(
                    """
                    UPDATE arteries.persistent
                    SET valid_until = now()
                    WHERE id = %s AND project_id = %s AND valid_until IS NULL
                    """,
                    (pid, PROJECT_ID),
                )
                superseded_count += cur.rowcount

        cur.execute(
            "UPDATE arteries.ephemeral SET status = 'cleared' WHERE id = ANY(%s::uuid[])",
            (claimed_ids,),
        )
        conn.commit()

    return {"new": new_count, "superseded": superseded_count}


async def compile_loop(max_passes: int = 5) -> list[dict]:
    """Run compilation passes until nothing left or max_passes reached."""
    results = []
    for _ in range(max_passes):
        r = await compile_once()
        results.append(r)
        if r["status"] == "nothing_to_compile":
            break
    return results
