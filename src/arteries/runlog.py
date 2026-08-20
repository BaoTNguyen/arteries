"""Minimal run/event telemetry for arteries and capillaries integration.

Runlog is intentionally small: append event facts, then evaluate later.
It writes to Postgres when available and falls back to repo-local JSONL.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

from arteries.config import AGENT_PROCESS_ID, DB_CONFIG, PROJECT_ID
from arteries.spool import spool_emit

RUN_ENV_KEYS = ("AGENT_RUN_ID", "ARTERIES_RUN_ID")


def new_turn_id() -> str:
    return str(uuid.uuid4())


def start_run(
    project_id: str | None = None,
    agent_id: str | None = None,
    cli: str | None = None,
    repo_path: str | Path | None = None,
) -> dict[str, Any]:
    repo = _repo(repo_path)
    run = _run_payload(
        str(uuid.uuid4()),
        _project(project_id, repo),
        _agent(agent_id),
        _cli(cli),
        repo,
    )
    _write_current_run(repo, run)
    log_event(
        "run.started",
        "arteries",
        {},
        project_id=run["project_id"],
        run_id=run["run_id"],
        agent_id=run["agent_id"],
        cli=run["cli"],
        repo_path=repo,
    )
    return run


def current_run(
    project_id: str | None = None,
    agent_id: str | None = None,
    cli: str | None = None,
    repo_path: str | Path | None = None,
) -> dict[str, Any]:
    repo = _repo(repo_path)
    project = _project(project_id, repo)
    agent = _agent(agent_id)
    cli_name = _cli(cli)

    run_id = _env_run_id()
    if run_id:
        return _run_payload(run_id, project, agent, cli_name, repo)

    current_path = repo / ".arteries" / "current-run.json"
    if current_path.exists():
        try:
            data = json.loads(current_path.read_text(encoding="utf-8"))
            if data.get("run_id"):
                return _run_payload(
                    str(data["run_id"]),
                    project_id or os.getenv("ARTERIES_PROJECT") or data.get("project_id") or PROJECT_ID or repo.name,
                    agent_id or os.getenv("ARTERIES_AGENT_ID") or data.get("agent_id") or AGENT_PROCESS_ID,
                    cli or os.getenv("AGENT_CLI") or os.getenv("ARTERIES_CLI") or data.get("cli") or "unknown",
                    repo,
                    started_at=data.get("started_at"),
                )
        except Exception:
            pass

    run = _run_payload(str(uuid.uuid4()), project, agent, cli_name, repo)
    _write_current_run(repo, run)
    return run


def log_event(
    event_type: str,
    source: str,
    payload: dict[str, Any] | None = None,
    *,
    project_id: str | None = None,
    run_id: str | None = None,
    turn_id: str | None = None,
    agent_id: str | None = None,
    cli: str | None = None,
    repo_path: str | Path | None = None,
) -> dict[str, Any]:
    run = current_run(project_id=project_id, agent_id=agent_id, cli=cli, repo_path=repo_path)
    if run_id:
        run["run_id"] = run_id
    event = {
        "id": str(uuid.uuid4()),
        "run_id": run["run_id"],
        "project_id": run["project_id"],
        "turn_id": turn_id,
        "event_type": event_type,
        "source": source,
        "payload": payload or {},
        "created_at": _now_iso(),
    }
    # stamp episode identity (set by heart per episode) so events join the ledger
    for key, env_key in (("episode_id", "ARTERIES_EPISODE_ID"), ("task_id", "ARTERIES_TASK_ID")):
        value = os.getenv(env_key)
        if value:
            event["payload"].setdefault(key, value)
    store = "db"
    try:
        _write_db_run(run)
        _write_db_event(event)
    except Exception:
        _write_jsonl(run, event)
        store = "jsonl"  # degradation signal: pulse health watches this
    spool_emit(
        source, event_type, turn_id=turn_id, store=store,
        **{k: v for k, v in event["payload"].items() if k not in ("episode_id", "task_id")},
    )
    return event


def log_failure(
    event_type: str,
    source: str,
    exc: Exception,
    *,
    project_id: str | None = None,
    run_id: str | None = None,
    turn_id: str | None = None,
    agent_id: str | None = None,
    cli: str | None = None,
    repo_path: str | Path | None = None,
) -> dict[str, Any]:
    return log_event(
        event_type,
        source,
        # repr, not str: httpx timeout exceptions stringify to "", which is how
        # 82 consecutive compile failures logged an empty error field and read
        # as healthy on the control plane.
        {"error": (str(exc) or repr(exc))[:300], "error_type": type(exc).__name__},
        project_id=project_id,
        run_id=run_id,
        turn_id=turn_id,
        agent_id=agent_id,
        cli=cli,
        repo_path=repo_path,
    )


def recent_events(project_id: str | None = None, limit: int = 25, repo_path: str | Path | None = None) -> list[dict[str, Any]]:
    project = project_id or os.getenv("ARTERIES_PROJECT") or PROJECT_ID
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, run_id, project_id, turn_id, event_type, source, payload, created_at
                FROM arteries.agent_events
                WHERE project_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (project, limit),
            )
            return [_json_ready(dict(row)) for row in cur.fetchall()]
    except Exception:
        return _recent_jsonl_events(project, limit, repo_path=repo_path)


def show_run(run_id: str, limit: int = 100, repo_path: str | Path | None = None) -> dict[str, Any]:
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM arteries.agent_runs WHERE id = %s", (run_id,))
            run = cur.fetchone()
            cur.execute(
                """
                SELECT id, run_id, project_id, turn_id, event_type, source, payload, created_at
                FROM arteries.agent_events
                WHERE run_id = %s
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (run_id, limit),
            )
            return {"run": _json_ready(dict(run)) if run else None, "events": [_json_ready(dict(row)) for row in cur.fetchall()]}
    except Exception:
        events = [event for event in _recent_jsonl_events(None, limit=1000, repo_path=repo_path) if event.get("run_id") == run_id]
        return {"run": None, "events": events[:limit]}


