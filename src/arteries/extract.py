"""
Sync-track ephemeral extraction.

Runs on every turn (~1-5ms heuristic, no model call). Extracts candidate
memories from the user message: domain tags, factual signals, preferences,
and context markers. Inserts them as ephemeral records.

This is the bootstrap extractor — heuristic-based, meant to be replaced
by a fine-tuned 1-3B model trained with RLVR once enough signal exists.
The async LLM track (not yet built) handles deep extraction on a one-turn
delay; the sync track covers the current turn from the 10-pair window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from arteries.config import AGENT_PROCESS_ID, PROJECT_ID
from arteries import storage

# Reuse capillaries' domain taxonomy for consistency
DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "technical": ["code", "programming", "software", "api", "server", "database", "docker", "kubernetes", "devops", "infrastructure", "backend", "frontend", "web", "app", "algorithm", "system", "architecture", "python", "javascript", "java", "go", "rust", "sql", "linux", "git", "testing", "debugging", "refactor"],
    "AI": ["ai", "machine learning", "ml", "model", "llm", "gpt", "embedding", "training", "inference", "prompt", "rag", "vector", "nlp", "neural", "deep learning", "transformer", "classification", "clustering"],
    "business": ["business", "revenue", "growth", "market", "customer", "sales", "marketing", "strategy", "company", "enterprise", "roi", "kpi", "metric", "budget", "cost", "pricing"],
    "strategy": ["strategy", "strategic", "roadmap", "vision", "goal", "objective", "planning", "competitive", "advantage", "positioning"],
    "product": ["product", "feature", "user experience", "ux", "ui", "design", "requirement", "specification", "roadmap", "launch"],
    "finance": ["finance", "financial", "investment", "budget", "cost", "revenue", "profit", "pricing", "valuation", "financial model", "forecast", "cash flow"],
    "career": ["career", "job", "resume", "interview", "promotion", "salary", "skills", "professional", "development", "leadership"],
    "learning": ["learn", "study", "education", "course", "training", "knowledge", "skill", "practice", "teach"],
    "personal": ["personal", "life", "habit", "goal", "productivity", "health", "wellness", "organization"],
    "writing": ["write", "writing", "content", "copy", "blog", "article", "documentation", "story", "narrative"],
}

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


def extract_and_store(message: str) -> int:
    """Extract from message and insert into ephemeral storage. Returns count inserted."""
    extractions = extract_from_message(message)
    for ext in extractions:
        storage.insert_ephemeral(
            project_id=PROJECT_ID,
            agent_process_id=AGENT_PROCESS_ID,
            fact=ext.fact,
            domains=ext.domains,
            confidence=ext.confidence,
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
