"""Continuity packet assembly for CLI context pressure events."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from arteries import actionlog, degrade, memory_select, rank, runlog, storage, triage
from arteries import frame as frame_mod
from arteries.cli_caps import get_capabilities
from arteries.conversation import recent_assistant_turns
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
    parser.add_argument("--format",
                        choices=("markdown", "pi-compaction-json", "provenance-json"),
                        default="markdown")
    parser.add_argument("--message", default="", help="current user message or compaction reason")
    parser.add_argument("--budget", type=int, default=20000, help="approximate character budget")
    parser.add_argument("--stdin-json", action="store_true", help="read CLI event JSON from stdin")
    args = parser.parse_args(argv)

    event = read_stdin_json() if args.stdin_json else {}
    message = args.message or _event_message(event)
    provenance: list[dict[str, Any]] = []
    gate: dict[str, Any] = {}
    packet = build_packet(message=message, event=event, budget=args.budget,
                          provenance=provenance, gate_out=gate)
    capabilities = get_capabilities()

    if args.format == "provenance-json":
        # the packet, what went into it, and the frame itself. The frame is
        # here because capillaries' `cap find --context` takes exactly this
        # shape and arteries owns the type -- serialising it anywhere else
        # would make a second place that has to track the contract.
        print(json.dumps({"packet": packet, "memories": provenance,
                          "corpus": gate,
                          "project": PROJECT_ID, "agent_id": AGENT_PROCESS_ID}))
        return 0

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


#: Coverage above which the gate abstains: the situation is already answered by
#: this session's memory, so a corpus lookup is wasted work. Deliberately high
#: and configurable -- eval.py measures this distribution precisely because the
#: threshold has not been chosen from data yet, and a gate that abstains too
#: eagerly is invisible: the agent just works without a prompt it should have
#: had. Erring toward searching keeps that failure out of the default.
GATE_COVERAGE_ABSTAIN = float(os.getenv("ARTERIES_GATE_COVERAGE", "0.92"))


def _corpus_suggestion(message: str, embedding: list[float] | None,
                       provenance: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Consult capillaries, if the gate says this turn needs it.

    The gate lives here because arteries owns it: capillaries "does not own a
    second retrieval path or a pre-retrieval gate" by its own account, and heart
    is downstream of both. Heart asking capillaries directly would call it every
    turn -- which is the work this exists to skip -- and would reach around the
    layer that feeds it.

    Best-effort in both directions: no capillaries installed, or a corpus that
    is down, leaves the packet exactly as it was.
    """
    try:
        coverage = storage.max_ephemeral_similarity(
            PROJECT_ID, AGENT_PROCESS_ID, embedding) if embedding else 0.0
    except Exception:
        coverage = 0.0          # unknown coverage reads as none, and searches

    if coverage >= GATE_COVERAGE_ABSTAIN:
        actionlog.log_decision(
            "retrieval.gate", chosen_action="abstain",
            available_actions=["abstain", "search"],
            observation={"reason": "already covered by session memory",
                         "coverage": round(coverage, 3)})
        return {"status": "abstained", "coverage": round(coverage, 3)}

    actionlog.log_decision(
        "retrieval.gate", chosen_action="search",
        available_actions=["abstain", "search"],
        observation={"reason": "not covered by session memory",
                     "coverage": round(coverage, 3)})
    # The daemon, not find_sync in-process. find() mints no trace_id -- that is
    # the HTTP router's doing -- and without one the outcome reported after the
    # episode has nothing to attach to, so the feedback half of the loop cannot
    # exist. `source` is deliberately unset: it is an eligibility filter, and
    # naming the caller there filtered all 1033 prompts out.
    import dataclasses
    import urllib.request

    try:
        frame = frame_mod.get_current_frame(message, embedding)
        body = json.dumps({"situation": message,
                           "memory_context": dataclasses.asdict(frame)}).encode()
        url = os.getenv("CAPILLARIES_URL", "http://127.0.0.1:8000") + "/agent/route"
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            found = json.load(resp)
    except Exception as exc:
        return {"status": "unavailable", "reason": str(exc)[:160]}

    confidence = float(found.get("confidence") or 0.0)
    mode = found.get("mode") or "none"
    # mode is the whole check. capillaries applies its own floor first --
    # config/paths.py clears_floor, CAPILLARIES_MIN_CONFIDENCE, currently 0.8 --
    # and returns mode="none" carrying the rejected score, so anything served
    # here has already cleared a bar far above any second one worth writing.
    # A 0.3 used to sit here, copied from capillaries' CLI docs; it could never
    # fire, and read like a tuning knob that did nothing. The same borrowed
    # constant is documented going stale once already at config.py's
    # RELEVANCE_THRESHOLD. The real knob is CAPILLARIES_MIN_CONFIDENCE.
    if mode == "none":
        return {"status": "no_match", "confidence": round(confidence, 3),
                "coverage": round(coverage, 3)}
    # /agent/route nests the payload one level down; find_sync returns it flat.
    # Reading only the flat shape got a trace_id and an empty prompt.
    rec = found.get("recommendation") or found
    title = rec.get("title")
    if provenance is not None:
        provenance.append({"tier": "corpus", "id": found.get("trace_id") or "",
                           "score": round(confidence, 4), "title": title})
    return {"status": "ok", "mode": mode, "title": title,
            "confidence": round(confidence, 3), "coverage": round(coverage, 3),
            "trace_id": found.get("trace_id"),
            "text": rec.get("prompt_text") or ""}


