"""Run schema.sql against the configured Postgres instance."""

from __future__ import annotations

import os

import psycopg2

from arteries.config import DB_CONFIG, EMBED_DIM


def setup() -> None:
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path) as f:
        # The width is templated, not literal, so it follows the shared
        # embedding contract in config. Same trick capillaries uses in chunk.py.
        sql = f.read().replace("VECTOR(EMBED_DIM)", f"VECTOR({EMBED_DIM})")

    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(sql)
        conn.commit()
    print("arteries schema ready.")


if __name__ == "__main__":
    setup()
