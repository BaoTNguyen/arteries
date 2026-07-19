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
import os
import sys

from arteries import actionlog, runlog
from arteries.config import EPHEMERAL_MODE, PERSISTENT_READ
from arteries.assistant import read_last_assistant
from arteries.extract import extract_and_store, store_assistant_response
from arteries.frame import get_current_frame

# capillaries is a hard dependency of arteries (the frame contract types
# already come from it), so no availability guard.
from capillaries.agent.gate import gate as run_gate
from capillaries.find import find as cap_find


async def evaluate(message: str) -> str | None:
    turn_id = runlog.new_turn_id()
    runlog.log_event(
        "turn.observed",
        "arteries",
        _message_payload(message),
        turn_id=turn_id,
    )

    _capture_last_response(turn_id)

    try:
        extracted = extract_and_store(message)
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
        frame = get_current_frame(message)
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
            "ground_truth_insights": len(frame.evergreen.ground_truth_insights),
        },
        turn_id=turn_id,
    )
    actionlog.log_decision(
        "memory.read_policy",
        chosen_action="read_none" if PERSISTENT_READ == "none" else "read_persistent_relevant",
        available_actions=[
            "read_none", "read_persistent_relevant", "read_recent_ephemeral", "read_evergreen",
        ],
        observation={
            "session_insights": len(frame.persistent.session_insights),
            "recent_messages": len(frame.ephemeral.recent_messages),
        },
        turn_id=turn_id,
    )

    compile_task = None
    if EPHEMERAL_MODE != "discard":
        compile_task = asyncio.create_task(_compile_background())

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
                    with _dependency_stdout_to_stderr():
                        result = await cap_find(message, memory=frame)
                except Exception as exc:
                    runlog.log_failure("prompt.retrieve.failed", "capillaries", exc, turn_id=turn_id)
                else:
                    accepted = result.mode != "none" and result.confidence >= 0.3
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
                        from arteries import storage
                        from arteries.config import PROJECT_ID, AGENT_PROCESS_ID
                        storage.log_retrieval(
                            project_id=PROJECT_ID,
                            agent_process_id=AGENT_PROCESS_ID,
                            prompt_id=result.prompt_id,
                            situation=message,
                            score=result.confidence,
                        )
                        prompt_text = result.prompt_text

    if compile_task:
        try:
            await asyncio.wait_for(asyncio.shield(compile_task), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            pass

    return prompt_text


def _message_payload(message: str) -> dict:
    preview = message[:500]
    return {
        "message_chars": len(message),
        "message_preview": preview,
        "message_preview_truncated": len(message) > len(preview),
        "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
    }


async def _compile_background() -> None:
    try:
        from arteries.compile import compile_once
        await compile_once()
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
        if not text or len(text) < 40:
            return
        stored = store_assistant_response(text)
        preview = text[:500]
        runlog.log_event(
            "assistant.response",
            "arteries",
            {
                "assistant_preview": preview,
                "assistant_preview_truncated": len(text) > len(preview),
                "assistant_chars": len(text),
            },
            turn_id=turn_id,
        )
        runlog.log_event(
            "memory.assistant.stored",
            "arteries",
            {"stored": stored, "input_chars": len(text)},
            turn_id=turn_id,
        )
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
