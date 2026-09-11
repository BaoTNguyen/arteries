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
import sys

from arteries import runlog
from arteries.config import AGENT_PROCESS_ID, PROJECT_ID


def main(argv: list[str] | None = None) -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

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
        },
        project_id=PROJECT_ID, agent_id=AGENT_PROCESS_ID,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
