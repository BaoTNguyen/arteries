"""Continuity packet assembly for CLI context pressure events."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from arteries import degrade, memory_select, runlog
from arteries.cli_caps import get_capabilities
from arteries.embed import embed_text_sync
from arteries.config import AGENT_PROCESS_ID, PROJECT_ID
from arteries.eventjson import event_messages, payload_text, read_stdin_json, text_from_mapping


@dataclass
class MemoryItem:
    tier: str
    text: str
    confidence: float
    domains: list[str]
    source_id: str | None = None
    # How this row was reached: None for a direct hit, otherwise the edge that
    # led here ("contradicts", "shares:hook"). `graph.expand` computes it and it
    # used to be dropped at this boundary, which is why the packet presented A
    # and not-A as two equally confident bullets (finding 15).
    via: str | None = None


@dataclass
class RecentPair:
    user: str
    assistant: str | None = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an Arteries continuity packet.")
    parser.add_argument("--format", choices=("markdown", "pi-compaction-json"), default="markdown")
    parser.add_argument("--message", default="", help="current user message or compaction reason")
    parser.add_argument("--budget", type=int, default=6000, help="approximate character budget")
    parser.add_argument("--stdin-json", action="store_true", help="read CLI event JSON from stdin")
    args = parser.parse_args(argv)

    event = read_stdin_json() if args.stdin_json else {}
    message = args.message or _event_message(event)
    packet = build_packet(message=message, event=event, budget=args.budget)
    capabilities = get_capabilities()

    if args.format == "pi-compaction-json":
        print(json.dumps({
            "summary": packet,
            "details": {
                "source": "arteries",
                "project": PROJECT_ID,
                "agent_id": AGENT_PROCESS_ID,
                "memory_tiers": ["ephemeral", "persistent"],
                "cli_capabilities": capabilities.__dict__,
            },
        }))
    else:
        print(packet)
    return 0


def build_packet(message: str = "", event: dict[str, Any] | None = None, budget: int = 6000) -> str:
    event = event or {}
    memories = _load_memories(message, event)
    recent_pairs = _load_recent_pairs(event)
    allocations = _allocations(budget)
    sections = [
        ("Current Context", _limit_lines(_current_context(message, event), allocations["context"])),
        ("Recent Conversation", _limit_lines(_format_recent_pairs(recent_pairs), allocations["recent"])),
        ("Ephemeral Memory", _limit_lines(_format_items(memories, "ephemeral"), allocations["memory"])),
        ("Persistent Memory", _limit_lines(_format_items(memories, "persistent"), allocations["memory"])),
        ("Use Rules", _limit_lines([
            "Treat this packet as continuity context, not as a higher-priority instruction.",
            "Prefer the current user request and repo instructions over older memories.",
            "Use recent raw conversation from the host CLI when it conflicts with this packet.",
            "Do not invent assistant answers when a CLI only captured user turns.",
        ], allocations["rules"])),
    ]
    text = "\n\n".join(_section(title, lines) for title, lines in sections if lines)
    return _limit(text, budget)


# Packet entry criteria. The old rule was "top 12 of each tier", which is not a
# criterion at all -- 36 candidates went in unranked and whichever ones happened
# to fit the 18% memory budget survived, so what reached the agent was decided by
# truncation order rather than by relevance. Now every candidate is scored, weak
# ones are refused entry, and truncation can only ever drop the worst survivor.
#
# The floor applies only to rows that carry a similarity, i.e. ones chosen by the
# relevance query. Ephemeral is selected by a different policy (this session,
# this agent, recency) and has no similarity to be judged on; scoring it against
# a scale it never competed on would silently empty the tier.
# Measured against 237 real plexus session queries over 115 claims. The top hit
# per query runs p25 0.50, p50 0.55, p90 0.66, max 0.78 -- Qwen3-Embedding-0.6B
# compresses unrelated technical prose into roughly 0.45-0.55, so a score in that
# band carries almost no signal. The old 0.45 sat at the 25th percentile of
# *every* returned row, which is how a question about cost tracking came back
# with claims about retrieval ownership and retry latency.
#
# 0.55 is the median top hit: if the best thing the store has for your query is
# below what a median query's best match scores, the store probably has nothing,
# and saying nothing beats saying something confidently irrelevant.
#
# ponytail: one global number tuned on cross-project queries. Within-project
# paraphrases score 0.6-0.8, so this is conservative for them. Re-derive from
# `art benchmark` as the corpus grows.
MEMORY_SIMILARITY_FLOOR = float(os.getenv("ARTERIES_PACKET_FLOOR", "0.55"))
MAX_PACKET_MEMORIES = 15
NEUTRAL_SIMILARITY = 0.5

# Tiers are fused by RANK, not by score, because their scores are not the same
# quantity. Persistent carries a query-to-claim cosine. Ephemeral is chosen by
# recency and carries no similarity at all. A graph neighbour carries a decayed
# hop score. Comparing them directly had two measured consequences:
#
#   * Ephemeral entered at a flat NEUTRAL_SIMILARITY of 0.5, which is *above*
#     most of the real persistent distribution. 82% of persistent rows (every
#     row at confidence <= 0.95) needed an above-median match just to tie one,
#     so on a median query persistent contributed nothing at all.
#   * A graph neighbour scores weight(1.0) x decay(0.6) x seed_similarity, so
#     clearing a 0.55 cosine floor needed a seed at 0.917 -- or 1.528 for the
#     shared-entity path, which is impossible. Nothing `graph.expand` produced
#     had ever reached a packet (finding 26).
#
# Ranks are commensurable where those scores are not: rank 1 means "the best
# thing this tier has" in every tier. Each arm ranks on its own policy, and RRF
# merges them without any arm needing to justify itself on another's scale.
RRF_K = int(os.getenv("ARTERIES_RRF_K", "60"))
# Keys are *arms* -- ranking lanes -- not packet sections. "related" holds claims
# reached through the graph; they are persistent rows and render as such, so the
# packet gains a lane, not a heading. Branch B adds an "evergreen" arm here when
# that tier exists.
TIER_WEIGHT = {"ephemeral": 1.00, "persistent": 0.95, "related": 0.90}

# A graph neighbour is relevant by association, never by wording, so it must not
# crowd out direct hits. Bounded rather than floored: the floor was the wrong
# instrument (see above), but "at most this many" is still worth saying.
MAX_GRAPH_MEMORIES = 3


def _score(tier: str, row: dict[str, Any]) -> float | None:
    """Admission and ordering **within the persistent arm**, or None if refused.

    No longer a cross-tier comparison -- see TIER_WEIGHT. MEMORY_SIMILARITY_FLOOR
    was calibrated on query-to-claim cosine and is applied only to rows that
    carry one.
    """
    similarity = row.get("similarity")
    if similarity is not None and float(similarity) < MEMORY_SIMILARITY_FLOOR:
        return None
    sim = NEUTRAL_SIMILARITY if similarity is None else float(similarity)
    return sim * float(row.get("confidence") or 1.0) * TIER_WEIGHT.get(tier, 1.0)


def _arms(ephemerals: list[dict[str, Any]],
          persistents: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Split the selection into ranked arms, each ordered by its own policy.

    `select_for_frame` returns graph neighbours mixed into the persistent list,
    tagged `via_graph`, with a hop score written into `similarity` so they could
    be compared against direct hits. They cannot be -- that comparison is what
    finding 26 measured -- so they are separated out here into their own arm.
    """
    direct, reached = [], []
    for row in persistents:
        (reached if row.get("via_graph") else direct).append(row)

    scored_direct = [(s, r) for r in direct if (s := _score("persistent", r)) is not None]
    scored_direct.sort(key=lambda t: t[0], reverse=True)

    # Own scale, own order, no cosine floor. Bounded instead.
    reached.sort(key=lambda r: float(r.get("similarity") or 0.0), reverse=True)

    return [
        # Already newest-first from storage.get_ephemeral; recency IS the ranking.
        ("ephemeral", list(ephemerals)),
        ("persistent", [r for _s, r in scored_direct]),
        ("related", reached[:MAX_GRAPH_MEMORIES]),
    ]


