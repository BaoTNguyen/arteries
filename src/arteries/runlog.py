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
from arteries.journal import journal_append

RUN_ENV_KEYS = ("AGENT_RUN_ID", "ARTERIES_RUN_ID")

# How long a run may sit idle before the sweep closes it.
#
# Two populations, two answers. Heart brackets its own work and emits
# episode.finished, so an episode run that goes quiet for an hour has crashed
# or been killed -- closing it is a mercy. An hour rather than thirty minutes
# because a real episode can spend that long inside one verify-and-fix round
# without emitting anything, and closing a live run is worse than closing a
# dead one late.
#
# An interactive window is the opposite: you leave it open Friday and come back
# Monday, and that is not a fault. 48 hours is deliberately generous, because a
# closed run costs nothing but a join -- session_id still stitches the
# conversation across it.
#
# Both are env-overridable: the tolerance is the orchestrator's judgement, not
# arteries'. Heart already builds the child environment where it would set one.
IDLE_EPISODE = os.getenv("ARTERIES_IDLE_EPISODE", "1 hour")
IDLE_INTERACTIVE = os.getenv("ARTERIES_IDLE_INTERACTIVE", "48 hours")

# One process resolves its run once. current_run() is called from every
# log_event, and the session lookup is a database round trip; hook processes are
# short-lived, so this turns one query per event into one query per hook.
_resolved: dict[tuple[str, str, str], dict[str, Any]] = {}

# Mirrors ARTERIES_EPHEMERAL=discard. The smoke script advertised itself as a
# dry run while writing turn.observed rows into the live store and rewriting
# the current-run pointer, which handed a real session's turns to a run created
# by a test. A dry run has to be dry on both sides of the ledger.
def _discard() -> bool:
    return os.getenv("ARTERIES_RUNLOG") == "discard"



def new_turn_id() -> str:
    return str(uuid.uuid4())


