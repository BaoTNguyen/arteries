"""Resize arteries' vector columns to config.EMBED_DIM.

schema.sql only templates the width for a *fresh* install -- CREATE TABLE IF NOT
EXISTS will not touch a table that already exists. This is the path for a
database that predates an embedding model change.

Dropping the column rather than casting is deliberate: the old vectors were
produced by a different model and are not comparable to new ones, so keeping
them would silently mix two embedding spaces in one index.

    python -m arteries.migrate_embed_dim            # report only
    python -m arteries.migrate_embed_dim --apply
"""

from __future__ import annotations

import argparse

import psycopg2

from arteries.config import DB_CONFIG, EMBED_DIM, EMBED_MODEL

# (table, column, index to drop with it). Ephemeral deliberately has no vector
# index: it holds a few hundred rows per agent process at most, so a sequential
# scan beats paying HNSW insert cost on the hook path.
# ponytail: add an index here if a single process ever holds >10k ephemerals.
TARGETS = [
    ("arteries.ephemeral", "embedding", None),
    ("arteries.persistent", "embedding", "arteries.idx_per_embedding"),
    ("arteries.evergreen", "embedding", "arteries.idx_evg_embedding"),
]

CURRENT_DIM_SQL = """
SELECT a.atttypmod
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s AND NOT a.attisdropped
"""

REBUILD_SQL = {
    "arteries.idx_per_embedding": (
        "CREATE INDEX idx_per_embedding ON arteries.persistent "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    ),
    "arteries.idx_evg_embedding": (
        "CREATE INDEX idx_evg_embedding ON arteries.evergreen "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    ),
}


def migrate(apply: bool = False, db_config: dict | None = None) -> list[dict]:
    conn = psycopg2.connect(**(db_config or DB_CONFIG))
    report: list[dict] = []
    try:
        cur = conn.cursor()
        for table, column, index in TARGETS:
            schema, name = table.split(".")
            cur.execute(CURRENT_DIM_SQL, (schema, name, column))
            row = cur.fetchone()
            if row is None:
                report.append({"table": table, "status": "missing"})
                continue

            current = row[0]  # pgvector stores the declared width in atttypmod
            if current == EMBED_DIM:
                report.append({"table": table, "status": "ok", "dim": current})
                continue

            cur.execute(f"SELECT count(*) FROM {table} WHERE {column} IS NOT NULL")
            populated = cur.fetchone()[0]
            report.append({"table": table, "status": "resize", "from": current,
                           "to": EMBED_DIM, "vectors_dropped": populated})
            if not apply:
                continue

            # One transaction per table: a half-migrated column is a column
            # whose declared width and contents disagree.
            if index:
                cur.execute(f"DROP INDEX IF EXISTS {index};")
            cur.execute(f"ALTER TABLE {table} DROP COLUMN {column};")
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} VECTOR({EMBED_DIM});")
            if index:
                cur.execute(REBUILD_SQL[index] + ";")
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
        if r["status"] == "resize":
            verb = "resized" if args.apply else "would resize"
            print(f"  {verb} {r['table']}.embedding {r['from']} -> {r['to']} "
                  f"(dropping {r['vectors_dropped']} vectors)")
        else:
            print(f"  {r['table']}: {r['status']}"
                  + (f" (dim {r['dim']})" if "dim" in r else ""))
    if not args.apply:
        print("\nreport only -- rerun with --apply to perform it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