def _query_embedding(message: str) -> list[float] | None:
    try:
        return embed_text_sync(message, is_query=True) if message else None
    except Exception:
        return None


def _frame_dict(message: str) -> dict[str, Any]:
    """The MemoryFrame as plain JSON, or {} if memory is unreachable.

    Best-effort by the same rule as the packet itself: a retrieval that cannot
    be enriched is worth doing unenriched, not worth failing a turn over.
    """
    import dataclasses

    try:
        from arteries import frame as frame_mod

        return dataclasses.asdict(frame_mod.get_current_frame(message))
    except Exception:
        return {}


def build_packet(message: str = "", event: dict[str, Any] | None = None,
                 budget: int = 20000,
                 provenance: list[dict[str, Any]] | None = None,
                 gate_out: dict[str, Any] | None = None) -> str:
    """Render a continuity packet. Pass `provenance` to also collect which
    records went into it.

    The ids exist all the way through selection -- _dedupe_by_id_or_fact works
    on them -- and were dropped at the boundary into MemoryItem, so a caller
    could see the text and never what produced it. Anything training retrieval
    on episode outcome needs that link: outcomes without a record of what was
    retrieved give you no way to attribute, and nothing to learn from.
    """
    event = event or {}
    memories = _load_memories(message, event, provenance)
    suggestion = _corpus_suggestion(message, _query_embedding(message), provenance)
    if gate_out is not None:
        gate_out.update({k: v for k, v in suggestion.items() if k != "text"})
    recent_pairs = _load_recent_pairs(event)
    allocations = _allocations(budget)
    sections = [
        ("Current Context", _limit_lines(_current_context(message, event), allocations["context"])),
        ("Recent Conversation", _limit_lines(_format_recent_pairs(recent_pairs), allocations["recent"])),
        ("Ephemeral Memory", _limit_lines(_format_items(memories, "ephemeral"), allocations["memory"])),
        ("Persistent Memory", _limit_lines(_format_items(memories, "persistent"), allocations["memory"])),
        ("Scope Memory", _limit_lines(_format_items(memories, "evergreen"), allocations["memory"])),
        ("Suggested Approach", _limit_lines(
            [suggestion["text"]] if suggestion.get("text") else [],
            allocations["suggestion"])),
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
# Keys are *arms* -- ranking lanes -- not packet sections. "related" holds claims
# reached through the graph; they are persistent rows and render as such, so the
# packet gains a lane, not a heading. Branch B adds an "evergreen" arm here when
# that tier exists.
TIER_WEIGHT = {"ephemeral": 1.00, "persistent": 0.95, "evergreen": 0.92,
               "related": 0.90}

# A graph neighbour is relevant by association, never by wording, so it must not
# crowd out direct hits. Bounded rather than floored: the floor was the wrong
# instrument (see above), but "at most this many" is still worth saying.
MAX_GRAPH_MEMORIES = 3


def _score(tier: str, row: dict[str, Any]) -> float | None:
    """Admission and ordering **within the persistent arm**, or None if refused.

    Similarity alone. `confidence` used to multiply it, and finding 4 measured
    what that bought: 448 of 527 live rows sit at 0.9 or above, so for 85% of the
    store the factor is a constant, and for the remaining 70 it silently demotes
    the most relevant row available on the grounds that the compiler was slightly
    less sure when it wrote it. Relevance and certainty are different questions
    and multiplying them answers neither.

    Confidence is still stored and still rendered on every line, which is what an
    annotation is for -- the reader can discount a 0.7 claim themselves.

    No longer a cross-tier comparison either; see TIER_WEIGHT.
    MEMORY_SIMILARITY_FLOOR was calibrated on query-to-claim cosine and applies
    only to rows carrying one.
    """
    similarity = row.get("similarity")
    if similarity is not None and float(similarity) < MEMORY_SIMILARITY_FLOOR:
        return None
    return NEUTRAL_SIMILARITY if similarity is None else float(similarity)


def _arms(ephemerals: list[dict[str, Any]],
          persistents: list[dict[str, Any]],
          evergreens: list[dict[str, Any]] | None = None,
          already_shown: set[str] | None = None) -> list[tuple[str, list[dict[str, Any]]]]:
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

    # Demoted, not dropped. A claim shown last turn that is still the best answer
    # should still appear; it just should not outrank something new. Dropping it
    # outright would make a packet worse the longer a session ran.
    shown = already_shown or set()

    def _rank_within(rows):
        return sorted(rows, key=lambda r: str(r.get("id") or "") in shown)

    return [
        # Already newest-first from storage.get_ephemeral; recency IS the ranking.
        ("ephemeral", _rank_within(ephemerals)),
        ("persistent", _rank_within([r for _s, r in scored_direct])),
        ("evergreen", _rank_within(list(evergreens or []))),
        ("related", reached[:MAX_GRAPH_MEMORIES]),
    ]


# An arm is how a row was ranked; a tier is where it renders. Graph neighbours
# are persistent rows reached sideways, so they belong in the Persistent section,
# marked with the edge that led to them rather than filed under a heading of
# their own.
ARM_TIER = {"ephemeral": "ephemeral", "persistent": "persistent",
            "evergreen": "evergreen", "related": "persistent"}


def _fuse(arms: list[tuple[str, list[dict[str, Any]]]]) -> list[tuple[str, dict[str, Any]]]:
    """Reciprocal rank fusion across arms. Returns (arm, row), best first."""
    return rank.fuse(
        [(arm, rows, TIER_WEIGHT.get(arm, 1.0)) for arm, rows in arms],
        key=lambda row: str(row.get("id") or id(row)),
    )


def _load_memories(message: str, event: dict[str, Any] | None = None,
                   provenance: list[dict[str, Any]] | None = None) -> list[MemoryItem]:
    items: list[MemoryItem] = []
    try:
        # Ask before embedding: a message with no object to search for should
        # not cost an embed call, and the rows it would return are ranked
        # results of searching for nothing.
        no_query = triage.skip_reason(message, recent_assistant_turns()) if message else None
        msg_vec = embed_text_sync(message, is_query=True) if message and not no_query else None
        ephemerals, persistents = memory_select.select_for_frame(
            message, embedding=msg_vec, similarity_search=not no_query)
        evergreens = (memory_select.select_evergreen(
            message, memory_select.context_from_env(), msg_vec)
            if not no_query else [])

        # Finding 19. What the last couple of packets already showed, so this one
        # can differ from them instead of re-sending the same claims every turn.
        already_shown = storage.recent_packet_members(PROJECT_ID)
        if no_query:
            runlog.log_event("memory.retrieval.skipped", "arteries",
                             {"reason": no_query}, project_id=PROJECT_ID,
                             agent_id=AGENT_PROCESS_ID)

        members: list[str] = []
        for rank, (arm, row) in enumerate(
                _fuse(_arms(ephemerals, persistents, evergreens,
                            already_shown))[:MAX_PACKET_MEMORIES],
                start=1):
            if row.get("id"):
                members.append(str(row["id"]))
            items.extend(_rows(ARM_TIER[arm], [row]))
            if provenance is not None and row.get("id"):
                provenance.append({
                    # `arm` rather than the rendered tier: what is being recorded
                    # is how the row was ranked, which is what training on
                    # retrieval outcome needs to attribute.
                    "tier": arm,
                    "id": str(row["id"]),
                    # Fused rank, not a score. After RRF the number a row carries
                    # is not comparable across runs with different arm sizes;
                    # its position is.
                    "rank": rank,
                    "task_id": row.get("task_id"),
                    "episode_id": row.get("episode_id"),
                })
        storage.record_packet(PROJECT_ID, members,
                              agent_process_id=AGENT_PROCESS_ID)
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
    summary_words = _content_words(previous_summary)
    for item in items:
        if item.tier == "status":
            out.append(item)
            continue
        key = _norm(item.text)
        if not key or key in seen:
            continue
        if _already_covered(item.text, summary_words):
            continue
        seen.add(key)
        out.append(item)
    return out


# Finding 21: dedupe was `key in previous_summary` -- substring containment on
# normalized text. That over-merges, because a claim that happens to be a prefix
# of a longer sentence in the summary is dropped even when it says something the
# summary does not, and it under-merges on any rewording at all, because one
# changed character breaks containment entirely.
#
# Word overlap instead: what fraction of this claim's content words the summary
# already contains. Still shallow -- paraphrase with different vocabulary is the
# compiler's job -- but it degrades sensibly instead of flipping.
SUMMARY_OVERLAP = float(os.getenv("ARTERIES_SUMMARY_OVERLAP", "0.8"))


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_./]+", (text or "").lower()) if len(w) > 3}


