"""
Per-turn evaluation entry point, called by the UserPromptSubmit hook.

Three jobs every turn:
1. Extract ephemeral memories from the message (sync, always runs)
2. Gate check -> retrieval if warranted (sync, conditional)
3. Kick off async compilation: ephemeral -> persistent (background, non-blocking)

Prints retrieved prompt text to stdout when the gate fires.
Prints nothing otherwise. Extraction and compilation are silent.

Usage:
    art eval "user message text"
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import re
import sys

from arteries import actionlog, degrade, memory_select, runlog, scope, storage
from arteries.assistant import capture_response, read_last_assistant
from arteries.config import (
    AGENT_PROCESS_ID,
    EPHEMERAL_MODE,
    PERSISTENT_READ,
    PROJECT_ID,
)
from arteries.conversation import recent_assistant_turns
from arteries.embed import embed_text_sync
from arteries.extract import extract_and_store

logger = logging.getLogger(__name__)
from arteries.frame import get_current_frame
from arteries.usage import turn_usage

# Capillaries is a hard dependency of arteries (the frame contract types
# already come from it), so no availability guard.
from capillaries.find import find as cap_find


_ACKNOWLEDGEMENTS = frozenset({
    "yes", "no", "yeah", "yep", "nope", "nah", "ok", "okay", "sure",
    "thanks", "thank you", "thx", "got it", "makes sense", "sounds good",
    "looks good", "perfect", "great", "nice", "cool", "awesome", "do it",
    "go ahead", "proceed", "continue", "agreed", "correct", "right",
    "exactly", "nevermind", "never mind", "nvm", "cancel",
})
_DIRECTIVE = re.compile(
    r"^\s*(?:set\s+up|add|change|fix|implement|update|create|build|run|use|"
    r"make|move|remove|rename|write|test|deploy|continue|revise|refine|edit)\b",
    re.IGNORECASE,
)
_CONTINUATION = re.compile(
    r"\b(?:again|previous|prior|above|earlier|same|that|those|this|these|it|"
    r"revise|refine|edit|continue)\b",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"\[([A-Z][A-Z0-9 _/-]{1,80})\]|\{\{\s*([^{}]{1,80})\s*\}\}")


def _triage_skip_reason(
    message: str,
    prior_assistant_turns: list[str],
) -> str | None:
    """Return a categorical reason not to retrieve, or None to retrieve.

    This deliberately replaces Capillaries' similarity, length, and
    specification-density pre-gate on the automatic-hook path.  A prompt
    library helps when the conversation does not already make a request
    obvious. It skips acknowledgements and explicit continuations of a prior
    assistant result. Subject-word overlap is never completion evidence.
    """
    normalized = message.strip().lower().rstrip("!?.,")
    if normalized in _ACKNOWLEDGEMENTS:
        return "acknowledgement"
    if not _DIRECTIVE.match(message) or "?" in message:
        return None

    if not _CONTINUATION.search(normalized):
        return None
    if not prior_assistant_turns:
        return None
    return "explicit continuation of prior assistant result"


def _assistant_ephemeral_text(rows: list[dict]) -> list[str]:
    """Keep assistant-produced ephemeral results as triage evidence.

    User facts remain in the frame for retrieval and prompt filling, but do not
    establish that a requested deliverable has already been produced.
    """
    return [
        str(row["fact"])
        for row in rows
        if row.get("source") == "assistant" and row.get("fact")
    ]


def _prepare_injection(prompt_text: str) -> tuple[str, list[str]]:
    """Make placeholder-bearing workflows safe to use with live context."""
    placeholders = list(dict.fromkeys(
        (first or second).strip() for first, second in _PLACEHOLDER.findall(prompt_text)
    ))
    if not placeholders:
        return prompt_text, []
    guidance = (
        "This retrieved workflow contains placeholders. Resolve them only from "
        "the current conversation and known project context; do not invent "
        "values. Ask focused follow-up questions for any required value that "
        "is still missing.\n\n"
    )
    return guidance + prompt_text, placeholders


async def evaluate(message: str) -> str | None:
    # Opt-in: a repo nobody registered is not observed at all -- no ephemeral,
    # no telemetry, no run log. Ahead of every write, and it logs once so the
    # skip is visible in `art doctor` rather than looking like a dead hook.
    if not scope.is_tracked():
        runlog.log_event(
            "turn.skipped_untracked", "arteries",
            {"cwd": os.environ.get("ARTERIES_EVENT_CWD") or os.getcwd(),
             "hint": "art scope add <group> <repo path>"},
        )
        return None

    turn_id = runlog.new_turn_id()
    # what this turn actually cost, from the CLI's own transcript. Stamped onto
    # turn.observed because that is the only event a subscription turn produces
    # — without it the whole stack prices interactive work at zero, which is how
    # $40 sessions read as $0.00 on the control plane.
    # `cli` and `repo` — which plexus needs to pick a rate card and attribute
    # spend to a project — are already stamped by runlog.log_event, so only the
    # usage counts belong here.
    try:
        spend = turn_usage()
    except Exception:  # telemetry must never be able to fail a turn
        spend = {}
    runlog.log_event(
        "turn.observed",
        "arteries",
        {**_message_payload(message), **spend},
        turn_id=turn_id,
    )

    _capture_last_response(turn_id)

    # One embedding per turn, computed here and used three times: to stamp the
    # ephemeral records this message produces, to query persistent
    # by relevance, and to measure how well the session already covers this
    # turn. Embedding per record instead would put N calls on the hook path.
    msg_vec = embed_text_sync(message, is_query=True)

    # How well this session already covers the turn. Recorded, not acted on:
    # skipping retrieval on a bad reading is invisible -- the agent just works
    # without a prompt it should have had -- so this builds the distribution a
    # threshold can later be chosen from.
    if msg_vec:
        try:
            coverage = storage.max_ephemeral_similarity(
                PROJECT_ID, AGENT_PROCESS_ID, msg_vec)
            runlog.log_event("memory.coverage.measured", "arteries",
                             {"coverage": round(coverage, 3)}, turn_id=turn_id)
        except Exception as exc:
            # This block shipped broken and green the first time -- undefined
            # names inside a bare handler. degrade.note is why it would not now.
            degrade.note(exc, "coverage measurement", turn_id=turn_id)

    # Snapshot earlier assistant output and assistant-produced ephemeral memory
    # before this turn is extracted. User facts remain available to Capillaries
    # in the frame, but cannot themselves prove a deliverable is complete.
    prior_assistant_turns = recent_assistant_turns()
    try:
        prior_ephemerals = memory_select.select_ephemeral()
    except Exception as exc:
        degrade.note(exc, "prior ephemeral lookup")
        prior_ephemerals = []
    prior_assistant_turns += _assistant_ephemeral_text(prior_ephemerals)

    try:
        extracted = extract_and_store(message, embedding=msg_vec)
    except Exception as exc:
        runlog.log_failure("memory.extract.failed", "arteries", exc, turn_id=turn_id)
        runlog.log_failure("turn.failed", "arteries", exc, turn_id=turn_id)
        return None

    runlog.log_event(
        "memory.ephemeral.extracted",
        "arteries",
        {"count": extracted},
        turn_id=turn_id,
    )
    actionlog.log_decision(
        "memory.write_policy",
        chosen_action={
            "discard": "discard_ephemeral",
            "keep": "write_ephemeral_no_compile",
        }.get(EPHEMERAL_MODE, "write_ephemeral"),
        available_actions=["write_ephemeral", "write_ephemeral_no_compile", "discard_ephemeral"],
        observation={"extracted": extracted},
        turn_id=turn_id,
    )

    try:
        frame = get_current_frame(message, embedding=msg_vec)
    except Exception as exc:
        runlog.log_failure("memory.frame.failed", "arteries", exc, turn_id=turn_id)
        runlog.log_failure("turn.failed", "arteries", exc, turn_id=turn_id)
        return None

    runlog.log_event(
        "memory.frame.built",
        "arteries",
        {
            "recent_messages": len(frame.ephemeral.recent_messages),
            "session_insights": len(frame.persistent.session_insights),
            "sibling_insights": len(frame.scope.sibling_insights),
        },
        turn_id=turn_id,
    )
    actionlog.log_decision(
        "memory.read_policy",
        chosen_action="read_none" if PERSISTENT_READ == "none" else "read_persistent_relevant",
        available_actions=[
            "read_none", "read_persistent_relevant", "read_recent_ephemeral", "read_scope",
        ],
        observation={
            "session_insights": len(frame.persistent.session_insights),
            "recent_messages": len(frame.ephemeral.recent_messages),
        },
        turn_id=turn_id,
    )

    # only "compile" promotes automatically. "keep" still writes ephemeral, so the
    # turn stays in the frame and in `art search` — it just never graduates to
    # permanent project memory without someone asking.
    if EPHEMERAL_MODE == "compile":
        # ponytail: detach compile into its own process instead of awaiting it
        # inline. The hook is a one-shot process, so an in-process asyncio task
        # only survives if awaited — which used to block the prompt up to 5s on
        # the compile LLM call and tripped the UserPromptSubmit timeout. A
        # detached process outlives the hook and does the same work off the hot
        # path; the "+N remembered" notice just surfaces on the next turn.
        _spawn_detached_compile()

    prompt_text = None
    # heart sets ARTERIES_RETRIEVAL=off for retrieval-ablation episodes.
    # Otherwise triage is categorical: it skips acknowledgements and clear
    # continuations, then retrieves. It does not use similarity, word-count,
    # or specification-density thresholds.
    retrieval_off = os.environ.get("ARTERIES_RETRIEVAL", "").lower() == "off"
    skip_reason = "retrieval_off_env" if retrieval_off else _triage_skip_reason(
        message, prior_assistant_turns
    )
    if skip_reason:
        runlog.log_event(
            "prompt.gate.decided",
            "arteries",
            {"search": False, "reason": skip_reason},
            turn_id=turn_id,
        )
        actionlog.log_decision(
            "retrieval.gate",
            chosen_action="abstain",
            available_actions=["abstain", "search"],
            observation={"reason": skip_reason},
            turn_id=turn_id,
        )
    else:
        runlog.log_event(
            "prompt.gate.decided",
            "arteries",
            {"search": True, "reason": "triage: no clear in-context instruction"},
            turn_id=turn_id,
        )
        actionlog.log_decision(
            "retrieval.gate",
            chosen_action="search",
            available_actions=["abstain", "search"],
            observation={"reason": "triage: no clear in-context instruction"},
            turn_id=turn_id,
        )
        try:
            # cap_find owns retrieval and reranking; the hook only decides
            # whether this turn warrants consulting that library at all.
            os.environ["ARTERIES_TURN_ID"] = turn_id
            try:
                with _dependency_stdout_to_stderr():
                    result = await cap_find(message, context=frame)
            finally:
                os.environ.pop("ARTERIES_TURN_ID", None)
        except Exception as exc:
            runlog.log_failure("prompt.retrieve.failed", "capillaries", exc, turn_id=turn_id)
        else:
            # Capillaries owns the accept/abstain decision after retrieval and
            # chunk-aware reranking. Arteries only injects accepted results.
            accepted = result.mode != "none"
            actionlog.log_decision(
                "retrieval.select",
                chosen_action="accept_retrieval" if accepted else "reject_retrieval",
                available_actions=["accept_retrieval", "reject_retrieval"],
                observation={"mode": result.mode, "confidence": result.confidence},
                metadata={"prompt_id": str(result.prompt_id) if result.prompt_id else None},
                turn_id=turn_id,
            )

            if accepted:
                runlog.log_event(
                    "prompt.retrieved",
                    "capillaries",
                    {
                        "prompt_id": result.prompt_id,
                        "mode": result.mode,
                        "confidence": result.confidence,
                    },
                    turn_id=turn_id,
                )
                storage.log_retrieval(
                    project_id=PROJECT_ID,
                    agent_process_id=AGENT_PROCESS_ID,
                    prompt_id=result.prompt_id,
                    situation=message,
                    score=result.confidence,
                )
                # Placeholders left unfilled make a prompt only partly usable.
                # Recorded so the gap is visible rather than silently injected.
                prompt_text, placeholders = _prepare_injection(result.prompt_text)
                runlog.log_event(
                    "prompt.readiness.assessed",
                    "arteries",
                    {"placeholders": placeholders,
                     "status": "partial" if placeholders else "ready"},
                    turn_id=turn_id,
                )

    return prompt_text


def _message_payload(message: str) -> dict:
    preview = message[:2000]
    return {
        "message_chars": len(message),
        "message_preview": preview,
        "message_preview_truncated": len(message) > len(preview),
        "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
    }


def _spawn_detached_compile() -> None:
    """Fire-and-forget the ephemeral->persistent compile in its own process.

    start_new_session detaches it from the hook's process group so it keeps
    running after the hook returns. It inherits cwd + ARTERIES_* env, which is
    all compile_once needs.
    """
    import subprocess
    try:
        subprocess.Popen(
            [sys.executable, "-m", "arteries.compile"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        runlog.log_failure("memory.compile.failed", "arteries", exc)


@contextlib.contextmanager
def _dependency_stdout_to_stderr():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield
    logs = buffer.getvalue()
    if logs:
        print(logs, end="", file=sys.stderr)


def _capture_last_response(turn_id: str) -> None:
    """Read the last assistant message from the transcript and store as ephemeral."""
    transcript = os.environ.get("ARTERIES_TRANSCRIPT")
    if not transcript:
        return
    try:
        text = read_last_assistant(transcript)
        if text and len(text) >= 40:
            capture_response(text, turn_id=turn_id, prior_turn=True)
    except Exception:
        pass


def main() -> None:
    if len(sys.argv) < 2:
        return

    message = sys.argv[1]
    result = asyncio.run(evaluate(message))

    if result:
        print(result)


if __name__ == "__main__":
    main()