def summarize(project_id: str | None = None, limit: int = 100, repo_path: str | Path | None = None) -> dict[str, Any]:
    events = recent_events(project_id=project_id, limit=limit, repo_path=repo_path)
    counts: dict[str, int] = {}
    extracted = 0
    retrievals = 0
    compile_success = 0
    compile_failures = 0
    failures: list[dict[str, Any]] = []
    for event in events:
        event_type = event.get("event_type", "unknown")
        counts[event_type] = counts.get(event_type, 0) + 1
        payload = event.get("payload") or {}
        if event_type == "memory.ephemeral.extracted":
            extracted += int(payload.get("count") or 0)
        if event_type == "prompt.retrieved":
            retrievals += 1
        if event_type == "memory.compile.completed" and payload.get("status") == "compiled":
            compile_success += 1
        if event_type == "memory.compile.failed" or str(event_type).endswith(".failed"):
            compile_failures += 1 if event_type == "memory.compile.failed" else 0
            failures.append(event)
    return {
        "project_id": project_id or os.getenv("ARTERIES_PROJECT") or PROJECT_ID,
        "event_count": len(events),
        "latest_run_id": events[0].get("run_id") if events else None,
        "latest_event_at": events[0].get("created_at") if events else None,
        "counts_by_type": counts,
        "extracted_total": extracted,
        "retrieval_total": retrievals,
        "compile_success_total": compile_success,
        "compile_failure_total": compile_failures,
        "recent_failures": failures[:5],
    }


def _env_run_id() -> str | None:
    for key in RUN_ENV_KEYS:
        value = os.getenv(key)
        if value:
            return value
    return None


def _repo(repo_path: str | Path | None = None) -> Path:
    return Path(repo_path or os.getenv("ARTERIES_REPO") or Path.cwd()).resolve()


def _project(project_id: str | None, repo: Path) -> str:
    return project_id or os.getenv("ARTERIES_PROJECT") or PROJECT_ID or repo.name


def _agent(agent_id: str | None) -> str:
    return str(agent_id or os.getenv("ARTERIES_AGENT_ID") or AGENT_PROCESS_ID)


def _cli(cli: str | None) -> str:
    return cli or os.getenv("AGENT_CLI") or os.getenv("ARTERIES_CLI") or "unknown"


def _run_payload(run_id: str, project_id: str, agent_id: str, cli: str, repo: Path, started_at: str | None = None) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project_id": project_id,
        "repo_path": str(repo),
        "cli": cli,
        "agent_id": str(agent_id),
        "started_at": started_at or _now_iso(),
        "metadata": {},
    }


def _write_current_run(repo: Path, run: dict[str, Any]) -> None:
    current_path = repo / ".arteries" / "current-run.json"
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text(json.dumps(run, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _write_db_run(run: dict[str, Any]) -> None:
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arteries.agent_runs
                (id, project_id, repo_path, cli, agent_id, metadata)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                run["run_id"],
                run["project_id"],
                run.get("repo_path"),
                run.get("cli"),
                run.get("agent_id"),
                json.dumps(run.get("metadata") or {}, default=str),
            ),
        )
        conn.commit()


def _write_db_event(event: dict[str, Any]) -> None:
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO arteries.agent_events
                (id, run_id, project_id, turn_id, event_type, source, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                event["id"],
                event["run_id"],
                event["project_id"],
                event.get("turn_id"),
                event["event_type"],
                event["source"],
                json.dumps(event.get("payload") or {}, default=str),
            ),
        )
        conn.commit()


def _write_jsonl(run: dict[str, Any], event: dict[str, Any]) -> None:
    root = Path(run.get("repo_path") or Path.cwd()) / ".arteries" / "runs"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{run['run_id']}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "event", **event}, sort_keys=True, default=str) + "\n")


def _recent_jsonl_events(project_id: str | None, limit: int, repo_path: str | Path | None = None) -> list[dict[str, Any]]:
    roots = [_repo(repo_path) / ".arteries" / "runs"]
    events: list[dict[str, Any]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("kind") != "event":
                    continue
                if project_id and event.get("project_id") != project_id:
                    continue
                events.append(event)
    events.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return events[:limit]


def _json_ready(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key, value in list(out.items()):
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif isinstance(value, uuid.UUID):
            out[key] = str(value)
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
