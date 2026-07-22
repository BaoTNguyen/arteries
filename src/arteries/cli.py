"""Short CLI entry point for arteries: `art`."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from arteries import doctor, evergreen, inspect, packet, remember, runs, setup_cli, setup_db, trace
from arteries.eval import evaluate


COMMANDS = ("setup", "evergreen", "setup-db", "eval", "inspect", "runs", "doctor", "packet", "trace", "decisions", "ingest", "backfill-embeddings", "remember", "spawn", "search")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="art",
        description="Arteries CLI shortcut.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=COMMANDS,
        help="command to run: setup, evergreen, setup-db, eval, inspect, runs, doctor, packet, trace",
    )
    parser.add_argument("args", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)

    if ns.command is None:
        parser.print_help()
        return 0

    if ns.command == "setup":
        return setup_cli.main(ns.args)
    if ns.command == "evergreen":
        return evergreen.main(ns.args)
    if ns.command == "setup-db":
        setup_db.setup()
        return 0
    if ns.command == "eval":
        if not ns.args:
            parser.error("eval requires a prompt")
        prompt = " ".join(ns.args)
        result = asyncio.run(evaluate(prompt))
        if result:
            print(result)
        return 0
    if ns.command == "inspect":
        return inspect.main(ns.args)
    if ns.command == "runs":
        return runs.main(ns.args)
    if ns.command == "doctor":
        return doctor.main(ns.args)
    if ns.command == "packet":
        return packet.main(list(ns.args))
    if ns.command == "trace":
        return trace.main(ns.args)
    if ns.command == "decisions":
        return _decisions(ns.args)
    if ns.command == "ingest":
        return _ingest(ns.args)
    if ns.command == "backfill-embeddings":
        return _backfill_embeddings(ns.args)
    if ns.command == "remember":
        return remember.main(ns.args)
    if ns.command == "spawn":
        return _spawn(list(ns.args))
    if ns.command == "search":
        return _search(ns.args)

    parser.error(f"unknown command: {ns.command}")
    return 2


def _spawn(args: list[str]) -> int:
    """Run a child agent command with subagent memory attribution.

    The child writes ephemeral tagged with this agent as parent; the parent's
    compilation pass claims those records and applies the [SUBAGENT] bar.
    """
    import os
    import sys
    import uuid

    if args and args[0] == "--":
        args = args[1:]
    if not args:
        print("usage: art spawn -- <command> [args...]", file=sys.stderr)
        return 2

    from arteries.config import AGENT_PROCESS_ID

    env = os.environ.copy()
    env["ARTERIES_PARENT_AGENT_ID"] = AGENT_PROCESS_ID
    env["ARTERIES_AGENT_ID"] = f"{AGENT_PROCESS_ID}-sub-{uuid.uuid4().hex[:8]}"
    env["ARTERIES_AGENT_ROLE"] = "subagent"
    env.setdefault("ARTERIES_MEMORY", "subagent")
    os.execvpe(args[0], args, env)


def _search(args: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="art search", description="Full-text search over observed turns.")
    p.add_argument("query", nargs="+")
    p.add_argument("--project", default=None)
    p.add_argument("--limit", type=int, default=15)
    ns = p.parse_args(args)
    query = " ".join(ns.query)

    import psycopg2
    import psycopg2.extras

    from arteries.config import DB_CONFIG, PROJECT_ID

    project = ns.project or PROJECT_ID
    tsv = ("to_tsvector('english', coalesce(payload->>'message_preview','') || ' ' || "
           "coalesce(payload->>'assistant_preview',''))")
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT created_at, event_type,
                   coalesce(payload->>'message_preview', payload->>'assistant_preview') AS text,
                   ts_rank({tsv}, websearch_to_tsquery('english', %s)) AS rank
            FROM arteries.agent_events
            WHERE project_id = %s
              AND {tsv} @@ websearch_to_tsquery('english', %s)
            ORDER BY rank DESC, created_at DESC
            LIMIT %s
            """,
            (query, project, query, ns.limit),
        )
        rows = cur.fetchall()

    if not rows:
        print("no matches")
        return 0
    for r in rows:
        role = "assistant" if r["event_type"] == "assistant.response" else "user"
        text = " ".join((r["text"] or "").split())
        if len(text) > 160:
            text = text[:160] + "…"
        print(f"{str(r['created_at'])[:19]}  {role:<9} {text}")
    return 0


def _decisions(args: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="art decisions", description="Inspect the decision ledger.")
    p.add_argument("--project", default=None)
    p.add_argument("--episode", default=None, help="filter by episode id")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--json", action="store_true")
    ns = p.parse_args(args)

    import json as _json

    from arteries import actionlog

    rows = actionlog.recent_decisions(project_id=ns.project, episode=ns.episode, limit=ns.limit)
    if ns.json:
        print(_json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("no decisions recorded")
        return 0
    for r in rows:
        created = str(r.get("created_at", ""))[:19]
        ep = r.get("episode_id") or "-"
        print(f"{created}  {r['decision_type']:<22} {r['chosen_action']:<26} ep={ep}")
    return 0


def _ingest(args: Sequence[str]) -> int:
    p = argparse.ArgumentParser(
        prog="art ingest",
        description="Backfill episode rewards from heart runs dir or episodes.jsonl.",
    )
    p.add_argument("path", help="heart runs directory or episodes.jsonl export")
    ns = p.parse_args(args)

    from arteries import actionlog

    n = actionlog.ingest_heart_episodes(ns.path)
    print(f"ingested {n} episode rewards")
    return 0


def _backfill_embeddings(args: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="art backfill-embeddings")
    p.add_argument("--project", required=True)
    p.add_argument("--batch", type=int, default=50)
    ns = p.parse_args(args)

    from arteries.embed import embed_text_sync
    from arteries import storage

    import psycopg2.extras
    from arteries.config import DB_CONFIG

    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, fact FROM arteries.persistent
            WHERE project_id = %s AND valid_until IS NULL AND embedding IS NULL
            LIMIT %s
            """,
            (ns.project, ns.batch),
        )
        rows = cur.fetchall()

    updated = 0
    for r in rows:
        vec = embed_text_sync(r["fact"])
        if vec:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE arteries.persistent SET embedding = %s::vector WHERE id = %s",
                    (vec, r["id"]),
                )
                conn.commit()
            updated += 1
    conn.close()
    print(f"Backfilled {updated}/{len(rows)} persistent memories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
