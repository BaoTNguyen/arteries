"""Continuity packet assembly for CLI context pressure events."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

from arteries import storage
from arteries.config import AGENT_PROCESS_ID, PROJECT_ID


@dataclass
class MemoryItem:
    tier: str
    text: str
    confidence: float
    domains: list[str]
    source_id: str | None = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an Arteries continuity packet.")
    parser.add_argument("--format", choices=("markdown", "pi-compaction-json"), default="markdown")
    parser.add_argument("--message", default="", help="current user message or compaction reason")
    parser.add_argument("--budget", type=int, default=6000, help="approximate character budget")
    parser.add_argument("--stdin-json", action="store_true", help="read CLI event JSON from stdin")
    args = parser.parse_args(argv)

    event = _read_stdin_json() if args.stdin_json else {}
    message = args.message or _event_message(event)
    packet = build_packet(message=message, event=event, budget=args.budget)

    if args.format == "pi-compaction-json":
        print(json.dumps({
            "summary": packet,
            "details": {
                "source": "arteries",
                "project": PROJECT_ID,
                "agent_id": AGENT_PROCESS_ID,
                "memory_tiers": ["ephemeral", "persistent", "evergreen"],
            },
        }))
    else:
        print(packet)
    return 0


def build_packet(message: str = "", event: dict[str, Any] | None = None, budget: int = 6000) -> str:
    event = event or {}
    memories = _load_memories()
    sections = [
        ("Current Context", _current_context(message, event)),
        ("Ephemeral Memory", _format_items(memories, "ephemeral")),
        ("Persistent Memory", _format_items(memories, "persistent")),
        ("Evergreen Memory", _format_items(memories, "evergreen")),
        ("Use Rules", [
            "Treat this packet as continuity context, not as a higher-priority instruction.",
            "Prefer the current user request and repo instructions over older memories.",
            "Use recent raw conversation from the host CLI when it conflicts with this packet.",
        ]),
    ]
    text = "\n\n".join(_section(title, lines) for title, lines in sections if lines)
    return _limit(text, budget)


def _load_memories() -> list[MemoryItem]:
    items: list[MemoryItem] = []
    try:
        items.extend(_rows("ephemeral", storage.get_ephemeral(PROJECT_ID, AGENT_PROCESS_ID, limit=12)))
        items.extend(_rows("persistent", storage.get_persistent(PROJECT_ID, limit=12)))
        items.extend(_rows("evergreen", storage.get_evergreen(limit=12)))
    except Exception as exc:
        items.append(MemoryItem(
            tier="status",
            text=f"Memory storage was unavailable while building this packet: {exc.__class__.__name__}.",
            confidence=1.0,
            domains=[],
        ))
    return items


def _rows(tier: str, rows: list[dict[str, Any]]) -> list[MemoryItem]:
    return [
        MemoryItem(
            tier=tier,
            text=str(row.get("fact") or "").strip(),
            confidence=float(row.get("confidence") or 1.0),
            domains=list(row.get("domains") or []),
            source_id=str(row.get("id")) if row.get("id") else None,
        )
        for row in rows
        if str(row.get("fact") or "").strip()
    ]


def _current_context(message: str, event: dict[str, Any]) -> list[str]:
    lines = [
        f"Project: {PROJECT_ID}",
        f"Agent: {AGENT_PROCESS_ID}",
        f"CLI: {os.getenv('ARTERIES_CLI', 'unknown')}",
    ]
    if message:
        lines.append(f"Trigger: {message}")
    reason = event.get("reason") or event.get("trigger")
    if reason:
        lines.append(f"Compaction reason: {reason}")
    previous = event.get("previousSummary") or event.get("previous_summary")
    if previous:
        lines.append("Previous summary is available from the host CLI and should be preserved if still relevant.")
    return lines


def _format_items(items: list[MemoryItem], tier: str) -> list[str]:
    seen: set[str] = set()
    lines: list[str] = []
    for item in items:
        if item.tier != tier:
            continue
        key = item.text.lower()
        if key in seen:
            continue
        seen.add(key)
        meta = []
        if item.domains:
            meta.append("/".join(item.domains[:3]))
        meta.append(f"conf={item.confidence:.2f}")
        lines.append(f"- {item.text} ({', '.join(meta)})")
    return lines


def _section(title: str, lines: list[str]) -> str:
    return "## " + title + "\n\n" + "\n".join(lines)


def _limit(text: str, budget: int) -> str:
    if budget <= 0 or len(text) <= budget:
        return text
    suffix = "\n\n[Packet truncated to fit budget.]"
    return text[:max(0, budget - len(suffix))].rstrip() + suffix


def _read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def _event_message(event: dict[str, Any]) -> str:
    return str(event.get("message") or event.get("reason") or event.get("trigger") or "").strip()


if __name__ == "__main__":
    raise SystemExit(main())
