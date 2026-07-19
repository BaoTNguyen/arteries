"""Single-process prompt hook entry point for CLI event payloads."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from arteries.cli_normalize import _message, _transcript, normalize
from arteries.eval import evaluate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observe one user prompt hook event.")
    parser.add_argument("prompt", nargs="*", help="prompt text override")
    parser.add_argument("--cli", default=os.getenv("ARTERIES_CLI", "generic"))
    parser.add_argument("--event", default="UserPromptSubmit")
    parser.add_argument("--project", default=os.getenv("ARTERIES_PROJECT", "default"))
    parser.add_argument("--agent", default=os.getenv("ARTERIES_AGENT_ID"))
    args = parser.parse_args(argv)

    event = _read_stdin_json()
    normalized = normalize(
        event,
        cli=args.cli,
        fallback_event=args.event,
        project_id=args.project,
        agent_id=args.agent,
    )
    _apply_event_env(normalized)

    transcript = _transcript(event)
    if transcript:
        os.environ["ARTERIES_TRANSCRIPT"] = transcript

    prompt = " ".join(args.prompt).strip() if args.prompt else _message(event)
    if not prompt:
        return 0

    result = asyncio.run(evaluate(prompt))
    if result:
        print(result)
    return 0


def _apply_event_env(event: Any) -> None:
    os.environ["ARTERIES_CLI"] = event.cli
    os.environ["ARTERIES_EVENT"] = event.event
    os.environ["ARTERIES_AGENT_ID"] = event.agent_id
    os.environ["ARTERIES_AGENT_ROLE"] = event.agent_role
    if event.parent_agent_id:
        os.environ["ARTERIES_PARENT_AGENT_ID"] = event.parent_agent_id
    if event.session_id:
        os.environ["ARTERIES_SESSION_ID"] = event.session_id
    if event.cwd:
        os.environ["ARTERIES_EVENT_CWD"] = event.cwd


def _read_stdin_json() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


if __name__ == "__main__":
    raise SystemExit(main())