def _already_covered(text: str, summary_words: set[str]) -> bool:
    """Compared on raw text, not on `_norm`ed text.

    `_norm` strips punctuation, so `claimed_at` becomes "claimed at" while the
    untouched summary still holds `claimed_at` -- identical tokens that never
    match. Both sides go through the same tokenizer now, which is the only way
    an overlap number means anything.
    """
    if not summary_words:
        return False
    words = _content_words(text)
    if not words:
        return False
    return len(words & summary_words) / len(words) >= SUMMARY_OVERLAP


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

    ordered: list[RecentPair] = []
    index_by_turn: dict[str, int] = {}
    responses: list[tuple[str | None, bool, str, str]] = []
    session = os.getenv("ARTERIES_SESSION_ID") or ""

    for event in reversed(events):
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        turn_id = str(event.get("turn_id") or "") or None
        if _other_session(payload, session):
            continue

        if event_type == "turn.observed":
            user = payload_text(payload, "message_preview", "message", "prompt", "user")
            if not user:
                continue
            if turn_id:
                index_by_turn[turn_id] = len(ordered)
            ordered.append(RecentPair(user=user))
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
            # An edited/resubmitted prompt fires the capture again, so the same
            # assistant text arrives twice under two turn_ids. Printing it under
            # two different questions is always wrong; one of them is a ghost.
            answers = payload_text(payload, "answers_preview")
            # An edited/resubmitted prompt fires the capture again, so the same
            # assistant text arrives twice under two turn_ids. Printing it under
            # two different questions is always wrong; one of them is a ghost.
            if assistant and (not responses or responses[-1][3] != assistant):
                responses.append((turn_id, bool(payload.get("prior_turn")), answers, assistant))

    # Resolved in a second pass because a capture fires at the start of turn N
    # while describing turn N-1, and it can reach the store before turn N's own
    # turn.observed row. Anchoring during the first pass therefore looked up a
    # turn that did not exist yet.
    keys = [_norm(pair.user) for pair in ordered]
    for turn_id, prior_turn, answers, assistant in responses:
        index = _match_question(keys, answers) if answers else None
        if index is None:
            index = _by_position(index_by_turn.get(turn_id or ""), prior_turn, ordered)
        if index is None:
            continue
        # Last capture wins: a turn can be captured more than once, and the
        # later read of the transcript is always the more complete one.
        ordered[index].assistant = assistant

    return ordered[-limit:]


