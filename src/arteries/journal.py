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
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

from arteries.config import DB_CONFIG, PROJECT_ID as _PROJECT


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


def drain(root: str | Path | None = None, delete: bool = True,
          backfill: bool = False) -> dict[str, int]:
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
    base.mkdir(parents=True, exist_ok=True)
    counts["stored"] += _ingest_daily(base, None if backfill else 2)
    if not incoming.is_dir():
        return counts

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
            if (row := _storable(event)) is not None:
                rows.append(row)

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


def _ingest_daily(base: Path, days: int | None = 2) -> int:
    """Store the events already in the journal's own daily files.

    An inbox is where a process writes when it cannot reach the database -- a
    container, mainly. Every other process heart runs writes straight to the
    daily file, and the drain never looked there, so no event heart has ever
    emitted reached the database: not from a sandbox, not from a plain local
    run. The channel had a reader for one of its two writers.

    There is no "sandboxed events" and "ordinary events". There is one event
    stream and two places a writer can put a line depending on what it can
    reach, and both are files on this disk.

    Re-reading the whole file on every drain is safe rather than clever: ids are
    derived from the event's own bytes, so a line already stored conflicts with
    itself and is skipped. Nothing is deleted here -- the daily file is the
    record, not a queue.
    """
    rows = []
    paths = sorted(base.glob("[0-9]*.ndjson"))
    # Only the recent files by default: ids make a re-read harmless, not free,
    # and the archive is 30k lines. `backfill=True` takes the lot, once.
    for path in (paths if days is None else paths[-days:]):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (row := _storable(event)) is not None:
                rows.append(row)
    return _store(rows) if rows else 0


def _known_run_ids(ids: set) -> set:
    """Which of these run ids exist in agent_runs. Empty on any failure, which
    detaches every id -- the events still land, just without the join."""
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute("SELECT id::text FROM arteries.agent_runs WHERE id::text = ANY(%s)",
                        (list(ids),))
            return {row[0] for row in cur.fetchall()}
    except Exception:
        return set()


def _detach_unknown_runs(events: list[dict]) -> list[dict]:
    """Blank any run_id that has no row in agent_runs.

    agent_events.run_id is a foreign key, and execute_batch sends the whole
    batch as one statement: one event naming a run that was never inserted --
    or was pruned -- takes every other event in the drain down with it, and the
    failure is swallowed as "stored 0". That is how 30,707 journal lines
    reported nothing wrong while landing nothing.

    An event whose run is gone is still an event. It loses the join, not its
    place in the record.
    """
    ids = {e.get("run_id") for e in events if e.get("run_id")}
    if not ids:
        return events
    known = _known_run_ids(ids)
    return [e if not e.get("run_id") or str(e["run_id"]) in known
            else {**e, "run_id": None} for e in events]


def _payload(event: dict) -> dict:
    """The event's payload, with the fields heart keeps at top level folded in.

    `episode_id` is the one that matters: it is what ties a sandboxed run's
    events to its episode, and the agent_events table has no column for it. On
    the outside of the payload it was simply lost.
    """
    payload = dict(event.get("payload") or {})
    for key in ("episode_id", "task_id", "role", "duration_ms"):
        if (value := event.get(key)) is not None:
            payload.setdefault(key, value)
    return payload


def _storable(event: object) -> dict | None:
    """An inbox line as a row, or None if it is not an event.

    The filter used to be `source == "arteries" and event.get("id")`, which no
    sandboxed run could ever satisfy: everything heart writes is
    `source: "heart"` with no id, so a drain read 54 lines, stored 0, merged
    them into the daily file and deleted the inbox. The journal was the channel
    chosen over mounting the Postgres socket into the container, and it was
    dropping every event that came through it.

    An id is synthesised from the event's own bytes rather than required. It is
    deterministic, so a re-drain of the same line conflicts with the row already
    there instead of duplicating it -- which is what the `id` was doing for
    arteries' own events, just supplied rather than derived.
    """
    if not isinstance(event, dict) or not event.get("kind"):
        return None
    if not event.get("id"):
        digest = json.dumps(event, sort_keys=True, default=str)
        event = {**event, "id": str(uuid.uuid5(uuid.NAMESPACE_URL, digest))}
    return event


def _store(events: list[dict]) -> int:
    """Insert drained arteries events, skipping any already there."""
    if not events:
        return 0
    try:
        events = _detach_unknown_runs(events)
        rows = [
            (e["id"], e.get("run_id"),
             (e.get("payload") or {}).get("project_id") or _PROJECT,
             e.get("turn_id"), e.get("kind"), e.get("source"),
             json.dumps(_payload(e), default=str), e.get("ts"))
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
