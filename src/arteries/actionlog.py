"""Decision/action ledger: what was available, what was chosen, what it cost.

Events (runlog) record what happened. Decisions record the counterfactual —
the available actions, the choice, and its cost — which is what RL credit
assignment needs. Postgres when available, repo-local JSONL fallback
(.vascular/arteries/decisions/), always teed to the heart event spine.

Episode/task identity arrives via env (set by heart per episode):
    ARTERIES_EPISODE_ID
    ARTERIES_TASK_ID
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

from arteries import pulse_read, runlog, storage, vascular_paths
from arteries.config import AGENT_PROCESS_ID, DB_CONFIG, PROJECT_ID
from arteries.journal import journal_append


def episode_id() -> str | None:
    return os.getenv("ARTERIES_EPISODE_ID") or None


def task_id() -> str | None:
    return os.getenv("ARTERIES_TASK_ID") or None


def log_decision(
    decision_type: str,
    chosen_action: str,
    available_actions: list[str],
    *,
    observation: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    turn_id: str | None = None,
    repo_path: str | Path | None = None,
) -> dict[str, Any]:
    run = _run(repo_path)
    record = {
        "id": str(uuid.uuid4()),
        "episode_id": episode_id(),
        "run_id": run["run_id"],
        "turn_id": turn_id,
        "project_id": run["project_id"],
        "agent_id": run["agent_id"],
        "decision_type": decision_type,
        "observation": observation or {},
        "available_actions": available_actions,
        "chosen_action": chosen_action,
        "cost": cost or {},
        "metadata": {**(metadata or {}), **({"task_id": task_id()} if task_id() else {})},
        "created_at": _now_iso(),
    }
    store = _persist(record, "decision", run, repo_path)
    # The whole record rides on the event (pulse P5, DIRECTION A3), keyed by the
    # decision's own id, so the table can be checked against pulse row for row
    # and then retired. Observations are small, so inline rather than artifacts.
    journal_append(
        "arteries",
        f"decision.{decision_type}",
        turn_id=turn_id,
        event_id=record["id"],
        run_id=record["run_id"],
        chosen=chosen_action,
        available=available_actions,
        store=store,
        **({"cost": cost} if cost else {}),
        **({"observation": record["observation"]} if record["observation"] else {}),
        **({"metadata": record["metadata"]} if record["metadata"] else {}),
        project_id=record["project_id"],
        agent_id=record["agent_id"],
        created_at=record["created_at"],
    )
    return record


def recent_decisions(
    project_id: str | None = None,
    limit: int = 25,
    episode: str | None = None,
    repo_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    project = project_id or os.getenv("ARTERIES_PROJECT") or PROJECT_ID
    # Pulse first (decision.* events that carry the whole record, newest
    # first), topped up from the table with rows older than the oldest used.
    # ponytail: one 500-event page; matches past it come from the table
    events = pulse_read.events(kind="decision.*", source="arteries", order="desc", limit=500)
    rows: list[dict[str, Any]] = []
    for e in events or []:
        if len(rows) >= limit:
            break
        p = e.get("payload") or {}
        if "created_at" not in p:
            continue  # written before the event carried the record
        if (e.get("episode_id") != episode) if episode else (p.get("project_id") != project):
            continue
        rows.append(_decision_from_event(e))
    if len(rows) >= limit:
        return rows
    before = rows[-1]["created_at"] if rows else None
    try:
        return rows + _recent_decisions_table(project, episode, limit - len(rows), before)
    except Exception:
        if rows:
            return rows
        return _recent_jsonl(project if not episode else None, episode, limit, repo_path)


def _decision_from_event(e: dict[str, Any]) -> dict[str, Any]:
    p = e.get("payload") or {}
    return {
        "id": e.get("id"),
        "episode_id": e.get("episode_id"),
        "run_id": e.get("run_id"),
        "turn_id": e.get("turn_id"),
        "project_id": p.get("project_id"),
        "decision_type": e["kind"].removeprefix("decision."),
        "observation": p.get("observation") or {},
        "available_actions": p.get("available") or [],
        "chosen_action": p.get("chosen"),
        "cost": p.get("cost") or {},
        "metadata": p.get("metadata") or {},
        "created_at": p["created_at"],
    }


def _recent_decisions_table(
    project: str, episode: str | None, limit: int, before: str | None = None,
) -> list[dict[str, Any]]:
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        where, params = "project_id = %s", [project]
        if episode:
            where, params = "episode_id = %s", [episode]
        if before:
            where += " AND created_at < %s"
            params.append(before)
        cur.execute(
            f"""
            SELECT id, episode_id, run_id, turn_id, project_id, decision_type,
                   observation, available_actions, chosen_action, cost, metadata, created_at
            FROM arteries.decisions
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (*params, limit),
        )
        return [runlog._json_ready(dict(row)) for row in cur.fetchall()]


