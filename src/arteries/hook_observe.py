"""Single-process prompt hook entry point for CLI event payloads."""

from __future__ import annotations

import argparse
import asyncio
import os

from arteries.cli_normalize import (
    _message, _transcript, add_event_args, normalize_from_args,
)
from arteries.eval import evaluate, frame_retrieved
from arteries.eventjson import read_stdin_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observe one user prompt hook event.")
    parser.add_argument("prompt", nargs="*", help="prompt text override")
    add_event_args(parser, event_default="UserPromptSubmit")
    args = parser.parse_args(argv)

    event = read_stdin_json()
    normalize_from_args(event, args)

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
