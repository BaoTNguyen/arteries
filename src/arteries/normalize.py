"""One sentence, once.

Two jobs, both stdlib, both on the hook path where a model call is not an option.

`atoms` splits a turn into one-sentence claims. `normalize_fact` reduces one of
those to a hash key, so that two sessions saying the same thing in the same words
collide inside a unique index rather than becoming two rows.

**This does not replace verbatim capture, and the distinction matters.**
`extract.py:1-17` records that pattern extraction was removed *because* splitting
lost signal: of 202 stored rows, 12.9% matched the preference regex and 87.1%
were the whole message truncated to 500 characters. That measurement was about
throwing the turn away and keeping fragments. Here the turn is still stored --
`agent_events.payload.message_preview` has it, whole -- and the compiler still
reads it. Atoms are the unit dedupe and promotion operate on, not the unit that
gets remembered.

Splitting is conservative on purpose. A sentence wrongly split becomes two atoms
that each fail the length gate and vanish; a sentence wrongly joined is one atom
that dedupes slightly worse. The second failure is cheaper, so every ambiguous
case joins.
"""

from __future__ import annotations

import re

# Reused unchanged from extract.py rather than re-tuned. It is the gate that
# already decides "ok thanks" is not a memory, and an atom is judged by the same
# standard as a turn was.
MIN_ATOM_WORDS = 5

# Past this, keep it whole rather than cut mid-clause. A 600-character sentence
# is usually a list or a pasted block, and half of one is worse than all of it.
MAX_ATOM_CHARS = 600

# Sentence end: terminal punctuation, whitespace, then something that starts a
# new sentence. Requiring the capital is what keeps "v1.2 released" together.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[`])")

# Guards against splitting inside these. Checked as a suffix of the left side, so
# "e.g." and "Fig." do not end a sentence.
_ABBREVIATIONS = frozenset({
    "e.g.", "i.e.", "etc.", "vs.", "cf.", "approx.", "fig.", "no.",
    "mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "jr.", "sr.",
    "inc.", "ltd.", "co.", "al.", "ca.", "ver.", "rev.",
})

# Code is atomic. A fence or an inline span can contain any punctuation at all,
# and splitting one produces two fragments that are each syntactically nothing.
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]+`")

# A bullet is a claim whether or not it ends in a period, which is most of why
# splitting on punctuation alone under-splits lists.
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)

# Discourse markers carry no meaning into a stored fact and would otherwise make
# "So the sweep is broken" and "The sweep is broken" two different rows.
_LEADING_MARKER = re.compile(
    r"^(?:so|and|but|or|also|then|now|well|actually|basically|essentially|"
    r"anyway|however|therefore|thus|hence|okay|ok|right|yeah|yes|no)\b[,:]?\s+",
    re.IGNORECASE,
)

# Interjections that are a whole turn's worth of nothing. Lives here rather than
# in `triage` because both layers need it and this is the lower one: triage asks
# whether a *message* is one of these, splitting asks whether a trailing fragment
# is. Gluing "Right?" onto the sentence before it makes that sentence hash
# differently from the same sentence said without it, which is a dedupe miss
# caused entirely by punctuation.
ACKNOWLEDGEMENTS = frozenset({
    "yes", "no", "yeah", "yep", "nope", "nah", "ok", "okay", "sure",
    "thanks", "thank you", "thx", "got it", "makes sense", "sounds good",
    "looks good", "perfect", "great", "nice", "cool", "awesome", "do it",
    "go ahead", "proceed", "continue", "agreed", "correct", "right",
    "exactly", "nevermind", "never mind", "nvm", "cancel",
})

_WHITESPACE = re.compile(r"\s+")
_TERMINAL = re.compile(r"[.!?]+$")


def _protect(text: str) -> tuple[str, list[str]]:
    """Swap code spans for placeholders so punctuation inside them is inert."""
    held: list[str] = []

    def _hold(match: re.Match) -> str:
        held.append(match.group(0))
        return f"\x00{len(held) - 1}\x00"

    return _INLINE_CODE.sub(_hold, _FENCE.sub(_hold, text)), held


def _restore(text: str, held: list[str]) -> str:
    for index, original in enumerate(held):
        text = text.replace(f"\x00{index}\x00", original)
    return text


def _ends_with_abbreviation(left: str) -> bool:
    tail = left.rsplit(None, 1)[-1].lower() if left.split() else ""
    if tail in _ABBREVIATIONS:
        return True
    # A single capital plus a period is an initial, not an ending: "J. Smith".
    return bool(re.fullmatch(r"[A-Za-z]\.", tail))


def atoms(text: str) -> list[str]:
    """Split a turn into one-sentence claims. No model call."""
    if not text or not text.strip():
        return []

    protected, held = _protect(text)

    # Bullets first: each item is its own claim regardless of punctuation, and
    # splitting on the marker is more reliable than inferring it from periods.
    blocks = [b for b in _BULLET.split(protected) if b.strip()] \
        if _BULLET.search(protected) else [protected]

    out: list[str] = []
    for block in blocks:
        for line in block.split("\n\n"):
            out.extend(_split_sentences(line))

    restored = [_restore(a, held).strip() for a in out]
    return [a for a in restored if len(a.split()) >= MIN_ATOM_WORDS]


def _split_sentences(text: str) -> list[str]:
    if not text.strip():
        return []
    if len(text) > MAX_ATOM_CHARS and not _SENTENCE_END.search(text):
        return [text.strip()]

    pieces: list[str] = []
    buffer = ""
    for piece in _SENTENCE_END.split(text):
        candidate = f"{buffer} {piece}".strip() if buffer else piece
        # Join back when the break was a false positive, or when the left side is
        # too short to be a claim on its own -- an over-split fragment fails the
        # word gate and disappears, which loses more than under-splitting does.
        if _ends_with_abbreviation(buffer) or len(candidate.split()) < MIN_ATOM_WORDS:
            buffer = candidate
            continue
        pieces.append(candidate)
        buffer = ""
    if buffer.strip():
        if normalize_fact(buffer) in ACKNOWLEDGEMENTS:
            pass          # "Right?" is not part of the claim it follows
        elif pieces and len(buffer.split()) < MIN_ATOM_WORDS:
            pieces[-1] = f"{pieces[-1]} {buffer}".strip()
        else:
            pieces.append(buffer.strip())
    return [p.strip() for p in pieces if p.strip()]


def normalize_fact(text: str) -> str:
    """The hash key: what makes two phrasings of one claim the same row.

    Lowercased, whitespace collapsed, terminal punctuation and a leading
    discourse marker removed. Deliberately shallow -- no stemming, no stopword
    removal, no synonym handling. Those would make unrelated claims collide, and
    a collision here is a fact silently not stored. Paraphrase is the compiler's
    job, where both texts can be read.
    """
    collapsed = _WHITESPACE.sub(" ", (text or "").strip())
    collapsed = _LEADING_MARKER.sub("", collapsed)
    return _TERMINAL.sub("", collapsed).strip().lower()


def fact_hash(text: str) -> str:
    """Stable 32-char digest of the normalized claim."""
    import hashlib

    return hashlib.sha256(normalize_fact(text).encode()).hexdigest()[:32]
