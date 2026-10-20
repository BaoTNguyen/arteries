"""Resize arteries' vector columns to config.EMBED_DIM.

schema.sql only templates the width for a *fresh* install -- CREATE TABLE IF NOT
EXISTS will not touch a table that already exists. This is the path for a
database that predates an embedding model change.

Dropping the column rather than casting is deliberate: the old vectors were
produced by a different model and are not comparable to new ones, so keeping
them would silently mix two embedding spaces in one index.

The columns come from the catalog, not a list: a hand-kept list named two of
five vector columns (chunks, evergreen and entities were added after it), and
a column the migration skips keeps the old width and fails every write after
the model change. Indexes are recreated from their own definitions for the
same reason.

    python -m arteries.migrate_embed_dim            # report only
    python -m arteries.migrate_embed_dim --apply
"""

from __future__ import annotations

import argparse

import psycopg2

from arteries.config import DB_CONFIG, EMBED_DIM, EMBED_MODEL

# atttypmod is pgvector's declared width
VECTOR_COLUMNS_SQL = """
SELECT c.relname, a.attname, a.atttypmod
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_type t ON t.oid = a.atttypid
WHERE n.nspname = %s AND c.relkind = 'r' AND t.typname = 'vector'
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY c.relname, a.attname
"""

COLUMN_INDEXES_SQL = """
SELECT pg_get_indexdef(x.indexrelid)
FROM pg_index x
JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = ANY(x.indkey)
WHERE x.indrelid = %s::regclass AND a.attname = %s
"""


def migrate(apply: bool = False, db_config: dict | None = None,
            schema: str = "arteries", dim: int = EMBED_DIM) -> list[dict]:
    conn = psycopg2.connect(**(db_config or DB_CONFIG))
    report: list[dict] = []
    try:
        cur = conn.cursor()
        cur.execute(VECTOR_COLUMNS_SQL, (schema,))
        for name, column, current in cur.fetchall():
            table = f"{schema}.{name}"
            if current == dim:
                report.append({"table": table, "column": column, "status": "ok", "dim": current})
                continue

            cur.execute(f"SELECT count(*) FROM {table} WHERE {column} IS NOT NULL")
            populated = cur.fetchone()[0]
            report.append({"table": table, "column": column, "status": "resize",
                           "from": current, "to": dim, "vectors_dropped": populated})
            if not apply:
                continue

            # One transaction per column: a half-migrated column is a column
            # whose declared width and contents disagree. DROP COLUMN takes its
            # indexes with it, so their definitions are read first.
            cur.execute(COLUMN_INDEXES_SQL, (table, column))
            indexes = [r[0] for r in cur.fetchall()]
            cur.execute(f"ALTER TABLE {table} DROP COLUMN {column};")
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} VECTOR({dim});")
            for indexdef in indexes:
                cur.execute(indexdef + ";")
            conn.commit()
    finally:
        conn.close()
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="perform the migration; without it, only report")
    args = ap.parse_args()

    print(f"target model {EMBED_MODEL}  dim {EMBED_DIM}")
    for r in migrate(apply=args.apply):
        where = f"{r['table']}.{r['column']}"
        if r["status"] == "resize":
            verb = "resized" if args.apply else "would resize"
            print(f"  {verb} {where} {r['from']} -> {r['to']} "
                  f"(dropping {r['vectors_dropped']} vectors)")
        else:
            print(f"  {where}: {r['status']} (dim {r['dim']})")
    if not args.apply:
        print("\nreport only -- rerun with --apply to perform it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