def _corpus_feedback(episode: dict) -> None:
    """Tell capillaries how the prompt it suggested turned out.

    Here rather than in heart for the same reason the gate is: the direction is
    capillaries -> arteries -> heart, and heart reaching back around arteries to
    report on a component it should not know about inverts that. This function
    already has the episode's outcome in hand.

    Best-effort: retrieval feedback must never be what fails a finished episode.
    """
    outcome = episode.get("outcome")
    for packet in episode.get("context_packets") or []:
        trace_id = (packet.get("corpus") or {}).get("trace_id")
        if not trace_id or not outcome:
            continue
        _corpus_feedback_post({"trace_id": trace_id, "outcome": outcome,
                               "notes": f"heart role={packet.get('role')}"})


def _corpus_feedback_post(body: dict) -> None:
    """Post to the daemon rather than calling FeedbackHandler directly.

    The handler wants `mode`, `prompt_id` and `skill_id` -- internals the API
    layer resolves from the trace. POST /agent/feedback needs only trace_id and
    outcome, so it is the contract that does not break when those internals
    move.
    """
    url = os.getenv("CAPILLARIES_URL", "http://127.0.0.1:8000") + "/agent/feedback"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).close()
    except Exception:
        pass


def ingest_episodes(source: str | Path | None = None) -> int:
    """Take in finished heart episodes. Returns how many were new.

    Per episode: write its memory row, tell capillaries how its suggestions
    turned out, and close its arteries.episodes row. Heart is stdlib-only and
    never talks to Postgres, so this is where those facts arrive.

    `source` may be a JSONL path, a directory of `*/episode.json`, or None to
    read JSONL from stdin. **Prefer stdin.** Arteries defines the record shape
    and the sender pipes it in; reaching into another repo's directory layout
    couples this to whichever of them happens to own the filesystem this week.

    Deduped on the closed row: an episode already closed is not re-posted to
    capillaries, which inserts a feedback row per POST.
    """
    if source is None:
        episodes = [json.loads(line) for line in sys.stdin.read().splitlines() if line.strip()]
    else:
        path = Path(source)
        if path.is_dir():
            episodes = [json.loads(p.read_text()) for p in sorted(path.glob("*/episode.json"))]
        else:
            episodes = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    closed: set = set()
    recorded: set = set()
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM arteries.episodes WHERE ended_at IS NOT NULL")
            closed = {row[0] for row in cur.fetchall()}
            # one query rather than an exists-check per episode: a runs
            # directory is hundreds of episodes and this pass reads all of them
            cur.execute("""SELECT episode_id FROM arteries.persistent
                            WHERE kind = 'episode' AND episode_id IS NOT NULL
                              AND valid_until IS NULL""")
            recorded = {row[0] for row in cur.fetchall()}
    except Exception:
        pass  # ponytail: no dedup with the db down; re-ingest after it is back

    closures: list[tuple[str, str | None, str | None]] = []
    for ep in episodes:
        eid = ep.get("episode_id")
        if not eid:
            continue
        # before the closed skip: an already-closed episode still needs its
        # memory row written once, or history stays invisible to memory
        if eid not in recorded:
            record_episode(ep)
            recorded.add(eid)
        if eid in closed:
            continue
        _corpus_feedback(ep)
        closures.append((eid, ep.get("outcome"), ep.get("task_id")))
    close_episodes(closures)
    return len(closures)


