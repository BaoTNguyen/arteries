"""Assistant-response memory capture for CLI hooks."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from arteries import runlog
from arteries.cli_normalize import normalize
from arteries.extract import store_assistant_response


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
)
TRANSCRIPT_KEYS = (
    "transcript_path",
    "transcriptPath",
    "transcript_file",
    "transcriptFile",
    "session_file",
    "sessionFile",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Store an assistant response as Arteries ephemeral memory.")
    parser.add_argument("text", nargs="*", help="assistant response text")
    parser.add_argument("--stdin-json", action="store_true", help="read a CLI event JSON payload from stdin")
    parser.add_argument("--transcript", help="JSONL transcript path to scan for the last assistant message")
    parser.add_argument("--cli", default=os.getenv("ARTERIES_CLI", "generic"))
    parser.add_argument("--event", default="assistant_response")
    parser.add_argument("--project", default=os.getenv("ARTERIES_PROJECT", "default"))
    parser.add_argument("--agent", default=os.getenv("ARTERIES_AGENT_ID"))
    args = parser.parse_args(argv)

    event = _read_stdin_json() if args.stdin_json else {}
    if event:
        normalized = normalize(
            event,
            cli=args.cli,
            fallback_event=args.event,
            project_id=args.project,
            agent_id=args.agent,
        )
        _apply_event_env(normalized)

    text = " ".join(args.text).strip()
    if not text and event:
        text = assistant_text_from_event(event)
    if not text and args.transcript:
        text = read_last_assistant(args.transcript)

    if not text:
        return 0

    stored = store_assistant_response(text)
    preview = text[:500]
    runlog.log_event(
        "assistant.response",
        "arteries",
        {
            "assistant_preview": preview,
            "assistant_preview_truncated": len(text) > len(preview),
            "assistant_chars": len(text),
        },
    )
    runlog.log_event(
        "memory.assistant.stored",
        "arteries",
        {"stored": stored, "input_chars": len(text)},
    )
    return 0


def assistant_text_from_event(event: dict[str, Any]) -> str:
    transcript = _first_text(event, *TRANSCRIPT_KEYS)
    if transcript:
        text = read_last_assistant(transcript)
        if text:
            return text

    messages = _event_messages(event)
    for message in reversed(messages):
        role = str(message.get("role") or message.get("speaker") or message.get("type") or "").lower()
        if role in {"assistant", "agent", "model", "ai", "output", "response"}:
            text = _text_from_mapping(message)
            if text:
                return text

    return _first_text(event, *TEXT_KEYS) or ""


def read_last_assistant(transcript_path: str) -> str:
    """Walk a JSONL transcript backwards to find the last assistant message."""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return ""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("role") != "assistant":
            continue
        return _text_from_mapping(entry)
    return ""


def _event_messages(event: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("messages", "conversation", "transcript", "entries"):
        value = _nested_get(event, key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    preparation = _nested_get(event, "preparation")
    if isinstance(preparation, dict):
        for key in ("messages", "conversation", "transcript", "entries"):
            value = preparation.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _text_from_mapping(value: dict[str, Any]) -> str:
    text = _first_text(value, *TEXT_KEYS)
    if text:
        return text
    parts = value.get("parts")
    if isinstance(parts, list):
        return "\n".join(
            str(part.get("text") or part.get("content") or "").strip()
            for part in parts
            if isinstance(part, dict) and str(part.get("text") or part.get("content") or "").strip()
        ).strip()
    content = value.get("content")
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "").strip()
            for part in content
            if isinstance(part, dict) and str(part.get("text") or "").strip()
        ).strip()
    return ""


def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _nested_get(payload, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _nested_get(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload[key]
    for container in ("data", "payload", "details", "hook_input", "hookInput"):
        nested = payload.get(container)
        if isinstance(nested, dict) and key in nested:
            return nested[key]
    return None


def _apply_event_env(event: Any) -> None:
    os.environ["ARTERIES_CLI"] = event.cli
    os.environ["ARTERIES_EVENT"] = event.event
    os.environ["ARTERIES_AGENT_ID"] = event.agent_id
    os.environ["ARTERIES_AGENT_ROLE"] = event.agent_role
    if event.parent_agent_id:
        os.environ["ARTERIES_PARENT_AGENT_ID"] = event.parent_agent_id
    if event.session_id:
        os.environ["ARTERIES_SESSION_ID"] = event.session_id
    if event.cwd:
        os.environ["ARTERIES_EVENT_CWD"] = event.cwd


def _read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


if __name__ == "__main__":
    raise SystemExit(main())
