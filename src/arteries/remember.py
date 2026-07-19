"""Explicit model/user-driven memory: art remember.

Writes directly to persistent (project-scoped) and optionally evergreen
(cross-project), bypassing the extraction heuristics. For facts the
extractor would miss — preferences, decisions, constraints stated by the
user or model.

    art remember "I prefer tabs over spaces"
    art remember "I prefer tabs over spaces" --also-evergreen
    art remember list
    art remember edit <id> --fact "updated text"
    art remember rm <id>
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from arteries import storage
from arteries.config import PROJECT_ID
from arteries.evergreen import _infer_domains


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="art remember", description="Explicit memory write.")
    sub = parser.add_subparsers(dest="action")

    add_p = sub.add_parser("add", help="store a memory")
    add_p.add_argument("fact", nargs="+")
    add_p.add_argument("--domains", default="")
    add_p.add_argument("--confidence", type=float, default=1.0)
    add_p.add_argument("--also-evergreen", action="store_true", help="promote to evergreen too")

    list_p = sub.add_parser("list", help="list user-written persistent memories")
    list_p.add_argument("--limit", type=int, default=50)
    list_p.add_argument("--json", action="store_true", dest="as_json")

    edit_p = sub.add_parser("edit", help="edit a persistent memory")
    edit_p.add_argument("id")
    edit_p.add_argument("--fact")
    edit_p.add_argument("--domains")
    edit_p.add_argument("--confidence", type=float)

    rm_p = sub.add_parser("rm", help="soft-delete a persistent memory")
    rm_p.add_argument("id")

    if argv and argv[0] not in ("add", "list", "edit", "rm", "-h", "--help"):
        return _add(argv, parser)

    args = parser.parse_args(argv)

    if args.action is None:
        parser.print_help()
        return 0

    if args.action == "add":
        return _do_add(args)
    if args.action == "list":
        return _do_list(args)
    if args.action == "edit":
        return _do_edit(args)
    if args.action == "rm":
        return _do_rm(args)
    return 2


def _add(argv: Sequence[str], parser: argparse.ArgumentParser) -> int:
    p = argparse.ArgumentParser(prog="art remember")
    p.add_argument("fact", nargs="+")
    p.add_argument("--domains", default="")
    p.add_argument("--confidence", type=float, default=1.0)
    p.add_argument("--also-evergreen", action="store_true")
    args = p.parse_args(argv)
    return _do_add(args)


def _do_add(args) -> int:
    fact = " ".join(args.fact)
    domains = [d.strip() for d in args.domains.split(",") if d.strip()] if args.domains else _infer_domains(fact)
    embedding = _embed(fact)

    pid = storage.insert_persistent(
        project_id=PROJECT_ID,
        fact=fact,
        domains=domains,
        confidence=args.confidence,
        scope="user",
        embedding=embedding,
    )
    print(f"persistent: {pid[:8]}  {fact}")

    if args.also_evergreen:
        eid = storage.insert_evergreen(
            fact=fact,
            domains=domains,
            confidence=args.confidence,
            embedding=embedding,
            source_meta={"source_type": "user_remember", "persistent_id": pid},
        )
        print(f"evergreen:  {eid[:8]}  (promoted)")

    return 0


def _do_list(args) -> int:
    rows = storage.get_persistent(PROJECT_ID, limit=args.limit, scope="user")
    if args.as_json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("No user-written persistent memories.")
        return 0
    for r in rows:
        domains = ", ".join(r.get("domains") or [])
        rid = str(r["id"])[:8]
        print(f"  {rid}  [{domains}]  {r['fact']}")
    return 0


def _do_edit(args) -> int:
    resolved = _resolve_id(args.id)
    if not resolved:
        return 1
    domains = [d.strip() for d in args.domains.split(",") if d.strip()] if args.domains else None
    ok = storage.update_persistent(resolved, PROJECT_ID, fact=args.fact, domains=domains, confidence=args.confidence)
    if ok:
        if args.fact:
            vec = _embed(args.fact)
            if vec:
                _update_embedding(resolved, vec)
        print(f"Updated: {resolved[:8]}")
    else:
        print(f"Not found: {args.id}")
        return 1
    return 0


def _do_rm(args) -> int:
    resolved = _resolve_id(args.id)
    if not resolved:
        return 1
    ok = storage.remove_persistent(resolved, PROJECT_ID)
    if ok:
        print(f"Removed: {resolved[:8]}")
    else:
        print(f"Not found: {args.id}")
        return 1
    return 0


def _resolve_id(prefix: str) -> str | None:
    rows = storage.get_persistent(PROJECT_ID, limit=500, scope="user")
    matches = [str(r["id"]) for r in rows if str(r["id"]).startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f"No memory matching: {prefix}")
        return None
    print(f"Ambiguous prefix '{prefix}', matches: {', '.join(m[:12] for m in matches)}")
    return None


def _embed(text: str) -> list[float] | None:
    try:
        from arteries.embed import embed_text_sync
        return embed_text_sync(text)
    except Exception:
        return None


def _update_embedding(persistent_id: str, vec: list[float]) -> None:
    import psycopg2
    from arteries.config import DB_CONFIG
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE arteries.persistent SET embedding = %s::vector WHERE id = %s",
            (vec, persistent_id),
        )
    conn.commit()
    conn.close()