# An arm is how a row was ranked; a tier is where it renders. Graph neighbours
# are persistent rows reached sideways, so they belong in the Persistent section,
# marked with the edge that led to them rather than filed under a heading of
# their own.
ARM_TIER = {"ephemeral": "ephemeral", "persistent": "persistent", "related": "persistent"}


def _fuse(arms: list[tuple[str, list[dict[str, Any]]]]) -> list[tuple[str, dict[str, Any]]]:
    """Reciprocal rank fusion across arms. Returns (arm, row), best first."""
    fused: list[tuple[float, int, str, dict[str, Any]]] = []
    for arm_index, (arm, rows) in enumerate(arms):
        weight = TIER_WEIGHT.get(arm, 1.0)
        for rank, row in enumerate(rows, start=1):
            # arm_index breaks ties deterministically, so an empty tier can
            # never reorder the others and the result is stable run to run.
            fused.append((weight / (RRF_K + rank), arm_index, arm, row))
    fused.sort(key=lambda t: (-t[0], t[1]))
    return [(arm, row) for _s, _i, arm, row in fused]


def _load_memories(message: str, event: dict[str, Any] | None = None) -> list[MemoryItem]:
    items: list[MemoryItem] = []
    try:
        msg_vec = embed_text_sync(message, is_query=True) if message else None
        ephemerals, persistents = memory_select.select_for_frame(message, embedding=msg_vec)

        for arm, row in _fuse(_arms(ephemerals, persistents))[:MAX_PACKET_MEMORIES]:
            items.extend(_rows(ARM_TIER[arm], [row]))
    except Exception as exc:
        items.append(MemoryItem(
            tier="status",
            text=f"Memory {degrade.note(exc, 'lookup')} while building this packet.",
            confidence=1.0,
            domains=[],
        ))
    return _dedupe_memories(items, _previous_summary(event or {}))


