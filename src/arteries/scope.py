"""Which repos share a memory, and which are watched at all.

A **scope** is a set of projects that read each other's persistent memory — a
harness of five repos behaves as one brain, a standalone project as its own.
`scope_members` is the only place scope is stored; reads resolve it with a CTE
so regrouping a repo is one UPDATE and no row can disagree with its group.

Tracking resolves by **longest matching repo_path**, not by project id, so
subdirectories inherit: an untracked project is untracked everywhere beneath it,
and working inside `src/arteries/` of a tracked repo is still that repo. This
also fixes an identity bug — project ids default to `cwd.name`
(`setup_cli.py:79`), so a hook fired from a subdirectory reported the wrong
project.

    art scope add harness ~/Projects/arteries ~/Projects/capillaries
    art scope show
    art scope move marrow harness
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import psycopg2
import psycopg2.extras

from arteries.config import DB_CONFIG

logger = logging.getLogger(__name__)

# Every scope-aware read expands the caller's project into its group. The UNION
# arm is the fallback for an unregistered project: it reads its own memory and
# nothing else, which is what arteries did before scopes existed. Opt-in is
# enforced on the *write* path, not by making an unconfigured install look
# broken.
SCOPE_CTE = """
WITH scope AS (
    SELECT m2.project_id
    FROM arteries.scope_members m1
    JOIN arteries.scope_members m2 USING (scope_id)
    WHERE m1.project_id = %(project)s
    UNION
    SELECT %(project)s
    WHERE NOT EXISTS (
        SELECT 1 FROM arteries.scope_members WHERE project_id = %(project)s
    )
)
"""


@dataclass(frozen=True)
class Member:
    project_id: str
    scope_id: str
    repo_path: Path


def _query(sql: str, params: tuple | dict = (), *, db_config: dict | None = None):
    """Run a scope query, returning [] rather than raising.

    Scope is configuration. A missing table or an unreachable database means
    "not configured", never a failed turn — the same contract as
    `ontology._lookup`.
    """
    conn = None
    try:
        conn = psycopg2.connect(**(db_config or DB_CONFIG))
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.debug("scope lookup unavailable; treating as unconfigured", exc_info=True)
        return []
    finally:
        if conn is not None:
            conn.close()


def members(scope_id: str | None = None, *, db_config: dict | None = None) -> list[Member]:
    sql = "SELECT project_id, scope_id, repo_path FROM arteries.scope_members"
    params: tuple = ()
    if scope_id:
        sql += " WHERE scope_id = %s"
        params = (scope_id,)
    sql += " ORDER BY scope_id, project_id"
    return [
        Member(r["project_id"], r["scope_id"], Path(r["repo_path"]))
        for r in _query(sql, params, db_config=db_config)
    ]


def resolve(cwd: str | Path | None = None, *, db_config: dict | None = None) -> Member | None:
    """The tracked repo containing `cwd`, or None if nothing covers it."""
    path = Path(cwd or os.environ.get("ARTERIES_EVENT_CWD") or os.getcwd()).resolve()
    matches = [
        m for m in members(db_config=db_config)
        # Component-wise, never string prefix: ".../arteries" is a string prefix
        # of ".../arteries-rework", which is a different repo.
        if path == m.repo_path or path.is_relative_to(m.repo_path)
    ]
    if not matches:
        return None
    # Longest match wins, so a tracked repo nested inside another resolves to
    # the inner one.
    return max(matches, key=lambda m: len(m.repo_path.parts))


def current_project(*, db_config: dict | None = None) -> str:
    """The project for the working directory, for commands run by hand.

    Hooks set ARTERIES_PROJECT; a human typing `art graph entities` does not, and
    config.PROJECT_ID would fall back to "default". Path resolution already knows
    the answer, so use it and keep the env var as the override.
    """
    from arteries.config import PROJECT_ID
    if os.environ.get("ARTERIES_PROJECT"):
        return os.environ["ARTERIES_PROJECT"]
    m = resolve(db_config=db_config)
    return m.project_id if m else PROJECT_ID


def is_tracked(cwd: str | Path | None = None, *, db_config: dict | None = None) -> bool:
    return resolve(cwd, db_config=db_config) is not None


def scope_for(project_id: str, *, db_config: dict | None = None) -> str | None:
    rows = _query(
        "SELECT scope_id FROM arteries.scope_members WHERE project_id = %s",
        (project_id,), db_config=db_config,
    )
    return rows[0]["scope_id"] if rows else None


def sibling_projects(project_id: str, *, db_config: dict | None = None) -> list[str]:
    """Projects sharing this one's scope, including itself.

    Only for callers that cannot use SCOPE_CTE inline.
    """
    rows = _query(
        SCOPE_CTE + "SELECT project_id FROM scope ORDER BY project_id",
        {"project": project_id}, db_config=db_config,
    )
    return [r["project_id"] for r in rows]


# -- mutation ------------------------------------------------------------------

def _execute(sql: str, params: tuple, *, db_config: dict | None = None) -> int:
    conn = psycopg2.connect(**(db_config or DB_CONFIG))
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount
    finally:
        conn.close()


def add(scope_id: str, repo_paths: list[str], *, db_config: dict | None = None) -> list[Member]:
    _execute("INSERT INTO arteries.scopes (scope_id) VALUES (%s) ON CONFLICT DO NOTHING",
             (scope_id,), db_config=db_config)
    added: list[Member] = []
    for raw in repo_paths:
        path = Path(raw).expanduser().resolve()
        project_id = path.name
        _execute(
            """
            INSERT INTO arteries.scope_members (project_id, scope_id, repo_path)
            VALUES (%s, %s, %s)
            ON CONFLICT (project_id) DO UPDATE
                SET scope_id = EXCLUDED.scope_id, repo_path = EXCLUDED.repo_path
            """,
            (project_id, scope_id, str(path)), db_config=db_config,
        )
        added.append(Member(project_id, scope_id, path))
    return added


def move(project_id: str, scope_id: str, *, db_config: dict | None = None) -> bool:
    _execute("INSERT INTO arteries.scopes (scope_id) VALUES (%s) ON CONFLICT DO NOTHING",
             (scope_id,), db_config=db_config)
    return _execute(
        "UPDATE arteries.scope_members SET scope_id = %s WHERE project_id = %s",
        (scope_id, project_id), db_config=db_config,
    ) > 0


def remove(project_id: str, *, db_config: dict | None = None) -> bool:
    return _execute("DELETE FROM arteries.scope_members WHERE project_id = %s",
                    (project_id,), db_config=db_config) > 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="art scope", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="track repos as one shared-memory group")
    p_add.add_argument("scope_id")
    p_add.add_argument("paths", nargs="+", help="repo paths (project id = directory name)")

    sub.add_parser("list", help="all scopes and their members")
    sub.add_parser("show", help="which scope covers the current directory")

    p_move = sub.add_parser("move", help="regroup one project")
    p_move.add_argument("project_id")
    p_move.add_argument("scope_id")

    p_rm = sub.add_parser("rm", help="stop tracking a project")
    p_rm.add_argument("project_id")

    args = parser.parse_args(argv)

    if args.cmd == "add":
        for m in add(args.scope_id, args.paths):
            print(f"  {m.scope_id:<16} {m.project_id:<16} {m.repo_path}")
        return 0

    if args.cmd == "list":
        rows = members()
        if not rows:
            print("no scopes configured -- every project reads only its own memory")
            return 0
        current = None
        for m in rows:
            if m.scope_id != current:
                current = m.scope_id
                print(f"\n{m.scope_id}")
            print(f"  {m.project_id:<18} {m.repo_path}")
        return 0

    if args.cmd == "show":
        m = resolve()
        if not m:
            print(f"{Path.cwd()} is not tracked -- arteries observes nothing here")
            return 1
        siblings = [s for s in sibling_projects(m.project_id) if s != m.project_id]
        print(f"project : {m.project_id}")
        print(f"scope   : {m.scope_id}")
        print(f"repo    : {m.repo_path}")
        print(f"shares  : {', '.join(siblings) if siblings else '(nothing yet)'}")
        return 0

    if args.cmd == "move":
        ok = move(args.project_id, args.scope_id)
        print(f"moved {args.project_id} -> {args.scope_id}" if ok
              else f"not tracked: {args.project_id}")
        return 0 if ok else 1

    ok = remove(args.project_id)
    print(f"stopped tracking {args.project_id}" if ok else f"not tracked: {args.project_id}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