def _other_session(payload: dict[str, Any], session: str) -> bool:
    """True when this event belongs to a different session of the same CLI.

    Runs are keyed by (repo, CLI), deliberately, so every Claude session in one
    repo shares a run and the packet was merging concurrent conversations into a
    single Recent Conversation -- other people's questions, answered by nobody.
    Session is the finer key the host CLI already hands us.

    Rows written before session stamping carry no session_id and are kept: a
    packet with some extra turns beats one that renders empty.
    """
    if not session:
        return False
    row = str(payload.get("session_id") or "")
    return bool(row) and row != session


def _match_question(keys: list[str], answers: str) -> int | None:
    """Index of the question an answer names, latest match first.

    Both sides are stored truncated -- the question at 2000 chars, the join key
    at 200 -- so they are compared on whichever prefix is shorter. Latest wins
    because a resubmitted prompt leaves an identical earlier row behind and the
    live one is the later of the two.
    """
    key = _norm(answers)
    if not key:
        return None
    for index in reversed(range(len(keys))):
        candidate = keys[index]
        if not candidate:
            continue
        width = min(len(candidate), len(key))
        if candidate[:width] == key[:width]:
            return index
    return None


def _by_position(anchor: int | None, prior_turn: bool, ordered: list[RecentPair]) -> int | None:
    """Fallback for rows with no join key: rows written before answers_preview
    existed, and transcripts whose parent chain fell out of the tail window.

    Counting is what the join key replaced, so it stays conservative: a
    prior_turn row with no anchor, or one whose subject sits outside the
    window, is dropped rather than guessed onto a neighbour.
    """
    if prior_turn:
        return anchor - 1 if anchor else None
    if anchor is not None:
        return anchor
    return next((i for i in reversed(range(len(ordered))) if not ordered[i].assistant), None)


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


