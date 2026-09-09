"""Ordered, checksummed schema migrations for a database two checkouts share.

`main` and a feature branch run against one Postgres. A branch that drops a
column or renames one breaks whichever checkout is live -- which has happened:
d40ff8e reverted a rename of `persistent.scope` after four of main's five read
paths started raising UndefinedColumn. So migrations here are additive by
default, and anything destructive waits for a contract migration after main is
on the new code.

    art migrate status                 what is applied, what is pending
    art migrate baseline               stamp existing schema as applied
    art migrate apply [--dry-run]      run pending migrations

`baseline --through NNN` is the step that makes this safe to introduce on a
database that already has a schema. A database with an empty
`schema_migrations` would otherwise re-run the whole history against real data;
`IF NOT EXISTS` covers most of that and covers no backfill at all. Baseline
stamps without executing.

`--through` is required. The operator knows which migrations the target already
satisfies and the code does not, and guessing in either direction is bad: stamp
too few and a migration re-runs, stamp too many and one silently never runs at
all. Run it once per database, ever.

Migration files are `migrations/NNN_name.sql`, applied in filename order. Two
things a file may say, on the first line:

    -- no-transaction        CREATE INDEX CONCURRENTLY and friends
    -- destructive           refuse unless --contract is passed

`VECTOR(EMBED_DIM)` is templated the same way setup_db.py templates it, so a
migration adding a vector column follows the shared embedding contract instead
of hardcoding a width that drifts.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg2

from arteries.config import DB_CONFIG, EMBED_DIM

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"

# One writer at a time, machine-wide. Two checkouts running `art migrate apply`
# at once would both read an empty pending list and both apply it. A Postgres
# advisory lock is the only thing both processes can see; a file lock is not,
# and neither is a Python variable.
_LOCK_KEY = "arteries.migrate"

_TABLE = """
CREATE TABLE IF NOT EXISTS arteries.schema_migrations (
    version     TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    baselined   BOOLEAN NOT NULL DEFAULT false
)
"""


def render(sql: str) -> str:
    """Substitute what schema.sql templates, so migrations match it."""
    return sql.replace("VECTOR(EMBED_DIM)", f"VECTOR({EMBED_DIM})")


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def available() -> list[tuple[str, str]]:
    """(version, raw sql) for every migration on disk, in filename order."""
    if not MIGRATIONS_DIR.is_dir():
        return []
    return [(p.stem, p.read_text())
            for p in sorted(MIGRATIONS_DIR.glob("*.sql"))]


def _applied(cur) -> dict[str, str]:
    cur.execute("SELECT version, checksum FROM arteries.schema_migrations")
    return dict(cur.fetchall())


def _directives(sql: str) -> set[str]:
    first = sql.lstrip().split("\n", 1)[0]
    return {word.strip() for word in first.removeprefix("--").split()} if first.startswith("--") else set()


def status() -> list[tuple[str, str]]:
    """[(version, 'applied' | 'pending' | 'CHANGED')]. CHANGED means an already
    applied file has been edited, which is never legitimate -- add a new
    migration instead of rewriting history someone else has run."""
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(_TABLE)
        conn.commit()
        done = _applied(cur)
    out = []
    for version, sql in available():
        if version not in done:
            out.append((version, "pending"))
        elif done[version] != _checksum(sql):
            out.append((version, "CHANGED"))
        else:
            out.append((version, "applied"))
    return out


def baseline(through: str) -> int:
    """Stamp migrations up to `through` as applied, without running any of them.

    `through` is required and there is no default, because the only safe default
    is the one the operator knows and the code does not: which migrations the
    target database already satisfies. Stamping everything is right for a
    database that just had schema.sql applied and catastrophic for one that has
    not -- it marks unapplied migrations as done, and they never run. A column
    that silently never gets created is worse than a migration that fails.

    So: name the last version this database already contains.
    """
    versions = [v for v, _sql in available()]
    if through not in versions:
        raise RuntimeError(
            f"unknown migration {through!r}. On disk: {', '.join(versions) or 'none'}")
    cutoff = versions.index(through)

    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(_TABLE)
        done = _applied(cur)
        stamped = 0
        for index, (version, sql) in enumerate(available()):
            if index > cutoff or version in done:
                continue
            cur.execute(
                "INSERT INTO arteries.schema_migrations (version, checksum, baselined) "
                "VALUES (%s, %s, true)",
                (version, _checksum(sql)),
            )
            stamped += 1
        conn.commit()
    return stamped


def apply(dry_run: bool = False, contract: bool = False) -> list[str]:
    """Run pending migrations in order. Returns the versions applied."""
    pending = [v for v, state in status() if state == "pending"]
    changed = [v for v, state in status() if state == "CHANGED"]
    if changed:
        raise RuntimeError(
            f"already-applied migrations were edited: {', '.join(changed)}. "
            "Add a new migration; do not rewrite one another checkout has run.")
    if dry_run or not pending:
        return pending

    by_version = dict(available())
    ran: list[str] = []
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (_LOCK_KEY,))
            conn.commit()
            # Re-read under the lock: another checkout may have applied these
            # between our status() call and acquiring it.
            for version in [v for v in pending if v not in _applied(cur)]:
                sql = by_version[version]
                directives = _directives(sql)
                if "destructive" in directives and not contract:
                    raise RuntimeError(
                        f"{version} is marked destructive. Run it as a contract "
                        "migration, after main is on the new code, with --contract.")
                if "no-transaction" in directives:
                    conn.set_session(autocommit=True)
                cur.execute(render(sql))
                cur.execute(
                    "INSERT INTO arteries.schema_migrations (version, checksum) "
                    "VALUES (%s, %s)", (version, _checksum(sql)))
                if "no-transaction" in directives:
                    conn.set_session(autocommit=False)
                else:
                    conn.commit()
                ran.append(version)
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_LOCK_KEY,))
            conn.commit()
    finally:
        conn.close()
    return ran


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="art migrate")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    baseline_p = sub.add_parser("baseline")
    baseline_p.add_argument(
        "--through", required=True,
        help="last migration this database already satisfies, e.g. 001_baseline")
    apply_p = sub.add_parser("apply")
    apply_p.add_argument("--dry-run", action="store_true")
    apply_p.add_argument("--contract", action="store_true",
                         help="allow migrations marked destructive")
    args = parser.parse_args(argv)

    if args.command == "status":
        rows = status()
        if not rows:
            print("no migrations on disk")
        for version, state in rows:
            print(f"{state:>8}  {version}")
        return 1 if any(s == "CHANGED" for _v, s in rows) else 0

    if args.command == "baseline":
        try:
            stamped = baseline(args.through)
        except RuntimeError as refused:
            print(f"refused: {refused}", file=sys.stderr)
            return 1
        print(f"baselined {stamped} migration(s) through {args.through} "
              f"against {DB_CONFIG.get('dbname')}")
        return 0

    try:
        ran = apply(dry_run=args.dry_run, contract=args.contract)
    except RuntimeError as refused:
        # A refusal is a message, not a stack trace. Both cases -- an edited
        # migration and an unflagged destructive one -- are things the operator
        # has to decide about, and a traceback buries the sentence that says so.
        print(f"refused: {refused}", file=sys.stderr)
        return 1
    verb = "would apply" if args.dry_run else "applied"
    print(f"{verb} {len(ran)}: {', '.join(ran) or 'nothing pending'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
