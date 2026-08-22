"""Short CLI entry point for arteries: `art`."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from arteries import doctor, docs, graph, inspect, ontology, packet, remember, runs, scope, setup_cli, trace
from arteries.eval import evaluate


COMMANDS = ("setup", "docs", "ontology", "scope", "graph", "identity", "eval", "inspect", "runs", "doctor", "packet", "trace", "decisions", "ingest", "remember", "spawn", "search", "compile")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="art",
        description="Arteries CLI shortcut.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=COMMANDS,
        help="command to run: " + ", ".join(COMMANDS),
    )
    parser.add_argument("args", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)

    if ns.command is None:
        parser.print_help()
        return 0

    if ns.command == "setup":
        return setup_cli.main(ns.args)
    if ns.command == "docs":
        return docs.main(ns.args)

    if ns.command == "ontology":
        return ontology.main(ns.args)

    if ns.command == "scope":
        return scope.main(ns.args)

    if ns.command == "graph":
        return graph.main(ns.args)
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
    if ns.command == "remember":
        return remember.main(ns.args)
    if ns.command == "identity":
        return _identity(ns.args)

    if ns.command == "spawn":
        return _spawn(list(ns.args))
    if ns.command == "search":
        return _search(ns.args)
    if ns.command == "compile":
        return _compile(ns.args)

    parser.error(f"unknown command: {ns.command}")
    return 2


def _spawn(args: list[str]) -> int:
    """Run a child agent command with subagent memory attribution.

    The child writes ephemeral tagged with this agent as parent; the parent's
    compilation pass claims those records and applies the [SUBAGENT] bar.
    """
    import os
    import sys

    if args and args[0] in ("-h", "--help"):
        print(_spawn.__doc__.strip())
        print("\nusage: art spawn -- <command> [args...]")
        return 0
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        print("usage: art spawn -- <command> [args...]", file=sys.stderr)
        return 2

    from arteries.config import AGENT_PROCESS_ID
    from arteries.subagent import subagent_env

    env = {**os.environ, **subagent_env(AGENT_PROCESS_ID)}
    env.setdefault("ARTERIES_MEMORY", "subagent")
    os.execvpe(args[0], args, env)


def _identity(args: Sequence[str]) -> int:
    """Mint a subagent memory identity for an orchestrator to spawn with.

    Arteries should not start processes -- heart orchestrates. What arteries
    owns is memory attribution: a child needs its own agent id tagged with this
    one as parent, so the parent's compile pass claims the child's ephemeral and
    applies the [SUBAGENT] bar. This prints that environment and stops.
    """
    p = argparse.ArgumentParser(
        prog="art identity",
        description="Print the environment a subagent should be spawned with.",
    )
    p.add_argument("--parent", default=None, help="parent agent id (default: this one)")
    p.add_argument("--role", default="subagent")
    p.add_argument("--json", action="store_true", dest="as_json")
    ns = p.parse_args(args)

    import json as _json

    from arteries.config import AGENT_PROCESS_ID
    from arteries.subagent import subagent_env

    env = subagent_env(ns.parent or AGENT_PROCESS_ID)
    env.setdefault("ARTERIES_MEMORY", "subagent")
    env.setdefault("ARTERIES_AGENT_ROLE", ns.role)

    if ns.as_json:
        print(_json.dumps(env, indent=2, sort_keys=True))
    else:
        for k, v in sorted(env.items()):
            print(f"export {k}={v}")
    return 0


def _compile(args: list[str]) -> int:
    """Run one compilation pass now, for the agent in ARTERIES_AGENT_ID. An
    orchestrator uses this to flush its subagents' ephemeral up to project memory
    after they exit (their own async compile never runs in a one-shot child)."""
    from arteries.compile import compile_once
    print(asyncio.run(compile_once()))
    return 0


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
        description="Ingest episode rewards. Reads JSONL from stdin by default.",
        epilog="marrow emits these; `marrow export | art ingest` is the intended shape.",
    )
    p.add_argument("path", nargs="?",
                   help="JSONL file or runs directory; omit to read stdin")
    ns = p.parse_args(args)

    from arteries import actionlog

    n = actionlog.ingest_episodes(ns.path)
    print(f"ingested {n} episode rewards")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
