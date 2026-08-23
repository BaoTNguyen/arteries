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
import sys

from arteries import actionlog, runlog
from arteries.config import (
    AGENT_PROCESS_ID,
    EPHEMERAL_MODE,
    PERSISTENT_READ,
    PROJECT_ID,
    RETRIEVAL_MIN_CONFIDENCE,
)
from arteries.assistant import capture_response, read_last_assistant
from arteries import scope, storage
from arteries.embed import embed_text_sync
from arteries.extract import extract_and_store

logger = logging.getLogger(__name__)
from arteries.frame import get_current_frame

# capillaries is a hard dependency of arteries (the frame contract types
# already come from it), so no availability guard.
from capillaries.agent.gate import gate as run_gate
from capillaries.find import find as cap_find


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
    runlog.log_event(
        "turn.observed",
        "arteries",
        _message_payload(message),
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
        except Exception:
            # Telemetry must never fail a turn, but a swallowed NameError here
            # is how this block shipped broken and green the first time.
            logger.debug("coverage measurement failed", exc_info=True)

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
        chosen_action="discard_ephemeral" if EPHEMERAL_MODE == "discard" else "write_ephemeral",
        available_actions=["write_ephemeral", "discard_ephemeral"],
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

    if EPHEMERAL_MODE != "discard":
        # ponytail: detach compile into its own process instead of awaiting it
        # inline. The hook is a one-shot process, so an in-process asyncio task
        # only survives if awaited — which used to block the prompt up to 5s on
        # the compile LLM call and tripped the UserPromptSubmit timeout. A
        # detached process outlives the hook and does the same work off the hot
        # path; the "+N remembered" notice just surfaces on the next turn.
        _spawn_detached_compile()

    prompt_text = None
    # heart sets ARTERIES_RETRIEVAL=off for retrieval-ablation episodes
    retrieval_off = os.environ.get("ARTERIES_RETRIEVAL", "").lower() == "off"
    if retrieval_off:
        runlog.log_event(
            "prompt.gate.decided",
            "arteries",
            {"search": False, "reason": "retrieval_off_env"},
            turn_id=turn_id,
        )
        actionlog.log_decision(
            "retrieval.gate",
            chosen_action="abstain",
            available_actions=["abstain", "search"],
            observation={"reason": "retrieval_off_env"},
            turn_id=turn_id,
        )
    if not retrieval_off:
        try:
            with _dependency_stdout_to_stderr():
                decision = await run_gate(message=message, memory=frame)
        except Exception as exc:
            runlog.log_failure("prompt.gate.failed", "capillaries", exc, turn_id=turn_id)
            decision = None

        if decision is not None:
            runlog.log_event(
                "prompt.gate.decided",
                "capillaries",
                {
                    "search": getattr(decision, "search", None),
                    "reason": getattr(decision, "reason", None),
                },
                turn_id=turn_id,
            )
            actionlog.log_decision(
                "retrieval.gate",
                chosen_action="search" if decision.search else "abstain",
                available_actions=["abstain", "search"],
                observation={"reason": getattr(decision, "reason", None)},
                cost={"confidence": getattr(decision, "confidence", None)},
                turn_id=turn_id,
            )

            if decision.search:
                try:
                    # capillaries' serving_log reads ARTERIES_TURN_ID to tie a
                    # served result back to this turn (serving.py); no other
                    # convention reaches that deep. Serial per turn, so process
                    # env is safe. Scoped to the call so it never leaks.
                    os.environ["ARTERIES_TURN_ID"] = turn_id
                    try:
                        with _dependency_stdout_to_stderr():
                            result = await cap_find(message, memory=frame)
                    finally:
                        os.environ.pop("ARTERIES_TURN_ID", None)
                except Exception as exc:
                    runlog.log_failure("prompt.retrieve.failed", "capillaries", exc, turn_id=turn_id)
                else:
                    accepted = (result.mode != "none"
                                and result.confidence >= RETRIEVAL_MIN_CONFIDENCE)
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
                        prompt_text = result.prompt_text

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
