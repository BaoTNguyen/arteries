"""Single-process prompt hook entry point for CLI event payloads."""

from __future__ import annotations

import argparse
import asyncio
import os

from arteries.cli_normalize import _message, _transcript, apply_event_env, normalize
from arteries.eval import evaluate, frame_retrieved
from arteries.eventjson import read_stdin_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observe one user prompt hook event.")
    parser.add_argument("prompt", nargs="*", help="prompt text override")
    parser.add_argument("--cli", default=os.getenv("ARTERIES_CLI", "generic"))
    parser.add_argument("--event", default="UserPromptSubmit")
    parser.add_argument("--project", default=os.getenv("ARTERIES_PROJECT", "default"))
    parser.add_argument("--agent", default=os.getenv("ARTERIES_AGENT_ID"))
    args = parser.parse_args(argv)

    event = read_stdin_json()
    normalized = normalize(
        event,
        cli=args.cli,
        fallback_event=args.event,
        project_id=args.project,
        agent_id=args.agent,
    )
    apply_event_env(normalized)

    transcript = _transcript(event)
    if transcript:
        os.environ["ARTERIES_TRANSCRIPT"] = transcript

    prompt = " ".join(args.prompt).strip() if args.prompt else _message(event)
    if not prompt:
        return 0

    result = asyncio.run(evaluate(prompt))
    if result:
        print(frame_retrieved(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
