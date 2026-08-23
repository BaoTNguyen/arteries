"""Short CLI entry point for arteries: `art`."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from arteries import doctor, evergreen, inspect, packet, remember, runs, setup_cli, setup_db, trace
from arteries.eval import evaluate


COMMANDS = ("setup", "evergreen", "setup-db", "eval", "observe", "activate", "inspect", "runs", "doctor", "packet", "trace", "decisions", "ingest", "backfill-embeddings", "remember", "spawn", "search", "compile")


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
    if ns.command == "observe":
        return _observe(ns.args)
    if ns.command == "activate":
        return _activate(ns.args)
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
    if ns.command == "compile":
        return _compile(ns.args)

    parser.error(f"unknown command: {ns.command}")
    return 2


def _cli_env(ns: argparse.Namespace) -> None:
    """Apply the identity flags shared by the host-agnostic commands.

    These are plain env vars because that is the contract every other module
    already reads (config.PROJECT_ID and friends resolve at import), so a flag
    has to be set before anything downstream is touched.
    """
    import os

    for flag, var in (("cli", "ARTERIES_CLI"), ("project", "ARTERIES_PROJECT"),
                      ("agent", "ARTERIES_AGENT_ID"), ("repo", "ARTERIES_REPO"),
                      ("transcript", "ARTERIES_TRANSCRIPT")):
        value = getattr(ns, flag, None)
        if value:
            os.environ[var] = str(value)
    for key in ("tokens_in", "tokens_out", "cache_read", "cache_write_5m", "cache_write_1h"):
        value = getattr(ns, key, None)
        if value:
            os.environ[f"ARTERIES_USAGE_{key.upper()}"] = str(value)


def _identity_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cli", default=None, help="host label for attribution; anything goes")
    p.add_argument("--project", default=None)
    p.add_argument("--agent", default=None)
    p.add_argument("--repo", default=None)


def _observe(args: Sequence[str]) -> int:
    """Observe one turn from any host. Prompt on argv or stdin, retrieval on stdout.

    The host-agnostic sibling of the per-CLI hook wrappers: those exist only to
    normalise each vendor's event JSON, and a CLI that can shell out does not
    need that layer. Prints nothing when the gate abstains, so it is safe to
    splice into a prompt unconditionally.
    """
    import sys

    p = argparse.ArgumentParser(prog="art observe", description="Observe one turn from any CLI.")
    p.add_argument("prompt", nargs="*", help="prompt text; read from stdin when omitted")
    _identity_args(p)
    p.add_argument("--transcript", default=None, help="session transcript path, if the host has one")
    for key in ("tokens-in", "tokens-out", "cache-read", "cache-write-5m", "cache-write-1h"):
        p.add_argument(f"--{key}", type=int, default=None, help="usage the host already measured")
    ns = p.parse_args(args)
    _cli_env(ns)

    prompt = " ".join(ns.prompt).strip() if ns.prompt else ("" if sys.stdin.isatty() else sys.stdin.read().strip())
    if not prompt:
        return 0
    result = asyncio.run(evaluate(prompt))
    if result:
        print(result)
    return 0


def _activate(args: Sequence[str]) -> int:
    """Session-start context for any host: opens a run, prints evergreen memory."""
    p = argparse.ArgumentParser(prog="art activate", description="Session-start context for any CLI.")
    _identity_args(p)
    ns = p.parse_args(args)
    _cli_env(ns)

    import os

    from arteries.config import AGENT_PROCESS_ID, PROJECT_ID

    project = os.environ.get("ARTERIES_PROJECT", PROJECT_ID)
    agent = os.environ.get("ARTERIES_AGENT_ID", AGENT_PROCESS_ID)
    # the run id goes to stdout, and stdout here is context the host will show
    import contextlib
    import io

    with contextlib.suppress(Exception), contextlib.redirect_stdout(io.StringIO()):
        runs.main(["start", "--project", project, "--agent", agent,
                   "--cli", os.environ.get("ARTERIES_CLI", "generic"),
                   "--repo", os.environ.get("ARTERIES_REPO", os.getcwd())])

    print("ARTERIES MEMORY SYSTEM ACTIVE.\n")
    print(f"This repo is connected to arteries project `{project}`.")
    print("Arteries observes turns, builds ephemeral/persistent/evergreen memory, "
          "and may surface retrieved prompts as visible context.")

    # never let a memory read stop a session from starting
    try:
        from arteries import storage
        rows = storage.get_evergreen(limit=8)
    except Exception:
        rows = []
    if rows:
        print("\nEvergreen preferences (authoritative source: arteries):")
        for r in rows:
            print(f"- {r['fact']}")
    return 0


def _spawn(args: list[str]) -> int:
    """Run a child agent command with subagent memory attribution.

    The child writes ephemeral tagged with this agent as parent; the parent's
    compilation pass claims those records and applies the [SUBAGENT] bar.
    """
    import os
    import sys

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
