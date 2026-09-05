"""Small, triage-only view of the host's recent raw conversation."""

from __future__ import annotations

import json
import os
from pathlib import Path

from arteries.eventjson import text_from_mapping

TAIL_BYTES = 256 * 1024
WINDOW_TURNS = 8


def recent_assistant_turns(transcript: str | None = None, limit: int = WINDOW_TURNS) -> list[str]:
    """Return only prior assistant text, the sole triage context evidence."""
    return _recent_turns(transcript, limit, {"assistant"})


def recent_user_turns(transcript: str | None = None, limit: int = WINDOW_TURNS) -> list[str]:
    """Return only prior user text.

    The assistant restatement filter needs the question it is answering. At
    Stop-hook time the newest transcript entry is the assistant's own reply, so
    `recent_turns` would hand the stripper the very text it is meant to trim
    against and cut all of it.
    """
    return _recent_turns(transcript, limit, {"user"})


def _recent_turns(transcript: str | None, limit: int, roles: set[str]) -> list[str]:
    path = transcript or os.getenv("ARTERIES_TRANSCRIPT")
    if not path and os.getenv("ARTERIES_CLI") == "codex":
        from arteries.usage import _codex_rollout
        path = _codex_rollout(os.environ.get("ARTERIES_EVENT_CWD") or os.getcwd())
    if not path:
        return []
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    lines = raw.splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]
    turns = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        message = message if isinstance(message, dict) else entry
        role = str(message.get("role") or message.get("type") or "").lower()
        if role not in roles:
            continue
        text = text_from_mapping(message)
        if text:
            turns.append(text)
    return turns[-limit:]
