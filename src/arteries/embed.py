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


def embed_texts_sync(texts: list[str], *, is_query: bool = False) -> list[list[float] | None]:
    """Embed many strings in one request. Returns one slot per input, None on failure.

    The endpoint takes a list and returns them in order, so N facts cost one
    round trip instead of N. Measured on 8 facts: 264ms sequential, 45ms batched.
    The failure mode is all-or-nothing by design -- a partial batch would make
    the caller reconcile indices against a shorter list, and every caller wants
    "embed what you can, store NULL for the rest".
    """
    if not texts:
        return []
    payload = [(QUERY_PREFIX + t) if is_query else t for t in texts]
    try:
        resp = httpx.post(
            EMBED_URL,
            json={"model": EMBED_MODEL, "input": payload},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        if len(data) != len(texts):
            return [None] * len(texts)
        # the API may return out of order; index is authoritative when present
        out: list[list[float] | None] = [None] * len(texts)
        for i, item in enumerate(data):
            out[item.get("index", i)] = item["embedding"]
        return out
    except Exception:
        return [None] * len(texts)
