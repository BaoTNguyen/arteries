"""Assistant-response memory capture for CLI hooks."""

from __future__ import annotations

import logging

import argparse
import json
import os
from typing import Any

from arteries import runlog
from arteries.config import PROJECT_ID
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
        capture_response(text, turn_id=_open_turn_id())
    return 0


logger = logging.getLogger(__name__)


def capture_response(text: str, turn_id: str | None = None, prior_turn: bool = False,
                     answers: str = "") -> int:
    """Store an assistant response as ephemeral and log the capture events."""
    # The user turn this replies to, so restatements of the question can be
    # dropped. Best effort: no transcript means no reference and nothing is cut.
    # `conversation` is not on this branch -- it is untracked work in the main
    # checkout -- so this import fails here and restatement filtering silently
    # never runs. Say so once rather than pretending there was no transcript,
    # which is what the bare handler did.
    user_turn = ""
    try:
        from arteries.conversation import recent_user_turns
    except ImportError:
        logger.debug("arteries.conversation absent; assistant restatement "
                     "filtering is inactive on this branch")
    else:
        try:
            prior = recent_user_turns(limit=1)
            user_turn = prior[-1] if prior else ""
        except Exception as exc:
            from arteries import degrade
            degrade.note(exc, "recent turn lookup")
    stored = store_assistant_response(text, user_turn)
    preview = text[:2000]
    payload = {
        "assistant_preview": preview,
        "assistant_preview_truncated": len(text) > len(preview),
        "assistant_chars": len(text),
    }
    if prior_turn:
        # captured at the start of turn N, this answers turn N-1
        payload["prior_turn"] = True
    if answers:
        # the question this answers, read off the transcript's parent chain.
        # packet.py matches on this instead of counting turns backwards.
        payload["answers_preview"] = answers[:200]
    runlog.log_event("assistant.response", "arteries", payload, turn_id=turn_id)
    report = {"stored": stored, "input_chars": len(text)}
    if not stored:
        # Only on a drop: the counters that say which filter did it. Costs
        # nothing on the 29% that store, and turns the other 71% from an
        # unexplained zero into a diagnosis.
        from arteries.extract import strip_report
        try:
            report |= strip_report(text, user_turn)
        except Exception as exc:
            report["strip_report_failed"] = f"{type(exc).__name__}: {exc}"[:200]
    runlog.log_event("memory.assistant.stored", "arteries", report, turn_id=turn_id)
    return stored


def _open_turn_id() -> str | None:
    """turn_id of the most recent observed turn, or None.

    A Stop hook is a fresh process, so eval.py's in-memory turn_id is gone by
    the time this runs. Reading the last turn.observed row back is the join.
    """
    try:
        for event in runlog.recent_events(project_id=PROJECT_ID, limit=20,
                                          repo_path=os.getenv("ARTERIES_REPO")):
            if str(event.get("event_type")) == "turn.observed" and event.get("turn_id"):
                return str(event["turn_id"])
    except Exception:
        pass
    return None


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


def _tail_entries(transcript_path: str) -> list[dict[str, Any]]:
    """Parse the tail of a JSONL transcript into entries, oldest first.

    Reads only the last TAIL_BYTES so huge transcripts stay cheap.
    """
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]  # first line is likely truncated mid-record
    entries: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _assistant_text(entry: dict[str, Any]) -> str:
    if (entry.get("role") or entry.get("type")) != "assistant":
        return ""
    message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
    return text_from_mapping(message)


def _user_prompt(entry: dict[str, Any]) -> str:
    """Text of a real user prompt, or "" for tool results and everything else.

    Claude Code files tool results as user entries too; the discriminator is
    that a typed prompt's content is a string, while a tool result is a list of
    tool_result blocks.
    """
    if (entry.get("role") or entry.get("type")) != "user":
        return ""
    message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


def read_last_assistant(transcript_path: str) -> str:
    """The last assistant message with text, or "".

    Handles both flat entries ({"role": "assistant", ...}) and Claude Code's
    nested format ({"type": "assistant", "message": {...}}).
    """
    for entry in reversed(_tail_entries(transcript_path)):
        text = _assistant_text(entry)
        if text:
            return text
        # tool-use-only entry: keep scanning for the last one with text
    return ""


def read_last_exchange(transcript_path: str) -> tuple[str, str]:
    """(last assistant text, the user prompt it answers).

    The second half is the join key. Deriving "which question is this an answer
    to" from the transcript's own parentUuid chain is the difference between a
    fact and a guess: the old scheme inferred it by subtracting one from the
    turn counter, so a single skipped capture silently shifted every earlier
    pair onto the wrong question.

    The prompt comes back empty when the parent chain leaves the tail window,
    which callers should treat as "no join key", not as an error.
    """
    entries = _tail_entries(transcript_path)
    by_uuid = {e["uuid"]: e for e in entries if e.get("uuid")}

    answer = None
    for entry in reversed(entries):
        if _assistant_text(entry):
            answer = entry
            break
    if answer is None:
        return "", ""

    seen: set[str] = set()
    node = answer
    while node is not None:
        parent = node.get("parentUuid")
        if not parent or parent in seen:
            break
        seen.add(parent)
        node = by_uuid.get(parent)
        if node is None:
            break
        prompt = _user_prompt(node)
        if prompt:
            return _assistant_text(answer), prompt
    return _assistant_text(answer), ""


if __name__ == "__main__":
    raise SystemExit(main())
