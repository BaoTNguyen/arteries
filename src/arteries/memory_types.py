"""Memory frame contract types.

The shape arteries hands to any consumer of its memory. Arteries builds these
frames (`arteries.frame`); capillaries reads them to gate and filter retrieval.
They used to live in capillaries, which meant the producer imported its own
output type from a consumer — the retrieval project could not be swapped or
removed without arteries losing the definition of its own contract.

Deliberately stdlib-only dataclasses. Both sides import this module on every
hook turn, so it must stay free of psycopg2, the ML stack, and anything else
with a startup cost. Nothing here should ever gain a dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Insight:
    text: str
    source: str
    domain: str | None = None
    confidence: float = 1.0


@dataclass
class CachedRetrieval:
    prompt_id: str
    situation: str
    score: float
    relevance: float = 1.0


@dataclass
class EphemeralMemory:
    recent_messages: list[str] = field(default_factory=list)
    topic_drift: float = 0.0
    turn_count: int = 0


@dataclass
class PersistentMemory:
    session_insights: list[Insight] = field(default_factory=list)
    prior_retrievals: list[CachedRetrieval] = field(default_factory=list)
    active_domains: list[str] = field(default_factory=list)


@dataclass
class EvergreenMemory:
    user_intent: list[str] = field(default_factory=list)
    recurring_domains: list[str] = field(default_factory=list)
    ground_truth_insights: list[Insight] = field(default_factory=list)
    last_retrieval_ts: float | None = None
    retrieval_confidence: float | None = None


@dataclass
class MemoryFrame:
    ephemeral: EphemeralMemory = field(default_factory=EphemeralMemory)
    persistent: PersistentMemory = field(default_factory=PersistentMemory)
    evergreen: EvergreenMemory = field(default_factory=EvergreenMemory)


__all__ = [
    "CachedRetrieval",
    "EphemeralMemory",
    "EvergreenMemory",
    "Insight",
    "MemoryFrame",
    "PersistentMemory",
]
