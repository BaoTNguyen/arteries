"""
MemoryFrame assembly from live storage.

Reads from arteries' three tiers and the retrieval log, builds the
MemoryFrame. When capillaries is installed, it consumes these directly.
"""

from __future__ import annotations

import logging

from arteries.memory_types import (
    CachedRetrieval,
    EphemeralMemory,
    Insight,
    MemoryFrame,
    PersistentMemory,
    ScopeMemory,
)

from arteries.config import AGENT_PROCESS_ID, PROJECT_ID
from arteries import memory_select, storage

logger = logging.getLogger(__name__)


def get_current_frame(message: str, embedding: list[float] | None = None) -> MemoryFrame:
    try:
        return _build_frame(message, embedding)
    except Exception:
        # An empty frame is a valid "no memories yet" answer, so a swallowed
        # failure here (Postgres down, embedder down) is invisible — it looks
        # identical to a healthy empty frame. Log so the two can be told apart.
        logger.warning("frame build failed; returning empty MemoryFrame", exc_info=True)
        return MemoryFrame()


def _build_frame(message: str, embedding: list[float] | None = None) -> MemoryFrame:
    ephemerals, persistents = memory_select.select_for_frame(message, embedding=embedding)

    # Reinforce what we surfaced. Not to reorder it -- persistent stays
    # relevance-ranked -- but so a claim nobody ever sees becomes identifiable,
    # and prunable. This is the only usage signal the store has.
    storage.touch_persistent([str(r["id"]) for r in persistents[:10] if r.get("id")])

    retrievals = storage.get_recent_retrievals(PROJECT_ID, AGENT_PROCESS_ID, limit=10)

    active_domains = storage.get_active_domains(PROJECT_ID)

    recent_messages = [r["fact"] for r in ephemerals[:10]]

    # Topic drift: ratio of domains in recent ephemerals that differ from
    # active persistent domains. 0 = on topic, 1 = fully drifted.
    eph_domains = set()
    for r in ephemerals[:10]:
        for d in (r.get("domains") or []):
            eph_domains.add(d)
    active_set = set(active_domains)
    if eph_domains and active_set:
        overlap = len(eph_domains & active_set)
        topic_drift = 1.0 - (overlap / max(len(eph_domains), 1))
    else:
        topic_drift = 0.0

    last_retrieval = retrievals[0] if retrievals else None

    return MemoryFrame(
        ephemeral=EphemeralMemory(
            recent_messages=recent_messages,
            topic_drift=topic_drift,
            turn_count=len(ephemerals),
        ),
        persistent=PersistentMemory(
            session_insights=[
                Insight(
                    text=r["fact"],
                    source="persistent",
                    domain=(r.get("domains") or [None])[0],
                    confidence=r.get("confidence", 1.0),
                )
                for r in persistents[:10]
            ],
            prior_retrievals=[
                CachedRetrieval(
                    prompt_id=r["prompt_id"],
                    situation=r["situation"],
                    score=r["score"],
                    relevance=r["relevance"],
                )
                for r in retrievals
            ],
            active_domains=active_domains,
        ),
        # Context from the other repos in this project's scope. Partitioned out
        # of rows already fetched -- get_persistent_by_relevance returns
        # project_id per row, so this costs no extra query. capillaries reads
        # recurring_domains and user_intent for two of its four ranking boosts;
        # both were dead while this was empty.
        scope=ScopeMemory(
            user_intent=[
                r["fact"] for r in persistents
                if "intent" in (r.get("domains") or [])
            ][:5],
            recurring_domains=_recurring(persistents, PROJECT_ID),
            sibling_insights=[
                Insight(
                    text=r["fact"],
                    source=str(r.get("project_id") or "sibling"),
                    domain=(r.get("domains") or [None])[0],
                    confidence=r.get("confidence", 1.0),
                )
                for r in persistents
                if r.get("project_id") and r["project_id"] != PROJECT_ID
            ][:8],
            last_retrieval_ts=(
                last_retrieval["created_at"].timestamp()
                if last_retrieval else None
            ),
            retrieval_confidence=(
                last_retrieval["score"]
                if last_retrieval else None
            ),
        ),
    )


def _recurring(rows: list[dict], home: str) -> list[str]:
    """Domains that show up in more than one repo in the scope.

    A domain this project alone uses is `active_domains`; one that recurs across
    siblings says something about how the group works, which is what capillaries
    weights with RECURRING_DOMAIN_BOOST.
    """
    by_domain: dict[str, set[str]] = {}
    for row in rows:
        project = row.get("project_id") or home
        for domain in row.get("domains") or []:
            by_domain.setdefault(domain, set()).add(project)
    return sorted(d for d, projects in by_domain.items() if len(projects) > 1)
