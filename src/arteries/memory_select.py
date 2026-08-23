"""Memory selection policy used by frame assembly.

This module keeps CLI-specific capability handling out of frame.py while
preserving the existing default behavior when no CLI/subagent metadata exists.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from arteries import storage
from arteries.cli_caps import CliCapabilities, get_capabilities
from arteries.config import AGENT_PROCESS_ID, EPHEMERAL_MODE, PERSISTENT_READ, PROJECT_ID, RELEVANCE_THRESHOLD
from arteries.embed import embed_text_sync
from arteries.extract import get_ephemeral_buffer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentContext:
    cli: str
    project_id: str
    agent_id: str
    parent_agent_id: str | None
    agent_role: str
    event: str
    capabilities: CliCapabilities


def context_from_env() -> AgentContext:
    cli = os.getenv("ARTERIES_CLI", "generic")
    return AgentContext(
        cli=cli,
        project_id=PROJECT_ID,
        agent_id=AGENT_PROCESS_ID,
        parent_agent_id=os.getenv("ARTERIES_PARENT_AGENT_ID") or None,
        agent_role=os.getenv("ARTERIES_AGENT_ROLE", "parent"),
        event=os.getenv("ARTERIES_EVENT", "prompt"),
        capabilities=get_capabilities(cli),
    )


def select_for_frame(
    message: str,
    context: AgentContext | None = None,
    embedding: list[float] | None = None,
) -> tuple[list[dict], list[dict]]:
    context = context or context_from_env()
    ephemerals = _select_ephemeral(context)
    persistents = _select_persistent(message, context, embedding)
    return ephemerals, persistents


def _select_ephemeral(context: AgentContext) -> list[dict]:
    if EPHEMERAL_MODE == "discard":
        return get_ephemeral_buffer()[-20:]

    current = storage.get_ephemeral(context.project_id, context.agent_id, limit=20)
    if not _should_include_parent_ephemeral(context):
        return current

    parent = storage.get_ephemeral(context.project_id, context.parent_agent_id, limit=10)
    return _dedupe_by_id_or_fact(current + parent)[:20]


def _should_include_parent_ephemeral(context: AgentContext) -> bool:
    if not context.parent_agent_id:
        return False
    if context.agent_role == "subagent":
        return True
    return context.capabilities.observes_subagents


# Graph expansion runs only when cosine came back thin. A strong seed set is
# already the answer; walking outward from it would add weaker neighbours to a
# frame that is budget-limited anyway. Thin means: few results, or the best one
# was not very close.
EXPAND_WHEN_FEWER_THAN = 5
EXPAND_WHEN_TOP_BELOW = 0.55
EXPAND_HOPS = 1


def _expand(seeds: list[dict], context: AgentContext, limit: int) -> list[dict]:
    """Add claims reachable from the seeds along the graph, ranked below them.

    A neighbour is worth surfacing precisely when similarity search missed it --
    a fact that contradicts, refines, or shares an entity with a strong hit is
    relevant by association rather than by wording. Weight-decayed so it never
    outranks a direct match.
    """
    if not seeds:
        return []
    try:
        import psycopg2

        from arteries import graph
        from arteries.config import DB_CONFIG

        conn = psycopg2.connect(**DB_CONFIG)
        try:
            reached = graph.expand(
                conn, context.project_id, [str(s["id"]) for s in seeds],
                hops=EXPAND_HOPS, limit=limit,
            )
        finally:
            conn.close()
    except Exception:
        logger.debug("graph expansion unavailable", exc_info=True)
        return []

    seen = {str(s["id"]) for s in seeds}
    added = []
    for row in reached:
        if str(row["id"]) in seen:
            continue
        # `similarity` so the packet scores it on the same axis as a direct hit,
        # but derived from hop distance rather than from the query vector.
        row["similarity"] = float(row.get("score") or 0.0)
        row["via_graph"] = True
        added.append(row)
    return added


def _select_persistent(
    message: str,
    context: AgentContext,
    embedding: list[float] | None = None,
) -> list[dict]:
    if PERSISTENT_READ == "none":
        return []
    if PERSISTENT_READ == "relevance":
        query_emb = embedding or embed_text_sync(message, is_query=True)
        has_emb = bool(query_emb) and storage.has_embeddings(context.project_id)
        if query_emb and has_emb:
            seeds = storage.get_persistent_by_relevance(
                context.project_id,
                query_emb,
                limit=20,
                threshold=RELEVANCE_THRESHOLD,
            )
            top = max((float(s.get("similarity") or 0.0) for s in seeds), default=0.0)
            if len(seeds) < EXPAND_WHEN_FEWER_THAN or top < EXPAND_WHEN_TOP_BELOW:
                expanded = _expand(seeds, context, limit=20 - len(seeds))
                if expanded:
                    logger.info("graph expansion added %d claims to %d seeds",
                                len(expanded), len(seeds))
                return seeds + expanded
            return seeds
        # Relevance was requested but we couldn't do it — no query embedding
        # (embedder down) or no stored embeddings. We fall back to recency, which
        # is a different, weaker read; say so rather than degrade silently.
        logger.info(
            "persistent read fell back to recency: query_emb=%s has_embeddings=%s",
            bool(query_emb), has_emb,
        )
    return storage.get_persistent(context.project_id, limit=20)


def _dedupe_by_id_or_fact(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        key = str(row.get("id") or row.get("fact") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
