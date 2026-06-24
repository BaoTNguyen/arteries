"""Run schema.sql against the configured Postgres instance."""

from __future__ import annotations

import os

import psycopg2

from arteries.config import DB_CONFIG


def setup() -> None:
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path) as f:
        sql = f.read()

    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(sql)
        conn.commit()
    print("arteries schema ready.")


if __name__ == "__main__":
    setup()
