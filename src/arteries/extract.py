"""
Sync-track ephemeral extraction.

Runs on every turn (~1-5ms heuristic, no model call). Extracts candidate
memories from the user message: domain tags, factual signals, preferences,
and context markers. Inserts them as ephemeral records.

This is the bootstrap extractor — heuristic-based, meant to be replaced
by a fine-tuned 1-3B model trained with RLVR once enough signal exists.
The async LLM track (compile.py) handles deep extraction in the background;
the sync track covers the current turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from arteries.config import AGENT_PROCESS_ID, EPHEMERAL_MODE, PARENT_AGENT_ID, PROJECT_ID
from arteries import storage

# Capillaries owns the domain taxonomy; prefer it so the two ends of the memory
# channel can't drift. But extraction is a pure-memory op — it must not hard-fail
# just because capillaries isn't installed. Fall back to arteries' own taxonomy
# (evergreen.DOMAIN_KEYWORDS) when capillaries is absent; drift only in that
# degraded mode, never when capillaries is present.
try:
    from capillaries.agent.inference import DOMAIN_KEYWORDS
except Exception:  # capillaries not installed / not importable
    from arteries.evergreen import DOMAIN_KEYWORDS

# Patterns that signal extractable facts
PREFERENCE_PATTERNS = re.compile(
    r"\b(i (?:prefer|like|want|need|use|always|never|hate|avoid))\b",
    re.IGNORECASE,
)
CONTEXT_PATTERNS = re.compile(
    r"\b(i(?:'m| am) (?:working on|building|using|debugging|trying to|looking at))\b",
    re.IGNORECASE,
)
FACT_PATTERNS = re.compile(
    r"\b(we (?:use|have|run|deploy|switched to|migrated to|adopted))\b",
    re.IGNORECASE,
)
CORRECTION_PATTERNS = re.compile(
    r"\b(no[,.]? (?:not that|i meant|actually|it should be|that's wrong))\b",
    re.IGNORECASE,
)

MIN_EXTRACTABLE_WORDS = 5


@dataclass
class Extraction:
    fact: str
    domains: list[str]
    confidence: float
    signal_type: str  # preference | context | fact | correction | domain_signal


def extract_from_message(message: str) -> list[Extraction]:
    """
    Extract candidate memories from a single user message.

    Returns a list of extractions. Each becomes one ephemeral record.
    The heuristic is intentionally permissive — the async LLM track
    will filter and refine during compilation, and RLVR will eventually
    learn what's worth keeping.
    """
    words = message.split()
    if len(words) < MIN_EXTRACTABLE_WORDS:
        return []

    text_lower = message.lower()
    extractions: list[Extraction] = []
    domains = _infer_domains(text_lower)

    # Extract sentences that match signal patterns
    sentences = re.split(r'[.!?\n]+', message)
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence.split()) < 3:
            continue

        signal = _classify_sentence(sentence)
        if signal:
            extractions.append(Extraction(
                fact=sentence,
                domains=domains,
                confidence=signal[1],
                signal_type=signal[0],
            ))

    # If no sentence-level signals but the message has clear domain content,
    # extract the whole message as a domain signal (lower confidence)
    if not extractions and domains:
        extractions.append(Extraction(
            fact=message[:500],
            domains=domains,
            confidence=0.5,
            signal_type="domain_signal",
        ))

    return extractions


_ephemeral_buffer: list[dict] = []


def get_ephemeral_buffer() -> list[dict]:
    return _ephemeral_buffer


def extract_and_store(message: str, embedding: list[float] | None = None) -> int:
    """Extract from message and insert into ephemeral storage. Returns count inserted.

    Every extraction from one turn came from one message, so they all share that
    message's vector -- there is nothing to gain from embedding each row
    separately, and doing so would put N HTTP calls on the hook path. The caller
    embeds the message once and hands the vector down.
    """
    extractions = extract_from_message(message)
    if EPHEMERAL_MODE == "discard":
        for ext in extractions:
            _ephemeral_buffer.append({
                "fact": ext.fact,
                "domains": ext.domains,
                "confidence": ext.confidence,
                "status": "ephemeral-only",
            })
        return len(extractions)
    for ext in extractions:
        storage.insert_ephemeral(
            project_id=PROJECT_ID,
            agent_process_id=AGENT_PROCESS_ID,
            fact=ext.fact,
            domains=ext.domains,
            confidence=ext.confidence,
            parent_agent_id=PARENT_AGENT_ID,
            embedding=embedding,
        )
    return len(extractions)


def _infer_domains(text: str) -> list[str]:
    scores: dict[str, int] = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[domain] = score
    if not scores:
        return []
    ranked = sorted(scores, key=lambda d: scores[d], reverse=True)
    top = [ranked[0]]
    if len(ranked) > 1 and scores[ranked[1]] >= scores[ranked[0]] * 0.7:
        top.append(ranked[1])
    return top


def _classify_sentence(sentence: str) -> tuple[str, float] | None:
    """Returns (signal_type, confidence) or None if no signal."""
    if CORRECTION_PATTERNS.search(sentence):
        return ("correction", 0.9)
    if PREFERENCE_PATTERNS.search(sentence):
        return ("preference", 0.8)
    if FACT_PATTERNS.search(sentence):
        return ("fact", 0.75)
    if CONTEXT_PATTERNS.search(sentence):
        return ("context", 0.7)
    return None


# -- Assistant response → single ephemeral record for LLM compilation ---------

_NARRATION = re.compile(
    r"^(let me|i'll|i will|now i|here's|here is|looking at|checking|reading|updating|creating)\b",
    re.IGNORECASE,
)
_CODE_FENCE = re.compile(r"^```")


def strip_assistant_response(text: str) -> str:
    """Remove code blocks, tool output, and narration. Returns substantive prose."""
    lines = text.splitlines()
    out: list[str] = []
    in_fence = False
    for line in lines:
        if _CODE_FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if _NARRATION.match(stripped):
            continue
        out.append(stripped)
    return "\n".join(out)


def store_assistant_response(text: str) -> int:
    """Strip and store an assistant response as a single ephemeral record.

    No pattern matching — the compilation LLM decides what's worth keeping.
    Returns 1 if stored, 0 if stripped to nothing.
    """
    stripped = strip_assistant_response(text)
    if len(stripped.split()) < MIN_EXTRACTABLE_WORDS:
        return 0
    # ponytail: cap at 1500 chars — longer responses are mostly code/narration anyway
    stripped = stripped[:1500]
    domains = _infer_domains(stripped.lower())
    if EPHEMERAL_MODE == "discard":
        _ephemeral_buffer.append({
            "fact": stripped,
            "domains": domains,
            "confidence": 0.5,
            "status": "ephemeral-only",
            "source": "assistant",
        })
        return 1
    storage.insert_ephemeral(
        project_id=PROJECT_ID,
        agent_process_id=AGENT_PROCESS_ID,
        fact=stripped,
        domains=domains,
        confidence=0.5,
        parent_agent_id=PARENT_AGENT_ID,
        source="assistant",
    )
    return 1
