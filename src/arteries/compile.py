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
import os
import uuid
from typing import Any

import httpx
import psycopg2
import psycopg2.extras

from arteries import graph, runlog, scope
from arteries.config import AGENT_PROCESS_ID, COMPILE_MODEL, DB_CONFIG, GENERATE_URL, PROJECT_ID
from arteries.scope import SCOPE_CTE

MAX_EPHEMERAL_BATCH = 10
MAX_PERSISTENT_CONTEXT = 15
STALE_CLAIM_MINUTES = 2

# Compilation is generation-bound, not prompt-bound: a 20-record batch measured
# 0.43s to prefill 5.7k tokens and ~40s to generate 1.3k. The old 30s ceiling
# cut every single pass off mid-generation -- 82 consecutive ReadTimeouts, and
# not one persistent memory ever written. With the batch at 10 and the discard
# rationale gone from the response, a pass generates ~500 tokens (~16s at the
# 32 tok/s a 27B gets across two 3090s). 60s is ~3x that headroom.
# ponytail: if this starts timing out again, the fix is a smaller compile model
# or a smaller batch -- not a bigger number.
COMPILE_TIMEOUT = float(os.getenv("ARTERIES_COMPILE_TIMEOUT", "60"))

COMPILE_SYSTEM = """You are a memory compiler. You receive raw conversation extracts (ephemeral memories) and existing long-term memories (persistent). Your job:

1. Distill the ephemeral records into concise, factual statements worth remembering long-term.
2. Tag each with relevant domains from this list: technical, AI, business, strategy, product, finance, career, learning, personal, writing.
3. If a new fact contradicts an existing persistent memory, mark the old one as superseded. Set "replaced_by" to the 0-based index of the entry in new_memories that replaces it, and give the reason.
4. Deduplicate — if an ephemeral record says the same thing as an existing persistent memory, skip it.
5. Assign confidence 0.0-1.0 based on how certain the fact is (corrections and explicit statements = high, inferred context = lower).

Records marked [SUBAGENT] came from an automated subagent, not directly from the user. Apply a higher bar:
- Only keep subagent records that state verifiable facts about the codebase or project.
- Discard subagent interpretations of user intent ("the user wants X") — only the user's own words count for intent.
- Discard subagent reasoning narration and intermediate conclusions.
- If a subagent record restates something already in persistent memory, skip it even if the wording differs.

Records marked [ASSISTANT] are stripped LLM responses. Extract only:
- Discovered facts about the codebase, environment, or dependencies.
- Decisions with rationale (chose X over Y because Z).
- Root cause diagnoses.
- Constraints or limitations discovered during work.
Discard everything else: narration, restatements of the user's request, code descriptions, and status updates.

For each memory also record:
- "kind": one of fact | decision | preference | constraint.
- "entities": the concrete things it is about, as {"name","kind"} where kind is
  concept, module, or dependency. Name modules and dependencies as they appear
  in the code. Do not invent entities for generic words.
- "relations": how it interacts with the numbered existing memories above, as
  {"persistent_id","rel"} where rel is supports, refines, contradicts, or
  depends_on. Use the id exactly as given; omit the field if nothing relates.
- "decision": only when kind is decision, as {"chose","over":[...],"because"}.

Respond with compact JSON only -- no indentation, no whitespace between keys:
{"new_memories":[{"fact":"...","domains":["..."],"confidence":0.9,"kind":"fact","entities":[{"name":"pgvector","kind":"dependency"}],"relations":[{"persistent_id":"uuid","rel":"refines"}]}],"superseded":[{"persistent_id":"uuid","reason":"replaced by: ...","replaced_by":0}]}

Do not explain what you discarded. Compilation is generation-bound on local
hardware, and prose about rejected records costs more time than the memories
themselves.

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

        persistent_context = _load_persistent_context(conn, claimed)

        try:
            result = await _llm_compile(claimed, persistent_context)
            problems = validate_response(result)
            if problems:
                runlog.log_event("memory.compile.invalid_response", "arteries",
                                 {"problems": problems[:5], "attempt": 1},
                                 project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID)
                result = await _llm_compile(claimed, persistent_context, problems=problems)
                problems = validate_response(result)
                if problems:
                    # Two bad responses is a prompt or model problem, not a blip.
                    # Release the batch rather than writing malformed rows.
                    raise ValueError(f"invalid compile response: {problems[:3]}")
        except asyncio.CancelledError:
            _release_claimed(conn, [r["id"] for r in claimed])
            raise
        except Exception as e:
            _release_claimed(conn, [r["id"] for r in claimed])
            stats = {"status": "llm_error", "error": str(e), "claimed": len(claimed)}
            runlog.log_failure("memory.compile.failed", "arteries", e, project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID)
            runlog.log_event("memory.compile.completed", "arteries", stats, project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID)
            return stats

        try:
            written = _write_results(conn, result, [r["id"] for r in claimed])
        except Exception as e:
            # Previously unguarded. A failure here (most likely an embedding
            # dimension mismatch) aborts the transaction, rolls back the inserts,
            # and used to leave the batch stuck in 'compiling' with no path back.
            conn.rollback()
            _release_claimed(conn, [r["id"] for r in claimed])
            stats = {"status": "write_error", "error": repr(e), "claimed": len(claimed)}
            runlog.log_failure("memory.compile.write_failed", "arteries", e,
                               project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID)
            runlog.log_event("memory.compile.completed", "arteries", stats,
                             project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID)
            return stats
        stats = {
            "status": "compiled",
            "claimed": len(claimed),
            "new_persistent": written["new"],
            "superseded": written["superseded"],
            "duplicates_rejected": written["duplicates_rejected"],
        }
        runlog.log_event("memory.compile.completed", "arteries", stats, project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID)
        return stats
    finally:
        conn.close()


def _release_stale_claims(conn) -> int:
    """Return old compiling records to uncompiled after a cancelled worker."""
    with conn.cursor() as cur:
        # Not scoped to this agent_process_id. A claim is stranded precisely
        # when the process that made it is gone, so scoping the sweep to the
        # caller means nobody ever releases it -- records sat in 'compiling'
        # for days that way. Any live worker sweeps for all of them.
        cur.execute(
            """
            UPDATE arteries.ephemeral
            SET status = 'uncompiled'
            WHERE project_id = %s
              AND status = 'compiling'
              AND source_ts < now() - (%s || ' minutes')::interval
            """,
            (PROJECT_ID, STALE_CLAIM_MINUTES),
        )
        released = cur.rowcount
        conn.commit()
        return released


def _claim_ephemeral(conn) -> list[dict]:
    """Atomically claim uncompiled ephemeral records for this compilation pass.

    Claims both the parent's own records AND any subagent records that
    tagged this agent as their parent_agent_id.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE arteries.ephemeral
            SET status = 'compiling'
            WHERE id IN (
                SELECT id FROM arteries.ephemeral
                WHERE project_id = %s
                  AND (agent_process_id = %s OR parent_agent_id = %s)
                  AND status = 'uncompiled'
                ORDER BY (agent_process_id = %s) DESC, source_ts ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, fact, domains, source_ts, parent_agent_id, source
            """,
            (PROJECT_ID, AGENT_PROCESS_ID, AGENT_PROCESS_ID, AGENT_PROCESS_ID, MAX_EPHEMERAL_BATCH),
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


# Similarity bands for admitting a new fact. The comparison runs over *every*
# live persistent row in scope via the vector index, not a recent slice -- the
# old ORDER BY source_ts DESC LIMIT 15 meant dedupe was blind to anything older
# than the last fifteen facts, which is how a 0.863-cosine duplicate pair got
# into the store.
#
# ponytail: both numbers are guesses calibrated against exactly one observed
# duplicate. Tune them once there is a week of real data.
DUPLICATE_SIM = 0.93   # at or above: mechanical duplicate, never reaches the LLM
RELATED_SIM = 0.75     # between: the LLM decides refine / contradict / distinct


def _load_persistent_context(conn, batch: list[dict] | None = None) -> list[dict]:
    """Existing facts the compiler should compare against.

    Selected by similarity to the batch being compiled rather than by recency,
    so a contradiction of something learned months ago is still visible. Falls
    back to recency when the batch cannot be embedded.
    """
    vec = None
    if batch:
        from arteries.embed import embed_text_sync
        vec = embed_text_sync(" ".join(r["fact"] for r in batch)[:4000])

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if vec:
            cur.execute(
                SCOPE_CTE + """
                SELECT p.id, p.fact, p.domains, p.confidence, p.source_ts,
                       1 - (p.embedding <=> %(q)s::vector) AS similarity
                FROM arteries.persistent p
                WHERE p.project_id IN (SELECT project_id FROM scope)
                  AND p.valid_until IS NULL AND p.embedding IS NOT NULL
                ORDER BY p.embedding <=> %(q)s::vector
                LIMIT %(limit)s
                """,
                {"q": vec, "project": PROJECT_ID, "limit": MAX_PERSISTENT_CONTEXT},
            )
        else:
            cur.execute(
                SCOPE_CTE + """
                SELECT p.id, p.fact, p.domains, p.confidence, p.source_ts,
                       NULL::float AS similarity
                FROM arteries.persistent p
                WHERE p.project_id IN (SELECT project_id FROM scope)
                  AND p.valid_until IS NULL
                ORDER BY p.source_ts DESC
                LIMIT %(limit)s
                """,
                {"project": PROJECT_ID, "limit": MAX_PERSISTENT_CONTEXT},
            )
        return [dict(r) for r in cur.fetchall()]


def _reject_duplicates(conn, memories: list[dict], vectors: list) -> tuple[list, list, list]:
    """Drop facts the store already holds, before they are written.

    Stage one of promotion, and the only deterministic one: a cosine at or above
    DUPLICATE_SIM against anything live in scope is a restatement, and no model
    opinion is needed to say so. This is what bounds growth -- the LLM pass is a
    decomposer (53 ephemeral rows produced 76 facts) and will not shrink itself.
    """
    kept, kept_vecs, rejected = [], [], []
    with conn.cursor() as cur:
        for mem, vec in zip(memories, vectors):
            if vec is None:
                kept.append(mem)
                kept_vecs.append(vec)
                continue
            cur.execute(
                SCOPE_CTE + """
                SELECT p.fact, 1 - (p.embedding <=> %(q)s::vector) AS sim
                FROM arteries.persistent p
                WHERE p.project_id IN (SELECT project_id FROM scope)
                  AND p.valid_until IS NULL AND p.embedding IS NOT NULL
                ORDER BY p.embedding <=> %(q)s::vector
                LIMIT 1
                """,
                {"q": vec, "project": PROJECT_ID},
            )
            row = cur.fetchone()
            if row and row[1] is not None and float(row[1]) >= DUPLICATE_SIM:
                rejected.append({"fact": mem["fact"], "duplicate_of": row[0],
                                 "similarity": round(float(row[1]), 3)})
                continue
            kept.append(mem)
            kept_vecs.append(vec)
    return kept, kept_vecs, rejected


_VALID_KINDS = {"fact", "decision", "preference", "constraint"}


def validate_response(payload: Any) -> list[str]:
    """Structural problems with a compile response. Empty means usable.

    Grammar-constrained JSON is well-formed, not correct. This module already
    validated UUIDs in Python because the model invents ids; entities and
    relations give it more to invent, so the check moves up front and the call
    is retried once when it fails.
    """
    problems: list[str] = []
    if not isinstance(payload, dict):
        return ["response is not an object"]
    mems = payload.get("new_memories")
    if not isinstance(mems, list):
        return ["new_memories is not a list"]
    for i, m in enumerate(mems):
        if not isinstance(m, dict) or not str(m.get("fact", "")).strip():
            problems.append(f"new_memories[{i}] has no fact")
            continue
        if m.get("kind") and m["kind"] not in _VALID_KINDS:
            problems.append(f"new_memories[{i}] kind={m['kind']!r}")
        for e in m.get("entities") or []:
            if not isinstance(e, dict) or not str(e.get("name", "")).strip():
                problems.append(f"new_memories[{i}] has a nameless entity")
        for r in m.get("relations") or []:
            if not isinstance(r, dict) or r.get("rel") not in graph.RELATIONS:
                problems.append(f"new_memories[{i}] rel={(r or {}).get('rel')!r}")
    if not isinstance(payload.get("superseded", []), list):
        problems.append("superseded is not a list")
    return problems


async def _llm_compile(
    ephemeral: list[dict],
    persistent: list[dict],
    problems: list[str] | None = None,
) -> dict:
    """Call the LLM to compile ephemeral records into persistent memories."""
    def _eph_line(i: int, r: dict) -> str:
        tag = ""
        if r.get("parent_agent_id"):
            tag = "[SUBAGENT] "
        elif r.get("source") == "assistant":
            tag = "[ASSISTANT] "
        return f"[{i+1}] {tag}(domains={r['domains']}) {r['fact']}"

    eph_text = "\n".join(_eph_line(i, r) for i, r in enumerate(ephemeral))

    per_text = "None yet." if not persistent else "\n".join(
        f"[{r['id']}] (domains={r['domains']}) {r['fact']}"
        for r in persistent
    )

    prompt = f"""Ephemeral records to compile:
{eph_text}

Existing persistent memories:
{per_text}"""
    if problems:
        prompt += ("\n\nYour previous response was rejected for these reasons. "
                   "Fix them and return the whole object again:\n- "
                   + "\n- ".join(problems[:5]))

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
            timeout=COMPILE_TIMEOUT,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    return json.loads(content)


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _write_results(conn, result: dict, claimed_ids: list) -> dict[str, int]:
    """Write compiled persistent records and mark ephemeral as cleared."""
    from arteries.embed import embed_texts_sync

    new_count = 0
    superseded_count = 0

    # Embed every fact in one request, before the transaction opens. This used
    # to be one HTTP call per fact issued *inside* the write transaction, which
    # held row locks across ~264ms of network I/O for a typical batch. One
    # batched call is 45ms and happens while holding nothing.
    memories = result.get("new_memories", [])
    vectors = embed_texts_sync([m["fact"] for m in memories])
    memories, vectors, duplicates = _reject_duplicates(conn, memories, vectors)
    if duplicates:
        runlog.log_event("memory.compile.duplicates_rejected", "arteries",
                         {"count": len(duplicates), "rejected": duplicates[:5]},
                         project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID)

    scope_id = scope.scope_for(PROJECT_ID) or PROJECT_ID
    new_ids: list[str] = []

    with conn.cursor() as cur:
        for mem, vec in zip(memories, vectors):
            cur.execute(
                """
                INSERT INTO arteries.persistent
                    (fact, domains, confidence, project_id, parent_ids, embedding, kind)
                VALUES (%s, %s::jsonb, %s, %s, %s::uuid[], %s::vector, %s)
                RETURNING id
                """,
                (
                    mem["fact"],
                    json.dumps(mem.get("domains", [])),
                    mem.get("confidence", 0.8),
                    PROJECT_ID,
                    claimed_ids,
                    vec,
                    mem.get("kind", "fact"),
                ),
            )
            claim_id = str(cur.fetchone()[0])
            new_ids.append(claim_id)
            new_count += 1

            # Provenance: the ephemeral rows this was distilled from. They are
            # endpoints only -- never entity-extracted, never nodes.
            for eph_id in claimed_ids:
                graph.add_edge(cur, PROJECT_ID, "persistent", claim_id,
                               graph.DERIVED_FROM, "ephemeral", eph_id)

            for ent in mem.get("entities") or []:
                node = graph.upsert_entity(cur, scope_id, ent.get("name", ""),
                                           ent.get("kind", "concept"))
                if node:
                    graph.add_edge(cur, PROJECT_ID, "persistent", claim_id,
                                   graph.MENTIONS, "entity", node.id)

            # How this fact interacts with ones already stored. The model only
            # ever sees ids it was given, but it invents them anyway.
            for rel in mem.get("relations") or []:
                target = rel.get("persistent_id")
                if rel.get("rel") not in graph.RELATIONS or not _is_uuid(target):
                    continue
                graph.add_edge(cur, PROJECT_ID, "persistent", claim_id,
                               rel["rel"], "persistent", target)

            decision = mem.get("decision") or {}
            if mem.get("kind") == "decision" and decision.get("chose"):
                graph.add_edge(cur, PROJECT_ID, "persistent", claim_id, "chose",
                               "literal", str(decision["chose"])[:200],
                               metadata={"because": str(decision.get("because", ""))[:500]})
                for alt in (decision.get("over") or [])[:5]:
                    graph.add_edge(cur, PROJECT_ID, "persistent", claim_id, "over",
                                   "literal", str(alt)[:200])

        for sup in result.get("superseded", []):
            pid = sup.get("persistent_id")
            if not pid:
                continue
            # Validate in Python — a bad literal would abort the whole
            # transaction (dropping the new_memories inserted just above), and
            # the LLM does invent ids. A UUID that matches no live row is also a
            # miss worth surfacing: the contradiction it claimed goes unretired.
            try:
                uuid.UUID(str(pid))
            except (ValueError, AttributeError, TypeError):
                runlog.log_event("memory.compile.bad_supersede", "arteries",
                                 {"persistent_id": str(pid), "reason": "not a uuid"},
                                 project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID)
                continue
            cur.execute(
                """
                UPDATE arteries.persistent
                SET valid_until = now()
                WHERE id = %s AND project_id = %s AND valid_until IS NULL
                """,
                (pid, PROJECT_ID),
            )
            if cur.rowcount:
                superseded_count += cur.rowcount
                # The reason has been generated on every pass since this module
                # was written and discarded every time. It lands on the edge.
                #
                # `replaced_by` indexes new_memories. Without it there is no
                # honest source for the edge -- the batch as a whole retired the
                # fact, not any one member -- so the tombstone stands alone.
                idx = sup.get("replaced_by")
                replacement = (new_ids[idx] if isinstance(idx, int) and 0 <= idx < len(new_ids)
                               else None)
                if replacement:
                    graph.add_edge(cur, PROJECT_ID, "persistent", replacement,
                                   graph.SUPERSEDES, "persistent", pid,
                                   metadata={"reason": str(sup.get("reason", ""))[:500]})
                else:
                    runlog.log_event("memory.compile.supersede_unattributed", "arteries",
                                     {"persistent_id": str(pid),
                                      "reason": str(sup.get("reason", ""))[:200]},
                                     project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID)
            else:
                runlog.log_event("memory.compile.bad_supersede", "arteries",
                                 {"persistent_id": str(pid), "reason": "no live match"},
                                 project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID)

        cur.execute(
            "UPDATE arteries.ephemeral SET status = 'cleared' WHERE id = ANY(%s::uuid[])",
            (claimed_ids,),
        )
        conn.commit()

    return {"new": new_count, "superseded": superseded_count,
            "duplicates_rejected": len(duplicates)}


if __name__ == "__main__":
    # `python -m arteries.compile` — one pass for ARTERIES_AGENT_ID, without
    # pulling the full `art` CLI import chain. Used by an orchestrator to flush
    # its subagents' ephemeral after they exit.
    print(asyncio.run(compile_once()))
