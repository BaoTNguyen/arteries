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
several of which change what gets fetched at all. Arteries has three, and two of
them are additive.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# A seed set this strong means similarity already answered the question.
STRONG_SIMILARITY = 0.65
ENOUGH_STRONG = 5

# Things that look like a named entity rather than prose: backticked spans,
# dotted paths, snake_case, CamelCase. Deliberately narrow -- this decides
# whether to *try* an entity lookup, and the lookup itself is the real filter.
_NAMED = re.compile(
    r"`([^`]+)`"                      # `backticked`
    r"|\b([a-zA-Z_]+\.[a-z]{1,4})\b"  # dotted path: find.py, config.toml
    r"|\b([A-Z][A-Z0-9]*_[A-Z0-9_]+)\b"  # CONSTANT_CASE -- the common shape here
    r"|\b([a-z]+_[a-z_]+)\b"          # snake_case
    r"|\b([A-Z][a-z]+[A-Z]\w+)\b"     # CamelCase
)


@dataclass
class Route:
    """A retrieval plan and the evidence behind it."""

    strategy: str                      # cosine | cosine+entity | cosine+expansion
    reason: str
    strong_seeds: int = 0
    entities: list[str] = field(default_factory=list)

    def as_payload(self) -> dict:
        return {"strategy": self.strategy, "reason": self.reason,
                "strong_seeds": self.strong_seeds, "entities": self.entities[:5]}


def named_candidates(query: str) -> list[str]:
    """Identifier-shaped tokens in a query, for the entity lookup to try."""
    out: list[str] = []
    for match in _NAMED.finditer(query):
        token = next(g for g in match.groups() if g)
        if token.lower() not in out:
            out.append(token.lower())
    return out


def choose(query: str, seeds: list[dict]) -> Route:
    """Pick a strategy from the seeds similarity search actually returned.

    Order matters. A strong seed set short-circuits everything -- adding
    neighbours to an answer that already exists spends context budget for
    nothing. Only when similarity came back weak is it worth asking whether the
    query names something the graph knows.
    """
    strong = sum(1 for s in seeds if float(s.get("similarity") or 0.0) >= STRONG_SIMILARITY)
    if strong >= ENOUGH_STRONG:
        return Route("cosine", f"{strong} seeds at or above {STRONG_SIMILARITY}", strong)

    named = named_candidates(query)
    if named:
        return Route("cosine+entity",
                     f"only {strong} strong seeds; query names {named[0]!r}",
                     strong, named)

    return Route("cosine+expansion",
                 f"only {strong} strong seeds and no named entity", strong)
