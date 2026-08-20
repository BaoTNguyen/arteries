"""Thin wrapper around the standalone embedding server."""

from __future__ import annotations

import httpx

from arteries.config import EMBED_MODEL, EMBED_URL, QUERY_PREFIX


def embed_text_sync(text: str, *, is_query: bool = False) -> list[float] | None:
    """Embed one string. Set is_query for the search side of an asymmetric pair.

    Stored facts are documents; the live user message matching against them is a
    query. Models trained for asymmetric retrieval want a prefix on the query
    side only, which capillaries owns as QUERY_PREFIX. It is empty for the
    current model -- passing it through anyway means a model change lands in one
    place rather than silently degrading recall here.
    """
    try:
        resp = httpx.post(
            EMBED_URL,
            json={
                "model": EMBED_MODEL,
                "input": (QUERY_PREFIX + text) if is_query else text,
            },
            timeout=5.0,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception:
        return None
