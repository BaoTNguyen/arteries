"""The event journal: append-only NDJSON shared by the whole stack (heart,
arteries, capillaries, plexus). Postgres remains the queryable archive; the
journal is the live record that `heart pulse` tails.

Named for what it contains rather than for a buffer bytes wait in: an ordered,
append-only account of what happened, which is the property every reader
actually depends on. The name also stops claiming this belongs to heart, which
it never did -- arteries, capillaries and plexus all write here.

This is also the outbound channel for sandboxed runs. A container that cannot
reach Postgres can still append here through a bind mount, which is the whole
point: appending needs no credential, and a database connection does.

Optional and silent: ARTERIES_JOURNAL=off disables; failures never propagate.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def journal_dir() -> Path:
    # The directory is deliberately unchanged by the rename: the files on disk
    # are one continuous record, and moving them would strand every event
    # written before today behind a path nothing reads.
    return Path(os.environ.get("EVENT_JOURNAL_DIR",
                               str(Path.home() / ".local" / "share" / "heart" / "events")))


def inbox(run_id: str, root: str | Path | None = None) -> Path:
    """The per-run directory a sandbox writes into.

    One shared daily file works while every writer is a host process. Once
    containers write too, concurrent appends over PIPE_BUF (4096 bytes) can
    interleave -- and an assistant_preview alone runs to 2000 characters before
    JSON escaping. Giving each run its own inbox makes interleaving structurally
    impossible instead of merely unlikely, and it means a container can neither
    read nor corrupt another run's record.
    """
    return Path(root or journal_dir()) / "incoming" / run_id


def journal_append(source: str, kind: str, *, turn_id: str | None = None,
                   event_id: str | None = None, run_id: str | None = None,
                   **payload) -> None:
    if os.getenv("ARTERIES_JOURNAL", "").lower() == "off":
        return
    try:
        now = datetime.now(timezone.utc)
        event: dict = {"ts": now.isoformat(), "source": source, "kind": kind}
        for key, value in (
            # id and run_id ride at the top level so the drain can insert this
            # row idempotently without unpacking the payload
            ("id", event_id),
            ("run_id", run_id),
            ("episode_id", os.getenv("ARTERIES_EPISODE_ID")),
            ("task_id", os.getenv("ARTERIES_TASK_ID")),
            ("turn_id", turn_id),
        ):
            if value:
                event[key] = value
        if payload:
            event["payload"] = payload
        journal = journal_dir()
        journal.mkdir(parents=True, exist_ok=True)
        with open(journal / now.strftime("%Y%m%d.ndjson"), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception:
        pass


def drain(root: str | Path | None = None, delete: bool = True) -> dict[str, int]:
    """Fold every sandbox inbox back into the journal and the database.

    Runs on the host, where the credentials are. A container appends lines and
    knows nothing else; this is the only side that needs a database connection,
    which is the whole reason the channel is a file.

    Idempotent on the event id, so a drain killed halfway can simply run again --
    the alternative is a half-ingested file that nobody can tell apart from a
    fully ingested one.

    Returns counts, because a drain that silently does nothing looks exactly
    like a drain with nothing to do.
    """
    base = Path(root or journal_dir())
    incoming = base / "incoming"
    counts = {"files": 0, "lines": 0, "stored": 0}
    if not incoming.is_dir():
        return counts

    base.mkdir(parents=True, exist_ok=True)
    merged = base / datetime.now(timezone.utc).strftime("%Y%m%d.ndjson")

    for path in sorted(incoming.glob("*/*.ndjson")):
        try:
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            continue

        rows = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # a container killed mid-write leaves a partial last line
                continue
            if isinstance(event, dict) and event.get("source") == "arteries" and event.get("id"):
                rows.append(event)

        # Store before merging, and delete only if both succeeded. The inbox file
        # is the only durable copy until then: merging first would duplicate
        # lines on every retry, and deleting first would lose the events outright
        # the one time the database is down.
        if rows and _store(rows) == 0:
            continue

        with open(merged, "a", encoding="utf-8") as out:
            for line in lines:
                out.write(line + "\n")
        counts["files"] += 1
        counts["lines"] += len(lines)
        counts["stored"] += len(rows)
        if delete:
            path.unlink(missing_ok=True)

    for run_dir in sorted(incoming.iterdir()):
        if run_dir.is_dir() and not any(run_dir.iterdir()):
            run_dir.rmdir()
    return counts


def _store(events: list[dict]) -> int:
    """Insert drained arteries events, skipping any already there."""
    if not events:
        return 0
    try:
        import psycopg2
        import psycopg2.extras

        from arteries.config import DB_CONFIG
        rows = [
            (e["id"], e.get("run_id"), (e.get("payload") or {}).get("project_id"),
             e.get("turn_id"), e.get("kind"), e.get("source"),
             json.dumps(e.get("payload") or {}, default=str), e.get("ts"))
            for e in events
        ]
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, """
                INSERT INTO arteries.agent_events
                    (id, run_id, project_id, turn_id, event_type, source, payload, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (id) DO NOTHING
            """, rows)
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(rows)
    except Exception:
        # The lines are already merged into the journal, so nothing is lost --
        # a later drain picks them up once the database is reachable again.
        return 0
