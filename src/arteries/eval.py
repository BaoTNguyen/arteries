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
import io
import sys

from arteries import runlog
from arteries.extract import extract_and_store
from arteries.frame import get_current_frame
from capillaries.agent.gate import gate as run_gate


async def evaluate(message: str) -> str | None:
    turn_id = runlog.new_turn_id()
    runlog.log_event(
        "turn.observed",
        "arteries",
        {"message_chars": len(message)},
        turn_id=turn_id,
    )

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

    compile_task = asyncio.create_task(_compile_background())

    prompt_text = None
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

        if decision.search:
            try:
                from capillaries.find import find
                with _dependency_stdout_to_stderr():
                    result = await find(message, memory=frame)
            except Exception as exc:
                runlog.log_failure("prompt.retrieve.failed", "capillaries", exc, turn_id=turn_id)
            else:
                if result.mode != "none" and result.confidence >= 0.3:
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

    try:
        await asyncio.wait_for(asyncio.shield(compile_task), timeout=5.0)
    except (asyncio.TimeoutError, Exception):
        pass

    return prompt_text


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


def main() -> None:
    if len(sys.argv) < 2:
        return

    message = sys.argv[1]
    result = asyncio.run(evaluate(message))

    if result:
        print(result)


if __name__ == "__main__":
    main()
