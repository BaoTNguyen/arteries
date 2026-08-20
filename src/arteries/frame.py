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
    EvergreenMemory,
    Insight,
    MemoryFrame,
    PersistentMemory,
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

    evergreens = storage.get_evergreen(limit=20, query_embedding=embedding)
    # reinforce what we surfaced — this is the only writer of access_count
    storage.touch_evergreen([str(r["id"]) for r in evergreens if r.get("id")])
    retrievals = storage.get_recent_retrievals(PROJECT_ID, AGENT_PROCESS_ID, limit=10)

    active_domains = storage.get_active_domains(PROJECT_ID)
    recurring_domains = storage.get_recurring_domains()

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
        evergreen=EvergreenMemory(
            user_intent=[
                r["fact"] for r in evergreens
                if "intent" in (r.get("domains") or [])
            ],
            recurring_domains=recurring_domains,
            ground_truth_insights=[
                Insight(
                    text=r["fact"],
                    source="evergreen",
                    domain=(r.get("domains") or [None])[0],
                    confidence=r.get("confidence", 1.0),
                )
                for r in evergreens
            ],
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