def episode_fact(ep: dict) -> str:
    """One line of what an episode did. Mechanical only.

    Every clause is read off episode.json -- outcome, which verifiers ran and
    what they said, how big the diff was, which agent. No summary of the
    agent's reasoning and no judgment about whether the work was good: a
    model's account of its own failed run is the last thing that should harden
    into memory.
    """
    parts = [f"heart episode {ep.get('episode_id')}"]
    if task := ep.get("task_id"):
        parts.append(f"({task})")
    if repo := ep.get("repo_path"):
        parts.append(f"on {Path(repo).name}")
    if base := ep.get("base_commit"):
        parts.append(f"@{base[:12]}")
    line = " ".join(parts) + f": {ep.get('outcome') or 'unknown'}"
    if agent := ep.get("agent"):
        line += f"; agent {agent}"
    if (lines := ep.get("diff_lines")) is not None:
        line += f"; {lines} diff lines"
    verdicts = [f"{name} {'passed' if r.get('passed') else 'failed'}"
                for name, r in (ep.get("verifier_results") or {}).items()]
    if verdicts:
        line += "; verifiers: " + ", ".join(sorted(verdicts))
    if refused := ep.get("scope_refused_paths"):
        line += f"; refused {len(refused)} path(s)"
    return line


def record_episode(ep: dict) -> str | None:
    """Write what an episode did straight to persistent. Returns the row id.

    Straight to persistent, with no ephemeral row in between, because the
    compile pass exists to turn a session's loose turns into a durable claim and
    there is nothing loose here: episode.json is already the compiled form. A
    model call to re-say it would add cost, a failure mode at wrap-up, and an
    opportunity to invent.

    Written for every episode, not just sandboxed ones -- the sandbox is a scope
    boundary, not a category of run.

    Idempotent on episode_id, so a re-ingest of the same runs directory does not
    accumulate one row per pass.
    """
    eid = ep.get("episode_id")
    if not eid:
        return None
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute("""SELECT 1 FROM arteries.persistent
                            WHERE episode_id = %s AND kind = 'episode'
                              AND valid_until IS NULL LIMIT 1""", (eid,))
            if cur.fetchone():
                return None
        return storage.insert_persistent(
            project_id=os.getenv("ARTERIES_PROJECT") or PROJECT_ID,
            fact=episode_fact(ep),
            domains=["episode"],
            # embedding deferred: `art doctor` backfills null embeddings, and a
            # wrap-up that needs the embedding service up is a wrap-up that
            # fails when it is down
            embedding=None,
            source_meta={"source": "heart", "outcome": ep.get("outcome"),
                         "agent": ep.get("agent"), "repo_path": ep.get("repo_path"),
                         "base_commit": ep.get("base_commit"),
                         "diff_lines": ep.get("diff_lines"),
                         "usage": ep.get("usage") or {}},
            episode_id=eid,
            task_id=ep.get("task_id"),
            kind="episode",
        )
    except Exception:
        # a memory row is never worth failing an ingest over
        return None