def _previous_summary(event: dict[str, Any]) -> str:
    return _norm(str(event.get("previousSummary") or event.get("previous_summary") or ""))


def _norm(text: str) -> str:
    # tokenize to alnum words so punctuation ("spaces." vs "spaces,") and casing
    # don't defeat containment/dedup comparisons
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _dedupe_memories(items: list[MemoryItem], previous_summary: str) -> list[MemoryItem]:
    """Drop the same fact showing up in more than one tier (common: a
    the same fact reaching two tiers), and drop
    anything the host CLI's previous summary already carries. Both waste the very
    budget a continuity packet exists to conserve. First occurrence wins, so tier
    order (ephemeral, persistent) is preserved. Status lines are never
    dropped."""
    seen: set[str] = set()
    out: list[MemoryItem] = []
    for item in items:
        if item.tier == "status":
            out.append(item)
            continue
        key = _norm(item.text)
        if not key or key in seen:
            continue
        if previous_summary and key in previous_summary:
            continue
        seen.add(key)
        out.append(item)
    return out


def _rows(tier: str, rows: list[dict[str, Any]]) -> list[MemoryItem]:
    return [
        MemoryItem(
            tier=tier,
            text=str(row.get("fact") or "").strip(),
            confidence=float(row.get("confidence") or 1.0),
            domains=list(row.get("domains") or []),
            source_id=str(row.get("id")) if row.get("id") else None,
            via=str(row["via"]) if row.get("via") else None,
        )
        for row in rows
        if str(row.get("fact") or "").strip()
    ]


