"""Small health check for arteries runtime wiring."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg2

from arteries import runlog
from arteries.config import AGENT_PROCESS_ID, DB_CONFIG, PROJECT_ID


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check arteries project/run logging setup.")
    parser.add_argument("--project", default=os.getenv("ARTERIES_PROJECT") or PROJECT_ID)
    parser.add_argument("--agent", default=os.getenv("ARTERIES_AGENT_ID") or AGENT_PROCESS_ID)
    parser.add_argument("--cli", default=os.getenv("ARTERIES_CLI") or os.getenv("AGENT_CLI") or "unknown")
    parser.add_argument("--repo", type=Path, default=Path(os.getenv("ARTERIES_REPO") or Path.cwd()))
    ns = parser.parse_args(argv)

    report = check(ns.project, ns.agent, ns.cli, ns.repo)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def check(project: str, agent: str, cli: str, repo: Path) -> dict[str, Any]:
    fallback = repo / ".arteries" / "runs"
    checks: dict[str, Any] = {
        "project_id": project,
        "agent_id": str(agent),
        "cli": cli,
        "repo": str(repo.resolve()),
        "fallback_path": str(fallback),
    }

    db_ok, schema_ok, db_error = _check_db()
    checks["db_ok"] = db_ok
    checks["schema_ok"] = schema_ok
    if db_error:
        checks["db_error"] = db_error

    try:
        fallback.mkdir(parents=True, exist_ok=True)
        checks["fallback_ok"] = fallback.exists() and os.access(fallback, os.W_OK)
    except Exception as exc:
        checks["fallback_ok"] = False
        checks["fallback_error"] = str(exc)

    event = runlog.log_event(
        "doctor.write_test",
        "arteries",
        {},
        project_id=project,
        agent_id=agent,
        cli=cli,
        repo_path=repo,
    )
    recent = runlog.recent_events(project, limit=10, repo_path=repo)
    checks["write_ok"] = bool(event.get("id"))
    checks["read_ok"] = any(row.get("id") == event.get("id") for row in recent)
    checks["run_id"] = event.get("run_id")
    checks["ok"] = bool(checks["fallback_ok"] and checks["write_ok"] and checks["read_ok"] and (not db_ok or schema_ok))
    return checks


def _check_db() -> tuple[bool, bool, str | None]:
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT to_regclass('arteries.agent_runs'),
                       to_regclass('arteries.agent_events')
                """
            )
            runs, events = cur.fetchone()
            return True, bool(runs and events), None
    except Exception as exc:
        return False, False, str(exc)[:300]


if __name__ == "__main__":
    raise SystemExit(main())
