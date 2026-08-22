"""Inspect current arteries memory and runlog state."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from arteries import runlog, storage
from arteries.config import AGENT_PROCESS_ID, PROJECT_ID


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect current arteries state.")
    parser.add_argument("--project", default=PROJECT_ID)
    parser.add_argument("--agent", default=AGENT_PROCESS_ID)
    parser.add_argument("--events", type=int, default=10)
    ns = parser.parse_args(argv)

    summary = {
        "project_id": ns.project,
        "agent_id": ns.agent,
        "ephemeral": _safe(lambda: storage.get_ephemeral(ns.project, ns.agent, limit=10)),
        "persistent": _safe(lambda: storage.get_persistent(ns.project, limit=10)),
        "recent_events": runlog.recent_events(ns.project, limit=ns.events),
    }
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


def _safe(fn):
    try:
        return fn()
    except Exception as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    raise SystemExit(main())
