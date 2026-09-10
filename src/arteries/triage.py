"""Whether a message names something to look for.

Some turns carry no query. "yes", "go ahead", "clean up" -- the object of the
sentence is the turn before it, not anything in the store. Embedding one of
those produces a centroid of nothing, and a centroid of nothing still comes back
with rows, because a nearest-neighbour search always has a nearest neighbour.

This was already gating the prompt-corpus search on the hook path
(`eval._triage_skip_reason`). Memory retrieval ran unconditionally, so on those
turns arteries embedded a non-query and ranked the results of searching for it.

Lifted here because two callers need it and neither should import the other.
The rules are unchanged; only their home moved.

What a skip means is narrow, and worth being precise about: it invalidates
*similarity* search, because there is nothing to be similar to. It says nothing
about recency. Ephemeral is still exactly the right context for "continue" -- it
is what was just being talked about -- so the skip applies to the query-driven
arms only.
"""

from __future__ import annotations

import re

ACKNOWLEDGEMENTS = frozenset({
    "yes", "no", "yeah", "yep", "nope", "nah", "ok", "okay", "sure",
    "thanks", "thank you", "thx", "got it", "makes sense", "sounds good",
    "looks good", "perfect", "great", "nice", "cool", "awesome", "do it",
    "go ahead", "proceed", "continue", "agreed", "correct", "right",
    "exactly", "nevermind", "never mind", "nvm", "cancel",
})

DIRECTIVE = re.compile(
    r"^\s*(?:set\s+up|add|change|fix|implement|update|create|build|run|use|"
    r"make|move|remove|rename|write|test|deploy|continue|revise|refine|edit)\b",
    re.IGNORECASE,
)

CONTINUATION = re.compile(
    r"\b(?:again|previous|prior|above|earlier|same|that|those|this|these|it|"
    r"revise|refine|edit|continue)\b",
    re.IGNORECASE,
)

# A message that is only a verb and its particles names no object, so its object
# is the turn before it.
BARE_IMPERATIVE = re.compile(
    r"^[a-z]+(?:\s+(?:up|down|out|off|over|again|now|please|it|this|that|them|all))*$",
    re.IGNORECASE,
)


def skip_reason(message: str, prior_assistant_turns: list[str]) -> str | None:
    """A categorical reason this message has nothing to search for, or None.

    Deliberately categorical rather than a similarity, length, or
    specification-density threshold: those answer "is this query good", which is
    a different and much harder question than "is this a query at all".
    """
    normalized = message.strip().lower().rstrip("!?.,")
    if normalized in ACKNOWLEDGEMENTS:
        return "acknowledgement"
    # Checked ahead of the directive test, which only inspects the first word and
    # so waves "Clean up" through to a search -- that one retrieved a
    # spreadsheet-cleaning workflow at 0.974.
    if prior_assistant_turns and BARE_IMPERATIVE.match(normalized):
        return "bare imperative continuing prior assistant result"
    if not DIRECTIVE.match(message) or "?" in message:
        return None
    if not CONTINUATION.search(normalized):
        return None
    if not prior_assistant_turns:
        return None
    return "explicit continuation of prior assistant result"