def close_episodes(closures: list[tuple[str, str | None, str | None]]) -> int:
    """Mark episodes finished, creating the row if nothing has yet. Returns how
    many rows moved off `running`. `closures` is (episode_id, outcome, task_id).

    Creating it here matters: rows used to be born only when a decision or a
    reward carried the episode id, so an episode with neither had no row to
    close and was re-ingested -- and re-posted to capillaries -- on every pass.

    `WHERE ended_at IS NULL` so a re-ingest cannot rewrite history, the same
    guard runlog.close_run has always used.
    """
    rows = [(eid, os.getenv("ARTERIES_PROJECT") or PROJECT_ID, task, outcome or "finished")
            for eid, outcome, task in closures if eid]
    if not rows:
        return 0
    moved = 0
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            for row in rows:
                cur.execute("""
                    INSERT INTO arteries.episodes (id, project_id, task_id, status, ended_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (id) DO UPDATE
                       SET status = EXCLUDED.status, ended_at = now()
                     WHERE arteries.episodes.ended_at IS NULL
                """, row)
                moved += max(cur.rowcount, 0)
            conn.commit()
    except Exception:
        # a closed status is a read-side convenience, never worth failing an
        # ingest over
        return 0
    return moved


def _run(repo_path: str | Path | None) -> dict[str, Any]:
    try:
        return runlog.current_run(repo_path=repo_path)
    except Exception:
        return {
            "run_id": None,
            "project_id": os.getenv("ARTERIES_PROJECT") or PROJECT_ID,
            "agent_id": str(AGENT_PROCESS_ID),
            "repo_path": str(repo_path or Path.cwd()),
        }


def _persist(record: dict, kind: str, run: dict, repo_path: str | Path | None) -> str:
    """Returns where the write landed: db | jsonl | lost (degradation signal)."""
    try:
        _db_insert_decision(record)
        return "db"
    except Exception:
        try:
            _write_jsonl(run, kind, record, repo_path)
            return "jsonl"
        except Exception:
            return "lost"  # the journal tee still fires; never break the caller


def _db_insert_decision(record: dict) -> None:
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        _upsert_episode(cur, record)
        cur.execute(
            """
            INSERT INTO arteries.decisions
                (id, episode_id, run_id, turn_id, project_id, agent_id, decision_type,
                 observation, available_actions, chosen_action, cost, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s::jsonb)
            """,
            (
                record["id"], record["episode_id"], record["run_id"], record["turn_id"],
                record["project_id"], record["agent_id"], record["decision_type"],
                json.dumps(record["observation"], default=str),
                json.dumps(record["available_actions"], default=str),
                record["chosen_action"],
                json.dumps(record["cost"], default=str),
                json.dumps(record["metadata"], default=str),
            ),
        )
        conn.commit()


def _upsert_episode(cur, record: dict) -> None:
    # the ledger self-populates episode rows: heart never talks to Postgres,
    # so the first decision carrying an episode id creates the episode
    if not record.get("episode_id"):
        return
    cur.execute(
        """
        INSERT INTO arteries.episodes (id, project_id, agent_id, task_id, run_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            record["episode_id"], record["project_id"], record.get("agent_id"),
            task_id(), record.get("run_id"),
        ),
    )


def _write_jsonl(run: dict, kind: str, record: dict, repo_path: str | Path | None) -> None:
    root = vascular_paths.repo_dir(
        repo_path or run.get("repo_path") or os.getenv("ARTERIES_REPO") or Path.cwd(), "arteries"
    ) / "decisions"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, **record}, sort_keys=True, default=str) + "\n")


def _recent_jsonl(
    project_id: str | None, episode: str | None, limit: int, repo_path: str | Path | None
) -> list[dict[str, Any]]:
    root = vascular_paths.repo_dir(
        repo_path or os.getenv("ARTERIES_REPO") or Path.cwd(), "arteries"
    ) / "decisions"
    records: list[dict[str, Any]] = []
    if not root.exists():
        return records
    for path in sorted(root.glob("*.jsonl"), reverse=True):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") != "decision":
                continue
            if project_id and rec.get("project_id") != project_id:
                continue
            if episode and rec.get("episode_id") != episode:
                continue
            records.append(rec)
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return records[:limit]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
