"""Reciprocal rank fusion, in one place because two layers need it.

Used twice, for the same reason both times: the things being combined carry
scores that are not the same quantity. Across tiers a cosine meets a recency
position meets a decayed hop score. Within the persistent tier a cosine meets a
`ts_rank_cd`. Ranks are commensurable where those scores are not -- rank 1 means
"the best thing this channel has" in every channel.

    fused(row) = sum over channels of  weight / (K + rank_in_channel)

A row present in only one channel still contributes its term, which is the
property that matters: an exact identifier match that no embedding could find
should not need a cosine to earn its place.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Iterable

# 60 is the constant the original RRF paper uses and capillaries kept. Large
# relative to the number of rows in play, which flattens the difference between
# adjacent ranks and lets a row present in both channels beat a row that tops
# only one.
RRF_K = int(os.getenv("ARTERIES_RRF_K", "60"))


def fuse(channels: Iterable[tuple[str, list[Any], float]],
         key: Callable[[Any], Any]) -> list[tuple[str, Any]]:
    """Merge ranked channels. Returns (channel_name, item), best first.

    `channels` is (name, ranked_items, weight). `key` identifies an item across
    channels, so the same row found by two of them accumulates both terms
    instead of appearing twice.
    """
    scores: dict[Any, float] = {}
    origin: dict[Any, tuple[int, str, Any]] = {}

    for index, (name, items, weight) in enumerate(channels):
        for rank, item in enumerate(items, start=1):
            identity = key(item)
            scores[identity] = scores.get(identity, 0.0) + weight / (RRF_K + rank)
            # First channel to produce a row owns how it is labelled and which
            # copy survives. Channels are passed in priority order, so this is a
            # decision rather than an accident of iteration.
            origin.setdefault(identity, (index, name, item))

    # Sorted by score, then by the channel that found it, so ties resolve the
    # same way on every run. Half of any two equally weighted channels' rows tie
    # exactly, and set iteration order is not a tiebreak anyone can reason about.
    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], origin[pair[0]][0]))
    return [(origin[identity][1], origin[identity][2]) for identity, _score in ordered]