def start_run(
    project_id: str | None = None,
    agent_id: str | None = None,
    cli: str | None = None,
    repo_path: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Begin this session's run, or rejoin the one it already has.

    Called from every CLI's session-start hook, which fires on resume as well as
    on a fresh start -- so minting unconditionally is how one repo accumulated
    18 Claude runs. The sweep rides here too: session start is the moment a
    stale run could be joined by mistake, and the only moment that matters.
    """
    sweep_runs()
    repo = _repo(repo_path)

    if force:
        run = _run_payload(str(uuid.uuid4()), _project(project_id, repo), _agent(agent_id),
                           _cli(cli), repo, metadata=_run_metadata())
        _write_current_run(repo, run)
        _resolved.clear()
    else:
        run = current_run(project_id, agent_id, cli, repo_path)

    # Session start is where a run becomes real, so later processes can find it
    # by session and resume rather than minting a fresh id every time.
    existed = _run_exists(run["run_id"])
    _persist_run(run)

    log_event(
        "run.resumed" if existed else "run.started",
        "arteries",
        {},
        project_id=run["project_id"],
        run_id=run["run_id"],
        agent_id=run["agent_id"],
        cli=run["cli"],
        repo_path=repo,
    )
    return run


def _persist_run(run: dict[str, Any]) -> None:
    """Insert on creation so the next process can find this run. Best-effort:
    an unwritable run is still usable here, and log_event's JSONL fallback keeps
    the events either way."""
    if _discard():
        return
    _db(lambda cur: _write_db_run(run), None)


def _run_exists(run_id: str) -> bool:
    def work(cur):
        cur.execute("SELECT 1 FROM arteries.agent_runs WHERE id = %s", (run_id,))
        return cur.fetchone() is not None

    return _db(work, False)


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

    # Step 1: an assigned id wins outright. Heart mints one per subagent
    # session and hands it down, so two concurrent episodes cannot collide on
    # any shared file -- they never reach one.
    run_id = _env_run_id()
    if run_id:
        return _run_payload(run_id, project, agent, cli_name, repo,
                            metadata=_run_metadata())

    session = os.getenv("ARTERIES_SESSION_ID") or ""
    cache_key = (session, cli_name, str(repo))
    if cache_key in _resolved:
        cached = _resolved[cache_key]
        # The cache spares the database round trip, not the pointer file. A
        # sessionless CLI still reads that file from other processes, so a
        # missing one is repaired here rather than only on the cold path.
        if not session and not _current_run_path(repo, cli_name).exists():
            _write_current_run(repo, cached)
        return cached

    # Step 2: the session's own open run. This is what "resume yesterday's
    # session" was always meant to mean; keying by CLI could only ever say
    # "whatever ran last", which is how one run swallowed six days of work.
    if session:
        found = _session_run(session, open_only=True)
        if found:
            run = _run_payload(str(found["id"]), project, agent, cli_name, repo,
                               started_at=str(found["started_at"]),
                               metadata=dict(found.get("metadata") or {}))
            _write_current_run(repo, run)
            _resolved[cache_key] = run
            return run

    # Step 3 is for CLIs that report no session at all. A CLI that *did* report
    # one and found no open run of its own must fall through to a new run: the
    # per-CLI pointer names whatever ran last, so honouring it here would hand a
    # fresh session the previous session's run -- the exact bleed step 2 exists
    # to stop.
    #
    # Runs are keyed by CLI, not just by repo. A single repo is worked by several
    # CLIs, and a shared current-run file meant whoever called `runs start` last
    # owned every subsequent turn — a Claude turn landing on a Codex run, priced
    # against the wrong rate card. Each CLI now resumes its own run.
    for candidate in ((_current_run_path(repo, cli_name), repo / ".arteries" / "current-run.json")
                      if not session else ()):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not data.get("run_id"):
            continue
        if candidate.name == "current-run.json":
            file_cli = _cli(data.get("cli"))
            if cli_name == "unknown":
                # nothing was declared, so there is no identity to keep separate:
                # adopt the open run rather than forking an "unknown" one
                cli_name = file_cli
            elif file_cli != cli_name:
                # a named CLI never inherits another's run — that is the bug
                continue
        run = _run_payload(
            str(data["run_id"]),
            project_id or os.getenv("ARTERIES_PROJECT") or data.get("project_id") or PROJECT_ID or repo.name,
            agent_id or os.getenv("ARTERIES_AGENT_ID") or data.get("agent_id") or AGENT_PROCESS_ID,
            cli_name,
            repo,
            started_at=data.get("started_at"),
        )
        # Step 3: the per-CLI pointer, for CLIs that report no session. Only
        # honoured while the run is still open -- an ended run is history, and
        # joining it is what made runs immortal.
        if not _run_is_open(run["run_id"]):
            continue
        if candidate.name == "current-run.json":
            _write_current_run(repo, run)  # migrate it under the per-CLI key
        _resolved[cache_key] = run
        return run

    # Step 4: a new run. A session whose previous run was swept records it, so
    # one conversation stays walkable across the runs it spans.
    previous = _session_run(session, open_only=False) if session else None
    run = _run_payload(
        str(uuid.uuid4()), project, agent, cli_name, repo,
        metadata=_run_metadata(previous_run_id=str(previous["id"]) if previous else None),
    )
    _write_current_run(repo, run)
    # Deliberately not persisted here. Resolving a run must stay a read: this
    # function is called from every log_event, and writing a row on resolution
    # meant merely *asking* which run you were in created one -- a test that
    # resolved without logging left rows in the live store. start_run persists
    # at session start, and log_event's _write_db_run persists on first event,
    # so a run nobody began and nobody wrote to correctly does not exist.
    _resolved[cache_key] = run
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
    for key, env_key in (("episode_id", "ARTERIES_EPISODE_ID"), ("task_id", "ARTERIES_TASK_ID"),
                         ("session_id", "ARTERIES_SESSION_ID")):
        value = os.getenv(env_key)
        if value:
            event["payload"].setdefault(key, value)
    if _discard():
        return event
    store = "db"
    try:
        _write_db_run(run)
        _write_db_event(event)
    except Exception:
        _write_jsonl(run, event)
        store = "jsonl"  # degradation signal: pulse health watches this
    # journal_append's own parameters are source/kind/turn_id, and the payload is
    # spread into its **kwargs -- so a payload key with any of those names
    # raises "got multiple values for argument". Callers should not have to know
    # that, so the collision is resolved here by prefixing rather than dropping:
    # losing a field silently would be worse than renaming it.
    _RESERVED = {"source", "kind", "turn_id", "store", "ts",
                 "project_id", "repo", "cli", "agent_id", "event_id", "run_id"}
    journaled = {}
    for k, v in event["payload"].items():
        if k in ("episode_id", "task_id"):
            continue
        journaled[f"payload_{k}" if k in _RESERVED else k] = v
    journal_append(
        source, event_type, turn_id=turn_id, store=store,
        event_id=event["id"], run_id=run["run_id"],
        project_id=run["project_id"], repo=run["repo_path"],
        cli=run["cli"], agent_id=run["agent_id"],
        **journaled,
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


def _run_metadata(session_id: str | None = None, previous_run_id: str | None = None) -> dict[str, Any]:
    """What a run records about itself, decided once at creation.

    `kind` is stamped rather than derived so the sweep does not have to join
    events to know which threshold applies. A run is an episode when heart
    assigned it -- heart is the only thing that sets ARTERIES_EPISODE_ID.
    """
    metadata: dict[str, Any] = {
        "kind": "episode" if os.getenv("ARTERIES_EPISODE_ID") else "interactive",
    }
    session = session_id or os.getenv("ARTERIES_SESSION_ID")
    if session:
        metadata["session_id"] = session
    if previous_run_id:
        # the same session, resumed after its previous run was closed. Lets a
        # trace walk one conversation across runs without a group-by.
        metadata["previous_run_id"] = previous_run_id
    return metadata


def _db(work, default, dict_rows: bool = False):
    """Run `work(cursor)`, returning `default` if anything at all fails.

    Every run-lifecycle lookup is best-effort by contract -- telemetry must
    never fail a turn, and log_event's JSONL fallback already catches lost
    events. Swallowing in one place means one place to change if that ever
    stops being true, instead of six identical bare handlers.
    """
    factory = {"cursor_factory": psycopg2.extras.RealDictCursor} if dict_rows else {}
    try:
        with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor(**factory) as cur:
            return work(cur)
    except Exception:
        return default


def _session_run(session_id: str, open_only: bool = True) -> dict[str, Any] | None:
    """Newest run belonging to this session, or None. Never raises."""
    clause = "AND ended_at IS NULL" if open_only else ""

    def work(cur):
        cur.execute(
            f"""
            SELECT id, project_id, repo_path, cli, agent_id, started_at, metadata
            FROM arteries.agent_runs
            WHERE metadata->>'session_id' = %s {clause}
            ORDER BY started_at DESC LIMIT 1
            """,
            (session_id,),
        )
        return cur.fetchone()

    return _db(work, None, dict_rows=True)


def _run_is_open(run_id: str) -> bool:
    """False only when the database says so. A lookup that cannot answer must
    not close a run the caller is legitimately still using."""
    def work(cur):
        cur.execute("SELECT ended_at FROM arteries.agent_runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
        return row is None or row[0] is None

    return _db(work, True)


def end_run(run_id: str, reason: str = "explicit") -> bool:
    """Close a run. Idempotent, and the only way a run ever ends.

    Heart calls this at episode.finished and the sweep calls it for everything
    that never announced itself -- same write, same semantics, two triggers.
    That is the point: nothing may depend on the explicit signal arriving, so a
    killed terminal or a crashed episode costs lateness, never correctness.
    """
    def work(cur):
        cur.execute(
            """
            UPDATE arteries.agent_runs
            SET ended_at = now(),
                metadata = COALESCE(metadata, '{}'::jsonb)
                           || jsonb_build_object('end_reason', %s)
            WHERE id = %s AND ended_at IS NULL
            """,
            (reason, run_id),
        )
        return cur.rowcount > 0

    return _db(work, False)


def sweep_runs() -> list[str]:
    """Close every run idle past its threshold. Returns the ids closed.

    Runs at session start rather than at run resolution: current_run() is on the
    path of every log_event, and staleness only matters when a new session is
    about to decide what to join. A machine left off for a week sweeps clean the
    moment any CLI opens a session.
    """
    def work(cur):
        cur.execute(
            f"""
            UPDATE arteries.agent_runs r
            SET ended_at = now(),
                metadata = COALESCE(r.metadata, '{{}}'::jsonb)
                           || jsonb_build_object('end_reason', 'idle_sweep')
            WHERE r.ended_at IS NULL
              AND COALESCE(
                    (SELECT max(created_at) FROM arteries.agent_events e
                      WHERE e.run_id = r.id),
                    r.started_at
                  ) < now() - (CASE WHEN r.metadata->>'kind' = 'episode'
                                    THEN interval '{IDLE_EPISODE}'
                                    ELSE interval '{IDLE_INTERACTIVE}' END)
            RETURNING r.id
            """
        )
        return [str(row[0]) for row in cur.fetchall()]

    return _db(work, [])


def _repo(repo_path: str | Path | None = None) -> Path:
    return Path(repo_path or os.getenv("ARTERIES_REPO") or Path.cwd()).resolve()


def _project(project_id: str | None, repo: Path) -> str:
    return project_id or os.getenv("ARTERIES_PROJECT") or PROJECT_ID or repo.name


def _agent(agent_id: str | None) -> str:
    return str(agent_id or os.getenv("ARTERIES_AGENT_ID") or AGENT_PROCESS_ID)


def _cli(cli: str | None) -> str:
    return cli or os.getenv("AGENT_CLI") or os.getenv("ARTERIES_CLI") or "unknown"


def _run_payload(run_id: str, project_id: str, agent_id: str, cli: str, repo: Path,
                 started_at: str | None = None,
                 metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project_id": project_id,
        "repo_path": str(repo),
        "cli": cli,
        "agent_id": str(agent_id),
        "started_at": started_at or _now_iso(),
        "metadata": metadata if metadata is not None else {},
    }


def _cli_slug(cli: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(cli).lower()) or "unknown"


def _current_run_path(repo: Path, cli: str) -> Path:
    return repo / ".arteries" / "runs" / f"current-{_cli_slug(cli)}.json"


def _write_current_run(repo: Path, run: dict[str, Any]) -> None:
    if _discard():
        return
    blob = json.dumps(run, indent=2, sort_keys=True, default=str) + "\n"
    per_cli = _current_run_path(repo, run.get("cli") or "unknown")
    per_cli.parent.mkdir(parents=True, exist_ok=True)
    per_cli.write_text(blob, encoding="utf-8")
    # legacy pointer: whoever wrote last. Nothing routes turns through it any
    # more, but `art trace` and external readers still expect the old path.
    legacy = repo / ".arteries" / "current-run.json"
    legacy.write_text(blob, encoding="utf-8")


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
