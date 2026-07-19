"""Tee into the heart event spine: append-only NDJSON spool shared by the whole
stack (heart, arteries, marrow). Postgres remains the queryable archive; the
spool is the live view that `heart pulse` tails.

Optional and silent: ARTERIES_SPOOL=off disables; failures never propagate.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def spool_emit(source: str, kind: str, *, turn_id: str | None = None, **payload) -> None:
    if os.getenv("ARTERIES_SPOOL", "").lower() == "off":
        return
    try:
        now = datetime.now(timezone.utc)
        event: dict = {"ts": now.isoformat(), "source": source, "kind": kind}
        for key, value in (
            ("episode_id", os.getenv("ARTERIES_EPISODE_ID")),
            ("task_id", os.getenv("ARTERIES_TASK_ID")),
            ("turn_id", turn_id),
        ):
            if value:
                event[key] = value
        if payload:
            event["payload"] = payload
        spool = Path(
            os.environ.get("HEART_SPOOL_DIR", str(Path.home() / ".local" / "share" / "heart" / "events"))
        )
        spool.mkdir(parents=True, exist_ok=True)
        with open(spool / now.strftime("%Y%m%d.ndjson"), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception:
        pass
