"""Decision/action ledger: what was available, what was chosen, what it cost.

Events (runlog) record what happened. Decisions record the counterfactual —
the available actions, the choice, and its cost — which is what RL credit
assignment needs. Postgres when available, repo-local JSONL fallback
(.arteries/decisions/), always teed to the heart event spine.

Episode/task identity arrives via env (set by heart per episode):
    ARTERIES_EPISODE_ID
    ARTERIES_TASK_ID
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

from arteries import runlog
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
    journal_append(
        "arteries",
        f"decision.{decision_type}",
        turn_id=turn_id,
        chosen=chosen_action,
        available=available_actions,
        store=store,
        **({"cost": cost} if cost else {}),
    )
    return record


def log_reward(
    reward_type: str,
    value: float,
    *,
    components: dict[str, Any] | None = None,
    source: str = "arteries",
    decision_id: str | None = None,
    turn_id: str | None = None,
    repo_path: str | Path | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
) -> dict[str, Any]:
    run = _run(repo_path)
    record = {
        "id": str(uuid.uuid4()),
        "episode_id": episode_id(),
        "decision_id": decision_id,
        "run_id": run["run_id"],
        "project_id": run["project_id"],
        "reward_type": reward_type,
        "value": float(value),
        "components": components or {},
        "source": source,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "created_at": _now_iso(),
    }
    store = _persist(record, "reward", run, repo_path)
    journal_append(
        "arteries", f"reward.{reward_type}", turn_id=turn_id,
        value=value, reward_source=source, store=store,
    )
    return record


def recent_decisions(
    project_id: str | None = None,
    limit: int = 25,
    episode: str | None = None,
    repo_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    project = project_id or os.getenv("ARTERIES_PROJECT") or PROJECT_ID
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            where, params = "project_id = %s", [project]
            if episode:
                where, params = "episode_id = %s", [episode]
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
    except Exception:
        return _recent_jsonl(project if not episode else None, episode, limit, repo_path)


def _corpus_feedback(episode: dict) -> None:
    """Tell capillaries how the prompt it suggested turned out.

    Here rather than in heart for the same reason the gate is: the direction is
    capillaries -> arteries -> heart, and heart reaching back around arteries to
    report on a component it should not know about inverts that. This function
    already has the episode's outcome and reward in hand.

    Arteries had the whole loop for its own memory -- situation out, reward back
    through this ingest, joined on episode_id. Capillaries heard the question
    and never the answer, so its relevance signal could not exist at all. It is
    deliberately not the same signal as the episode reward (they are joined,
    neither replaces the other), but it needs the outcome to be computed from.

    Best-effort: retrieval feedback must never be what fails a finished episode.
    """
    outcome = episode.get("outcome")
    total = (episode.get("reward") or {}).get("total")
    for packet in episode.get("context_packets") or []:
        trace_id = (packet.get("corpus") or {}).get("trace_id")
        if not trace_id or not outcome:
            continue
        body = {"trace_id": trace_id, "outcome": outcome,
                "notes": f"heart role={packet.get('role')}"}
        if total is not None:
            body["quality_score"] = max(0.0, min(1.0, float(total)))
        _corpus_feedback_post(body)


def _corpus_feedback_post(body: dict) -> None:
    """Post to the daemon rather than calling FeedbackHandler directly.

    The handler wants `mode`, `prompt_id` and `skill_id` -- internals the API
    layer resolves from the trace. POST /agent/feedback needs only trace_id and
    outcome, so it is the contract that does not break when those internals
    move.
    """
    import urllib.request

    url = os.getenv("CAPILLARIES_URL", "http://127.0.0.1:8000") + "/agent/feedback"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).close()
    except Exception:
        pass


def ingest_episodes(source: str | Path | None = None, repo_path: str | Path | None = None) -> int:
    """Backfill the rewards table from episode records.

    This is the credit-assignment bridge: heart and marrow are stdlib-only and
    never talk to Postgres, so decisions get their episode reward here.

    `source` may be a JSONL path, a directory of `*/episode.json`, or None to
    read JSONL from stdin. **Prefer stdin.** Arteries defines the record shape
    and the sender pipes it in; reaching into another repo's directory layout
    couples this to whichever of them happens to own the filesystem this week,
    and RL traffic is moving to marrow.
    """
    if source is None:
        episodes = [json.loads(line) for line in sys.stdin.read().splitlines() if line.strip()]
    else:
        path = Path(source)
        if path.is_dir():
            episodes = [json.loads(p.read_text()) for p in sorted(path.glob("*/episode.json"))]
        else:
            episodes = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    ingested = set()
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT episode_id FROM arteries.rewards WHERE reward_type = 'episode'")
            ingested = {row[0] for row in cur.fetchall()}
    except Exception:
        pass  # ponytail: no dedup in jsonl-fallback mode; re-ingest after db is back

    count = 0
    saved = {k: os.environ.get(k) for k in ("ARTERIES_EPISODE_ID", "ARTERIES_TASK_ID")}
    try:
        for ep in episodes:
            if not ep.get("episode_id") or ep["episode_id"] in ingested:
                continue
            os.environ["ARTERIES_EPISODE_ID"] = ep["episode_id"]
            os.environ["ARTERIES_TASK_ID"] = ep.get("task_id") or ""
            usage = ep.get("usage") or {}
            log_reward(
                "episode",
                ep.get("reward", {}).get("total", 0.0),
                components={**ep.get("reward", {}).get("components", {}),
                            "outcome": ep.get("outcome")},
                source="heart",
                repo_path=repo_path,
                tokens_in=usage.get("tokens_in"),
                tokens_out=usage.get("tokens_out"),
                cost_usd=usage.get("cost_usd"),
            )
            _corpus_feedback(ep)
            count += 1
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    return count


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
        if kind == "decision":
            _db_insert_decision(record)
        else:
            _db_insert_reward(record)
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


def _db_insert_reward(record: dict) -> None:
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        _upsert_episode(cur, record)
        cur.execute(
            """
            INSERT INTO arteries.rewards
                (id, episode_id, decision_id, run_id, project_id, reward_type,
                 value, components, source, tokens_in, tokens_out, cost_usd)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            """,
            (
                record["id"], record["episode_id"], record.get("decision_id"),
                record["run_id"], record["project_id"], record["reward_type"],
                record["value"], json.dumps(record["components"], default=str),
                record["source"], record.get("tokens_in"), record.get("tokens_out"),
                record.get("cost_usd"),
            ),
        )
        conn.commit()


def _upsert_episode(cur, record: dict) -> None:
    # the ledger self-populates episode rows: heart never talks to Postgres,
    # so the first decision/reward carrying an episode id creates the episode
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
    root = Path(
        repo_path or run.get("repo_path") or os.getenv("ARTERIES_REPO") or Path.cwd()
    ) / ".arteries" / "decisions"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, **record}, sort_keys=True, default=str) + "\n")


def _recent_jsonl(
    project_id: str | None, episode: str | None, limit: int, repo_path: str | Path | None
) -> list[dict[str, Any]]:
    root = Path(
        repo_path or os.getenv("ARTERIES_REPO") or Path.cwd()
    ) / ".arteries" / "decisions"
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


# heart still calls this name; marrow will use ingest_episodes directly.
ingest_heart_episodes = ingest_episodes
