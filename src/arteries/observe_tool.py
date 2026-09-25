"""Record one tool result as an observation.

The writing half of the PostToolUse hook. Deliberately thin: it takes an already
filtered event and writes an event row. The filtering lives in the hook, where it
can exit before paying for a Python start-up.

Observations are events, not ephemeral rows. An observation is evidence the
compiler can weigh -- "the test failed" outranks "I think the test passes" --
rather than a memory competing for the fifteen packet slots. Promoting one past
ephemeral needs a rule that does not exist yet, and inventing one here would be
the write-side filtering failure this system already has a measured history of.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlsplit

from arteries import runlog
from arteries.config import AGENT_PROCESS_ID, PROJECT_ID


def main(argv: list[str] | None = None) -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    # Two callers: the plugin's node hook sends a filtered {tool, exit_code,
    # failed, target}; hook-tool.sh sends Claude's raw PostToolUse event.
    session = event.get("session_id") or os.getenv("ARTERIES_SESSION_ID") or None
    if "tool_name" in event:
        url = (event.get("tool_input") or {}).get("url") or ""
        event = {"tool": event["tool_name"],
                 "target": urlsplit(url).netloc if url else "search"}
    tool = str(event.get("tool") or "")[:64]
    if not tool:
        return 0

    runlog.log_event(
        "tool.result", "arteries",
        {
            "tool": tool,
            "exit_code": int(event.get("exit_code") or 0),
            "failed": bool(event.get("failed")),
            # Path or command prefix only. Output is never recorded: it is
            # unbounded, it is the part that would actually cost something, and
            # nothing downstream reads it.
            "target": str(event.get("target") or "")[:200],
            # carried in the event, not only through the run: trust.py asks
            # "did this session fetch from the web", and a hook that fires
            # before any run row exists would otherwise answer no
            "session_id": session,
        },
        project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
