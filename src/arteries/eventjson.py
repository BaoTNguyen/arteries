"""Shared parsing for CLI hook/event JSON payloads.

Every hook entry point needs the same key-probing logic — payloads arrive
from six CLIs with different key spellings and nesting. This is the one
home for it. Stdlib only; keep it import-cheap.
"""

from __future__ import annotations

import json
import sys
from typing import Any

TEXT_KEYS = (
    "assistant",
    "assistant_response",
    "assistantResponse",
    "response",
    "response_text",
    "responseText",
    "output",
    "text",
    "content",
    "message",
    "body",
    "value",
    "prompt",
)
# agent-specific transcripts first: on SubagentStop events these point at the
# subagent's own conversation, while transcript_path is the parent session's
AGENT_TRANSCRIPT_KEYS = (
    "agent_transcript_path",
    "agentTranscriptPath",
    "agent_transcript",
    "agentTranscript",
)
SESSION_TRANSCRIPT_KEYS = (
    "transcript_path",
    "transcriptPath",
    "transcript_file",
    "transcriptFile",
    "session_file",
    "sessionFile",
)
TRANSCRIPT_KEYS = AGENT_TRANSCRIPT_KEYS + SESSION_TRANSCRIPT_KEYS


def read_stdin_json() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def nested_get(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload[key]
    for container in ("data", "payload", "details", "hook_input", "hookInput"):
        nested = payload.get(container)
        if isinstance(nested, dict) and key in nested:
            return nested[key]
    return None


def first_text(payload: dict[str, Any], *keys: str) -> str | None:
    """First non-empty string value among keys (containers probed)."""
    for key in keys:
        value = nested_get(payload, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def payload_text(payload: dict[str, Any], *keys: str) -> str:
    """Like first_text but also joins lists of plain strings."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            text = "\n".join(
                str(item).strip()
                for item in value
                if not isinstance(item, dict) and str(item).strip()
            ).strip()
            if text:
                return text
    return ""


def text_from_mapping(value: dict[str, Any]) -> str:
    """Extract message text from one transcript/message mapping.

    Handles plain string fields plus parts/content block lists
    (e.g. Claude's [{"type": "text", "text": ...}]).
    """
    text = first_text(value, *TEXT_KEYS)
    if text:
        return text
    for key in ("parts", "content"):
        blocks = value.get(key)
        if isinstance(blocks, list):
            joined = "\n".join(
                str(b.get("text") or b.get("content") or "").strip()
                for b in blocks
                if isinstance(b, dict) and str(b.get("text") or b.get("content") or "").strip()
            ).strip()
            if joined:
                return joined
    return ""


def event_messages(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Find a message list in an event payload, wherever the CLI nested it."""
    for key in ("messages", "conversation", "transcript", "entries"):
        value = nested_get(event, key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    preparation = nested_get(event, "preparation")
    if isinstance(preparation, dict):
        for key in ("messages", "conversation", "transcript", "entries"):
            value = preparation.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []
