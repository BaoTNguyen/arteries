"""What is worth keeping permanently.

Between the model's JSON and a permanent row there were exactly two checks, and
neither asked about worth. `validate_response` is structural -- non-empty fact,
`kind` in the enum, `duplicate_of` parses. `_reject_duplicates` asks "do we have
this already?", which is a redundancy question. Novel noise passed at any store
size, and on a cold start `_reject_duplicates` has nothing to compare against, so
everything the model named was written.

The result, measured on the live store: **83 of 527 live rows (16%) are transient
session intent** -- "User intends to write a Dockerfile", "User is considering
replacing Ollama with OpenRouter". True when written, useless a week later, and
occupying the same retrieval budget as a fact about the codebase.

The rules here are deliberately blunt and few. A permanent store's error is
one-sided: a rejected fact that mattered comes back, because the next session
will say it again and the turn it came from is still in `agent_events`. A
retained fact that did not matter stays forever and competes for packet slots.
So each rule below is one someone could have written by hand after reading the
83 rows, and nothing tries to be clever about the rest -- read-side decay
(finding 7) is what bounds the error this cannot catch.
"""

from __future__ import annotations

import re

# "User intends to fix Plexus path B routing first." True on the day, worthless
# afterwards, and there were 83 of them. The verb is what separates a plan from a
# standing fact: `intends to`, `is considering`, `wants to` describe the state of
# a conversation, not the state of the project.
_TRANSIENT_INTENT = re.compile(
    r"^\s*(?:the\s+)?user\s+"
    r"(?:intends?|plans?|aims?\s+to|seeks?|is\s+open\s+to|"
    r"is\s+(?:considering|planning|going|about|currently|now)|"
    r"(?:wants?|needs?|would\s+like)\s+to|asked|inquired|is\s+(?:verifying|testing|checking))\b",
    re.IGNORECASE,
)

# A claim whose subject is the conversation rather than the project. "In this
# session we decided..." is a fact about a session that will not exist tomorrow.
_ABOUT_THE_CONVERSATION = re.compile(
    r"\b(?:this|the\s+current|the\s+present)\s+"
    r"(?:session|conversation|turn|exchange|chat|thread|discussion)\b",
    re.IGNORECASE,
)

# No "is this concrete?" rule, after trying two and discarding both.
#
# A regex for paths, digits, underscores and capitals rejected "pgvector is used
# for retrieval in the Arteries system" -- durable, specific, and lowercase. It
# refused 150 of 527 live rows, most of them fine.
#
# Absence of extracted entities looked better and is worse. The 109 rows it
# rejects include "`scripts/teardown.sh` must drop only the arteries schema".
# They carry no entity edges because entity extraction was added after they were
# written, so the signal measures the schema's history rather than the claim.
#
# Both rules failed the same way: they guessed at worth from surface features.
# The two rules that remain identify a *specific* population that was measured --
# 16% of the store describing what a conversation was about to do -- and nothing
# here tries to catch the rest. Read-side decay (finding 7) bounds that.

# No exemption by `kind`, though the first draft had one for preference and
# constraint. `kind` is the model's own label and is exactly the field the audit
# found being misused -- a one-session instruction filed as a permanent
# `constraint`. Exempting on it hands the filter's decision back to the thing
# being filtered: "The user intends to add heart and plexus to the scope" was
# typed `constraint` and sailed through.
#
# The verb list does the work instead, and it never contained the preference
# verbs. "User prefers to install updates in a sandbox first" does not match,
# because `prefers` describes a standing property of a person while `intends`,
# `aims`, `plans` and `is considering` describe what a conversation was about to
# do next.

MIN_WORDS = 4


def worth_keeping(fact: str) -> str | None:
    """The rule that refuses this fact, or None to keep it.

    Returns the reason rather than a bool so rejections can be logged by rule and
    the filter can be argued with from data instead of from memory.
    """
    text = (fact or "").strip()
    if len(text.split()) < MIN_WORDS:
        return "too short to be a claim"

    if _ABOUT_THE_CONVERSATION.search(text):
        return "about the conversation, not the project"

    if _TRANSIENT_INTENT.match(text):
        return "transient session intent"

    return None
