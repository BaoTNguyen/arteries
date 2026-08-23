"""Central trace view for a repo using arteries."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

from arteries import runlog, storage
from arteries.config import DB_CONFIG


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trace arteries activity for a target repo.")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repo to inspect")
    parser.add_argument("--project", help="project id; defaults to target .arteries/config.json or repo name")
    parser.add_argument("--agent", help="agent id; defaults to target .arteries/config.json")
    parser.add_argument("--events", type=int, default=50, help="recent events to include")
    parser.add_argument("--memories", type=int, default=10, help="memory rows per tier to include")
    parser.add_argument("--prompt-preview", type=int, default=500, help="retrieved prompt preview characters")
    parser.add_argument("--message-preview", type=int, default=500, help="retrieval situation preview characters")
    ns = parser.parse_args(argv)

    repo = ns.repo.resolve()
    config = _read_config(repo)
    project = ns.project or config.get("project") or repo.name
    agent = ns.agent or config.get("agent_id") or f"{_clean(project)}-hook"

    recent_events = _safe(lambda: runlog.recent_events(project, limit=ns.events, repo_path=repo))

    result = {
        "repo": str(repo),
        "project_id": project,
        "agent_id": agent,
        "configured_cli": config.get("cli"),
        "current_run": _safe(lambda: _read_current_run(repo)),
        "summary": _safe(lambda: runlog.summarize(project, limit=ns.events, repo_path=repo)),
        "prompt_timeline": _prompt_timeline(recent_events, project, agent, ns.prompt_preview, ns.message_preview),
        "recent_events": recent_events,
        "memories": {
            "ephemeral": _safe(lambda: storage.get_ephemeral(project, agent, limit=ns.memories)),
            "persistent": _safe(lambda: storage.get_persistent(project, limit=ns.memories)),
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def _prompt_timeline(
    events,
    project: str,
    agent: str,
    prompt_preview_chars: int,
    message_preview_chars: int,
) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        return []

    chronological = list(reversed(events))
    turn_context = _turn_context(chronological)
    prompt_ids = [
        (event.get("payload") or {}).get("prompt_id")
        for event in events
        if event.get("event_type") == "prompt.retrieved"
    ]
    prompt_refs = _prompt_refs([p for p in prompt_ids if p], prompt_preview_chars)
    situations = _retrieval_situations(project, agent, message_preview_chars)

    timeline = []
    for event in chronological:
        event_type = event.get("event_type")
        payload = event.get("payload") or {}
        if event_type == "prompt.gate.decided":
            reason = str(payload.get("reason") or "")
            timeline.append({
                "kind": "gate_decision",
                "created_at": event.get("created_at"),
                "run_id": event.get("run_id"),
                "turn_id": event.get("turn_id"),
                "search_opened": payload.get("search"),
                "gate_nearest_match_title": _nearest_match(reason),
                "reason": reason,
            })
        elif event_type == "prompt.retrieved":
            prompt_id = payload.get("prompt_id")
            item = {
                "kind": "prompt_retrieved",
                "created_at": event.get("created_at"),
                "run_id": event.get("run_id"),
                "turn_id": event.get("turn_id"),
                "retrieved_prompt_id": prompt_id,
                "retrieved_prompt": prompt_refs.get(prompt_id),
                "message_context": _message_context(event.get("turn_id"), turn_context, situations.get(prompt_id)),
                "confidence": payload.get("confidence"),
                "mode": payload.get("mode"),
            }
            timeline.append(item)
    return timeline


def _turn_context(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    observed = []
    for event in events:
        if event.get("event_type") != "turn.observed" or not event.get("turn_id"):
            continue
        payload = event.get("payload") or {}
        observed.append({
            "turn_id": event.get("turn_id"),
            "created_at": event.get("created_at"),
            "message_chars": payload.get("message_chars"),
            "message_preview": payload.get("message_preview"),
            "message_preview_truncated": payload.get("message_preview_truncated"),
            "message_sha256": payload.get("message_sha256"),
        })

    by_turn = {}
    for idx, item in enumerate(observed):
        by_turn[item["turn_id"]] = {
            "previous_observed_user_turn": observed[idx - 1] if idx > 0 else None,
            "invocation_user_turn": item,
            "next_observed_user_turn": observed[idx + 1] if idx + 1 < len(observed) else None,
        }
    return by_turn


def _message_context(turn_id: str | None, turn_context: dict[str, dict[str, Any]], situation: dict[str, Any] | None) -> dict[str, Any]:
    context = turn_context.get(turn_id or "") or {
        "previous_observed_user_turn": None,
        "invocation_user_turn": None,
        "next_observed_user_turn": None,
    }
    if situation:
        invocation = dict(context.get("invocation_user_turn") or {})
        invocation["retrieval_situation_preview"] = situation.get("situation_preview")
        invocation["retrieval_situation_truncated"] = situation.get("situation_truncated")
        invocation["retrieval_situation_chars"] = situation.get("situation_chars")
        context = {**context, "invocation_user_turn": invocation}
    return context


def _prompt_refs(prompt_ids: list[str], preview_chars: int) -> dict[str, dict[str, Any]]:
    if not prompt_ids:
        return {}
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT title, prompt_id, file_path, content_hash, status, source,
                       length(prompt_text) AS prompt_chars,
                       left(prompt_text, %s) AS prompt_preview
                FROM prompts
                WHERE prompt_id::text = ANY(%s)
                """,
                (preview_chars, list(dict.fromkeys(prompt_ids))),
            )
            return {str(row["prompt_id"]): dict(row) for row in cur.fetchall()}
    except Exception as exc:
        return {prompt_id: {"error": str(exc)} for prompt_id in prompt_ids}


def _retrieval_situations(project: str, agent: str, preview_chars: int) -> dict[str, dict[str, Any]]:
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (prompt_id)
                       prompt_id,
                       left(situation, %s) AS situation_preview,
                       length(situation) AS situation_chars,
                       length(situation) > %s AS situation_truncated,
                       created_at
                FROM arteries.retrievals
                WHERE project_id = %s AND agent_process_id = %s
                ORDER BY prompt_id, created_at DESC
                """,
                (preview_chars, preview_chars, project, agent),
            )
            return {str(row["prompt_id"]): dict(row) for row in cur.fetchall()}
    except Exception:
        return {}


def _nearest_match(reason: str) -> str | None:
    match = re.search(r"closest=([^)]*)", reason)
    return match.group(1) if match else None


def _read_config(repo: Path) -> dict[str, Any]:
    path = repo / ".arteries" / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_current_run(repo: Path) -> dict[str, Any] | None:
    path = repo / ".arteries" / "current-run.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _clean(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value.lower())


def _safe(fn):
    try:
        return fn()
    except Exception as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    raise SystemExit(main())
