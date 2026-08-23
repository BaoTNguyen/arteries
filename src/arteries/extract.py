"""Turn capture: what the hook hands to the compiler.

Every user turn becomes one ephemeral record, whole. There is no longer a
pattern filter here, because measurement showed there was never really one: of
202 stored rows, 12.9% matched the `preference` regex and 87.1% were the
whole-message fallback truncated to 500 characters. `fact`, `context`, and
`correction` had never fired once. The LLM compile pass was doing all the
filtering, and the truncation was losing signal on most of the corpus.

`extract_from_message` survives as a function even though its body is now
trivial. It is the seam a fine-tuned extractor drops into later, which is what
this module's original docstring always said it was for -- the heuristics were
labelled a bootstrap from the beginning.

Assistant replies get compressed rather than truncated, because they are long
and put their conclusions last. See `strip_assistant_response`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from arteries.config import AGENT_PROCESS_ID, EPHEMERAL_MODE, PARENT_AGENT_ID, PROJECT_ID
from arteries import storage

# Capillaries owns the domain taxonomy; prefer it so the two ends of the memory
# channel can't drift. But extraction is a pure-memory op — it must not hard-fail
# just because capillaries isn't installed. Fall back to arteries' own taxonomy
# (docs.DOMAIN_KEYWORDS) when capillaries is absent; drift only in that
# degraded mode, never when capillaries is present.
try:
    from capillaries.agent.inference import DOMAIN_KEYWORDS
except Exception:  # capillaries not installed / not importable
    from arteries.docs import DOMAIN_KEYWORDS

MIN_EXTRACTABLE_WORDS = 5


@dataclass
class Extraction:
    fact: str
    domains: list[str]


def extract_from_message(message: str) -> list[Extraction]:
    """One record per turn, verbatim.

    The only gate is length: "ok thanks" is not a memory. Everything else is
    the compiler's call, and it has the whole message plus its neighbours to
    make it with.
    """
    if len(message.split()) < MIN_EXTRACTABLE_WORDS:
        return []
    return [Extraction(fact=message, domains=_infer_domains(message.lower()))]


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
                "status": "ephemeral-only",
            })
        return len(extractions)
    for ext in extractions:
        storage.insert_ephemeral(
            project_id=PROJECT_ID,
            agent_process_id=AGENT_PROCESS_ID,
            fact=ext.fact,
            domains=ext.domains,
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


# -- Assistant response → single ephemeral record for LLM compilation ---------

_NARRATION = re.compile(
    r"^(let me|i'll|i will|now i|here's|here is|looking at|checking|reading|updating|creating)\b",
    re.IGNORECASE,
)
_CODE_FENCE = re.compile(r"^```")


# An assistant reply is long, and its conclusions are at the end. The old cap
# kept the first 1500 characters and threw the rest away, which lost exactly the
# part worth keeping. Compression is now by selection: score lines, keep the
# best, and always keep both ends.
HEAD_CHARS = 900
TAIL_CHARS = 600
_ELISION = "\n[...]\n"

# A fact in this domain names something -- an identifier, a path, a number.
# Prose that names nothing is nearly always narration.
_NAMED = re.compile(r"`[^`]+`|\b\d+\b|\b[a-z_]+\.[a-z_]+\b|\b[a-z]+_[a-z_]+\b")


def _informative(line: str) -> int:
    """Rough count of named things in a line. Higher is likelier to be a fact."""
    return len(_NAMED.findall(line))


def _overlap(line: str, reference: str) -> float:
    """Token overlap with the user's turn, for spotting restatements."""
    if not reference:
        return 0.0
    a = set(re.findall(r"[a-z]{3,}", line.lower()))
    b = set(re.findall(r"[a-z]{3,}", reference.lower()))
    return len(a & b) / len(a) if a else 0.0


def strip_assistant_response(text: str, user_turn: str = "") -> str:
    """Reduce an assistant reply to the lines likely to carry facts.

    Drops code fences, narration openers, and lines that mostly restate the
    question. What survives is ranked by how many concrete things it names, and
    the head and tail are always kept -- the opening states the finding and the
    closing states the conclusion.
    """
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
        # ponytail: 0.6 is a guess, tuned against nothing yet. It only drops a
        # line that is mostly the user's own words back at them.
        if _overlap(stripped, user_turn) > 0.6:
            continue
        out.append(stripped)

    body = "\n".join(out)
    if len(body) <= HEAD_CHARS + TAIL_CHARS:
        return body

    # Too long: keep both ends verbatim, and fill nothing in between unless the
    # middle actually names things.
    head, tail = body[:HEAD_CHARS], body[-TAIL_CHARS:]
    middle = [ln for ln in body[HEAD_CHARS:-TAIL_CHARS].splitlines() if _informative(ln) >= 2]
    kept = "\n".join(middle)[:600]
    return head + _ELISION + (kept + _ELISION if kept else "") + tail


def store_assistant_response(text: str, user_turn: str = "") -> int:
    """Strip and store an assistant response as a single ephemeral record.

    No pattern matching — the compilation LLM decides what's worth keeping.
    Returns 1 if stored, 0 if stripped to nothing.
    """
    stripped = strip_assistant_response(text, user_turn)
    if len(stripped.split()) < MIN_EXTRACTABLE_WORDS:
        return 0
    domains = _infer_domains(stripped.lower())
    if EPHEMERAL_MODE == "discard":
        _ephemeral_buffer.append({
            "fact": stripped,
            "domains": domains,
            "status": "ephemeral-only",
            "source": "assistant",
        })
        return 1
    storage.insert_ephemeral(
        project_id=PROJECT_ID,
        agent_process_id=AGENT_PROCESS_ID,
        fact=stripped,
        domains=domains,
        parent_agent_id=PARENT_AGENT_ID,
        source="assistant",
    )
    return 1
