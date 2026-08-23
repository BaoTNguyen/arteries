"""Which retrieval strategy a query gets, and why.

Cognee routes with a regex classifier over the query text, plus an optional LLM
mode-selector. Neither transfers here.

A regex classifier is the thing this codebase already measured failing:
`extract.py` shipped four patterns and three of them never matched once across
202 stored rows. Guessing a query's shape from its wording has the same failure
mode and no better prospects. An LLM classifier costs a call on a path whose
entire budget is one 27ms embedding.

So routing happens **after** the cheap evidence arrives, not before it. The
embedding is needed anyway -- for the ephemeral coverage check as well as for
retrieval -- and once it exists the similarity distribution is a *measured*
statement about how well the store covers the query. That beats any prediction
made from the words alone.

Cognee routes a priori because it has sixteen genuinely different strategies,
several of which change what gets fetched at all. Arteries has two, and the
second is additive -- which is why nothing here reads the query text. An earlier
version added a third route keyed on identifier-shaped tokens in the query; it
reintroduced exactly the regex-guessing this module exists to avoid, and it
overlapped so heavily with cosine that it was returning claims already ranked
first. Removed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# A seed set this strong means similarity already answered the question.
STRONG_SIMILARITY = 0.65
ENOUGH_STRONG = 5

@dataclass
class Route:
    """A retrieval plan and the evidence behind it."""

    strategy: str                      # cosine | cosine+expansion
    reason: str
    strong_seeds: int = 0

    def as_payload(self) -> dict:
        return {"strategy": self.strategy, "reason": self.reason,
                "strong_seeds": self.strong_seeds}


def choose(seeds: list[dict]) -> Route:
    """Pick a strategy from the seeds similarity search actually returned.

    A strong seed set short-circuits the walk: adding neighbours to an answer
    that already exists spends context budget for nothing. Measured on 22
    held-out queries, expansion costs about seven extra claims each, so it is
    worth paying only when similarity came back weak.
    """
    strong = sum(1 for s in seeds if float(s.get("similarity") or 0.0) >= STRONG_SIMILARITY)
    if strong >= ENOUGH_STRONG:
        return Route("cosine", f"{strong} seeds at or above {STRONG_SIMILARITY}", strong)

    return Route("cosine+expansion", f"only {strong} strong seeds", strong)