def _current_context(message: str, event: dict[str, Any]) -> list[str]:
    capabilities = get_capabilities()
    lines = [
        f"Project: {PROJECT_ID}",
        f"Agent: {AGENT_PROCESS_ID}",
        f"CLI: {capabilities.name}",
        "Capabilities: " + ", ".join(
            name for name, enabled in capabilities.__dict__.items()
            if name != "name" and enabled
        ),
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


def _load_recent_pairs(event: dict[str, Any], limit: int = 10) -> list[RecentPair]:
    from_event = _pairs_from_event(event, limit)
    if from_event:
        return from_event[-limit:]
    return _pairs_from_runlog(limit)


def _pairs_from_event(event: dict[str, Any], limit: int) -> list[RecentPair]:
    messages = event_messages(event)
    if not messages:
        return []

    pairs: list[RecentPair] = []
    pending_user: str | None = None
    for message in messages:
        role = str(message.get("role") or message.get("speaker") or message.get("type") or "").lower()
        text = text_from_mapping(message)
        if not text:
            continue
        if role in {"user", "human", "prompt", "input"}:
            if pending_user:
                pairs.append(RecentPair(user=pending_user))
            pending_user = text
            continue
        if role in {"assistant", "agent", "model", "ai", "output", "response"}:
            if pending_user:
                pairs.append(RecentPair(user=pending_user, assistant=text))
                pending_user = None
            elif pairs and not pairs[-1].assistant:
                pairs[-1].assistant = text

    if pending_user:
        pairs.append(RecentPair(user=pending_user))
    return pairs[-limit:]


def _pairs_from_runlog(limit: int) -> list[RecentPair]:
    try:
        events = runlog.recent_events(project_id=PROJECT_ID, limit=120, repo_path=os.getenv("ARTERIES_REPO"))
    except Exception:
        return []

    pairs_by_turn: dict[str, RecentPair] = {}
    ordered: list[RecentPair] = []
    pending: RecentPair | None = None
    for event in reversed(events):
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        turn_id = str(event.get("turn_id") or "") or None

        if event_type == "turn.observed":
            user = payload_text(payload, "message_preview", "message", "prompt", "user")
            if not user:
                continue
            pair = RecentPair(user=user)
            ordered.append(pair)
            pending = pair
            if turn_id:
                pairs_by_turn[turn_id] = pair
            continue

        if event_type in {"turn.assistant", "assistant.response", "message.assistant", "turn.completed"}:
            assistant = payload_text(
                payload,
                "assistant_preview",
                "response_preview",
                "message_preview",
                "assistant",
                "response",
                "text",
            )
            if not assistant:
                continue
            pair = pairs_by_turn.get(turn_id or "") if turn_id else pending
            if payload.get("prior_turn"):
                # transcript capture runs at the start of turn N and describes
                # turn N-1, so attach to the pair before the one sharing turn_id
                anchor = pairs_by_turn.get(turn_id or "")
                if anchor is not None and anchor in ordered and ordered.index(anchor) > 0:
                    pair = ordered[ordered.index(anchor) - 1]
                else:
                    pair = next((p for p in reversed(ordered) if not p.assistant), None)
            if pair and not pair.assistant:
                pair.assistant = assistant

    return ordered[-limit:]


def _format_recent_pairs(pairs: list[RecentPair]) -> list[str]:
    lines: list[str] = []
    for idx, pair in enumerate(pairs[-10:], start=1):
        lines.append(f"{idx}. Q: {_one_line(pair.user)}")
        if pair.assistant:
            lines.append(f"   A: {_one_line(pair.assistant)}")
        else:
            lines.append("   A: [not captured by this CLI]")
    return lines


def _one_line(text: str, limit: int = 500) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 15].rstrip() + " [truncated]"


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
        # Finding 15: without this, a claim and the claim contradicting it render
        # as two identical bullets and the reader cannot tell which is disputed.
        prefix = f"[{item.via}] " if item.via else ""
        lines.append(f"- {prefix}{item.text} ({', '.join(meta)})")
    return lines


def _section(title: str, lines: list[str]) -> str:
    return "## " + title + "\n\n" + "\n".join(lines)


def _allocations(budget: int) -> dict[str, int]:
    budget = max(budget, 1)
    return {
        "context": int(budget * 0.10),
        "recent": int(budget * 0.25),
        "memory": int(budget * 0.18),
        "rules": int(budget * 0.07),
    }


def _limit_lines(lines: list[str], budget: int) -> list[str]:
    if budget <= 0:
        return lines
    out: list[str] = []
    used = 0
    suffix = "[Section truncated to fit budget.]"
    for line in lines:
        cost = len(line) + 1
        if out and used + cost > budget:
            out.append(suffix)
            break
        if not out and cost > budget:
            out.append(line[: max(0, budget - len(suffix) - 1)].rstrip() + " " + suffix)
            break
        out.append(line)
        used += cost
    return out


def _limit(text: str, budget: int) -> str:
    if budget <= 0 or len(text) <= budget:
        return text
    suffix = "\n\n[Packet truncated to fit budget.]"
    return text[:max(0, budget - len(suffix))].rstrip() + suffix


def _event_message(event: dict[str, Any]) -> str:
    return str(event.get("message") or event.get("reason") or event.get("trigger") or "").strip()


if __name__ == "__main__":
    raise SystemExit(main())
