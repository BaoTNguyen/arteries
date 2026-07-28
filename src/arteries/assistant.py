"""Assistant-response memory capture for CLI hooks."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from arteries import runlog
from arteries.cli_normalize import apply_event_env, normalize
from arteries.eventjson import (
    AGENT_TRANSCRIPT_KEYS,
    TRANSCRIPT_KEYS,
    event_messages,
    first_text,
    read_stdin_json,
    text_from_mapping,
)
from arteries.extract import store_assistant_response

TAIL_BYTES = 256 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Store an assistant response as Arteries ephemeral memory.")
    parser.add_argument("text", nargs="*", help="assistant response text")
    parser.add_argument("--stdin-json", action="store_true", help="read a CLI event JSON payload from stdin")
    parser.add_argument("--transcript", help="JSONL transcript path to scan for the last assistant message")
    parser.add_argument(
        "--require-agent-transcript",
        action="store_true",
        help="only ingest when the event carries an agent-specific transcript (SubagentStop safety)",
    )
    parser.add_argument("--cli", default=os.getenv("ARTERIES_CLI", "generic"))
    parser.add_argument("--event", default="assistant_response")
    parser.add_argument("--project", default=os.getenv("ARTERIES_PROJECT", "default"))
    parser.add_argument("--agent", default=os.getenv("ARTERIES_AGENT_ID"))
    args = parser.parse_args(argv)

    event = read_stdin_json() if args.stdin_json else {}
    if event:
        normalized = normalize(
            event,
            cli=args.cli,
            fallback_event=args.event,
            project_id=args.project,
            agent_id=args.agent,
        )
        apply_event_env(normalized)

    if args.require_agent_transcript and not first_text(event, *AGENT_TRANSCRIPT_KEYS):
        return 0

    text = " ".join(args.text).strip()
    if not text and event:
        text = assistant_text_from_event(event)
    if not text and args.transcript:
        text = read_last_assistant(args.transcript)

    if text:
        capture_response(text)
    return 0


def capture_response(text: str, turn_id: str | None = None, prior_turn: bool = False) -> int:
    """Store an assistant response as ephemeral and log the capture events."""
    stored = store_assistant_response(text)
    preview = text[:2000]
    payload = {
        "assistant_preview": preview,
        "assistant_preview_truncated": len(text) > len(preview),
        "assistant_chars": len(text),
    }
    if prior_turn:
        # captured at the start of turn N, this answers turn N-1
        payload["prior_turn"] = True
    runlog.log_event("assistant.response", "arteries", payload, turn_id=turn_id)
    runlog.log_event(
        "memory.assistant.stored",
        "arteries",
        {"stored": stored, "input_chars": len(text)},
        turn_id=turn_id,
    )
    return stored


def assistant_text_from_event(event: dict[str, Any]) -> str:
    transcript = first_text(event, *TRANSCRIPT_KEYS)
    if transcript:
        text = read_last_assistant(transcript)
        if text:
            return text

    for message in reversed(event_messages(event)):
        role = str(message.get("role") or message.get("speaker") or message.get("type") or "").lower()
        if role in {"assistant", "agent", "model", "ai", "output", "response"}:
            text = text_from_mapping(message)
            if text:
                return text

    return first_text(event, "assistant", "assistant_response", "assistantResponse",
                      "response", "output", "text") or ""


def read_last_assistant(transcript_path: str) -> str:
    """Walk a JSONL transcript backwards to find the last assistant message with text.

    Handles both flat entries ({"role": "assistant", ...}) and Claude Code's
    nested format ({"type": "assistant", "message": {...}}). Reads only the
    file tail so huge transcripts stay cheap.
    """
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = raw.splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]  # first line is likely truncated mid-record
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        role = entry.get("role") or entry.get("type")
        if role != "assistant":
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
        text = text_from_mapping(message)
        if text:
            return text
        # tool-use-only entry: keep scanning for the last one with text
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
