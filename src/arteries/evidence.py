"""How strongly a claim is known.

Finding 22: the ladder had one rung. Every stored claim was `stated` -- someone
said it in a session. Nothing was `observed`: the test passed, the file existed,
the command exited 0. With one rung, two contradicting claims can only be
ordered by recency, so a confident wrong claim beats a quiet correct one every
time.

    user      the user said it themselves
    observed  something actually happened and was recorded
    stated    said in a session, by anyone
    inferred  the compiler concluded it

Distinct from `origin`, which is a different question: origin is which door a row
came through, evidence is how strongly it is known. A PostToolUse observation
comes through the ordinary conversation door carrying `observed`.

The ladder does real work in exactly one place -- deciding which of two
contradicting claims wins -- and one rule: a claim may only be superseded by one
of equal or higher class. That is what stops an inferred preference from
overwriting a stated one, and it is why the eviction exemption for preferences
is worth anything.
"""

from __future__ import annotations

# Ordered strongest first. The index in this tuple *is* the rank; a separate
# dict of scores would be a second thing to keep in step with it.
LADDER = ("user", "observed", "stated", "inferred")

DEFAULT = "stated"


def rank(evidence: str | None) -> int:
    """Lower is stronger. An unknown class sorts last rather than raising --
    memory must not fail a turn over a label it does not recognise."""
    try:
        return LADDER.index((evidence or DEFAULT).strip().lower())
    except ValueError:
        return len(LADDER)


def can_supersede(new: str | None, old: str | None) -> bool:
    """Whether a claim may replace one it contradicts.

    Equal or higher class only. An inferred "prefers spaces" loses to a stated
    "I prefer tabs" and is recorded as a `contradicts` edge instead -- visible,
    and not silently applied.
    """
    return rank(new) <= rank(old)


def for_source(source: str | None) -> str:
    """The class a row gets from where it came from.

    `source` is set at intake: 'user' for a typed turn, 'assistant' for a
    stripped reply, 'tool' for an observation, and the service names for
    heart/plexus/marrow. Those services report what happened rather than what
    was said, which is the same kind of evidence a tool result is.
    """
    mapping = {
        "user": "user",
        "tool": "observed",
        "heart": "observed",
        "plexus": "observed",
        "marrow": "observed",
        "assistant": "stated",
    }
    return mapping.get((source or "").strip().lower(), DEFAULT)