def _allocations(budget: int, capabilities: Any = None) -> dict[str, int]:
    """Byte shares per section. Depends on whether the host still has the
    conversation.

    `Recent Conversation` took 55% of the budget unconditionally (finding 18).
    That is the one section every host CLI keeps verbatim -- Claude's compaction
    prompt and Cursor's watermarks exist specifically to avoid re-sending it --
    so on the injection path more than half the packet was spending its budget
    telling the model what it had just read, while memory got 12%.

    It is not always redundant. When the packet *replaces* the host's compaction
    output, the recent turns are the only record of the conversation and dropping
    them loses the thing being compacted. So the split follows the capability
    rather than a single number.

    _load_recent_pairs asks for 10 pairs and _one_line caps each side at 500
    chars, so ~10k is what delivering all ten actually costs. Shares sum to 0.96,
    leaving headroom under the hard _limit() so the tail section is never the one
    clipped.
    """
    budget = max(budget, 1)
    capabilities = capabilities or get_capabilities()
    replacing = getattr(capabilities, "can_replace_compaction", False)
    memory = budget * (0.12 if replacing else 0.52)
    return {
        "context": int(budget * 0.10),
        "recent": int(budget * (0.55 if replacing else 0.15)),
        # Ephemeral and Persistent are two headings over one ranked set of at
        # most MAX_PACKET_MEMORIES rows, so they share this budget rather than
        # each taking it. Applying the same number to both is how the old shares
        # summed to 1.08 while the comment claimed 0.96 -- and over-allocating
        # hands the decision back to truncation, which is what ranking exists to
        # take away from it.
        "memory": int(memory / 2),
        "suggestion": int(budget * 0.10),
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
