"""Thin wrapper around the standalone embedding server."""

from __future__ import annotations

import httpx

from arteries.config import EMBED_MODEL, EMBED_URL


def embed_text_sync(text: str) -> list[float] | None:
    try:
        resp = httpx.post(
            EMBED_URL,
            json={"model": EMBED_MODEL, "input": text},
            timeout=5.0,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception:
        return None
