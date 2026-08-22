"""Entities and typed edges over the memory tiers.

The tiers stay tables with vectors; this is the layer that gives them
structure. A claim is a `persistent` row, an entity is a canonical name inside
a scope, and everything between them is a row in `memory_edges`.

Two rules the rest of the code depends on:

* **Ephemeral rows are edge endpoints, never nodes.** Their job in the graph is
  to answer "where did this come from"; they are a working set and expire by
  being consumed.
* **Scope filtering sits on the claim side of a traversal**, never the entity.
  Entities are scoped so that "hook" in a harness and "hook" in a React app are
  different nodes with their own ontology class and their own embedding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import psycopg2
import psycopg2.extras

from arteries.scope import SCOPE_CTE

logger = logging.getLogger(__name__)

# Edge vocabulary. Where a term exists in PROV-O the ontology loader can ground
# it; these are the local names the writer uses.
DERIVED_FROM = "derived_from"     # prov:wasDerivedFrom
SUPERSEDES = "supersedes"         # prov:wasRevisionOf
MENTIONS = "mentions"
RELATIONS = ("supports", "refines", "contradicts", "depends_on")
DECISION_RELS = ("chose", "over", "because")

ENTITY_KINDS = ("concept", "module", "dependency")


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    kind: str
    ontology_class: str | None
    ontology_valid: bool


def upsert_entity(cur, scope_id: str, raw_name: str, kind: str = "concept") -> Entity | None:
    """Canonicalize a name against the ontology and return its node.

    An unmatched name is kept with ontology_valid=false, never dropped -- a
    vocabulary you are still growing must not eat the facts it does not cover.
    """
    from arteries import ontology

    name = (raw_name or "").strip()
    if not name:
        return None
    if kind not in ENTITY_KINDS:
        kind = "concept"

    match = ontology.resolve(name, kind="class")
    canonical = match.name if match.valid else name

    cur.execute(
        """
        INSERT INTO arteries.entities
            (scope_id, name, raw_name, kind, ontology_class, ontology_valid, match_score)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (scope_id, kind, lower(name)) DO UPDATE
            SET raw_name = COALESCE(arteries.entities.raw_name, EXCLUDED.raw_name)
        RETURNING id, name, kind, ontology_class, ontology_valid
        """,
        (scope_id, canonical, name, kind,
         match.uri if match.valid else None, match.valid,
         match.score if match.valid else None),
    )
    row = cur.fetchone()
    return Entity(str(row[0]), row[1], row[2], row[3], row[4])


def add_edge(cur, project_id: str, src_kind: str, src_id: str, rel: str,
             dst_kind: str, dst_id: str, *, weight: float = 1.0,
             metadata: dict[str, Any] | None = None) -> None:
    """Write one typed edge, ignoring an exact repeat.

    project_id records which repo asserted the edge. Scope is resolved from the
    claim at read time, so no scope column lives here.
    """
    cur.execute(
        """
        INSERT INTO arteries.memory_edges
            (project_id, src_kind, src_id, dst_kind, dst_id, rel, weight, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT DO NOTHING
        """,
        (project_id, src_kind, str(src_id), dst_kind, str(dst_id), rel, weight,
         psycopg2.extras.Json(metadata or {})),
    )


def expand(conn, project_id: str, seed_ids: list[str], *, hops: int = 1,
           decay: float = 0.6, limit: int = 40) -> list[dict[str, Any]]:
    """Claims reachable from the seeds, weight-decayed per hop.

    The scope filter is on the claim side, so a shared entity cannot leak one
    group's claims into another's traversal.
    """
    if not seed_ids:
        return []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            SCOPE_CTE + """
            , walk AS (
                SELECT id::text AS nid, 1.0::float AS score, 0 AS hop
                FROM arteries.persistent WHERE id = ANY(%(seeds)s::uuid[])
                UNION ALL
                SELECT e.dst_id, w.score * e.weight * %(decay)s, w.hop + 1
                FROM walk w
                JOIN arteries.memory_edges e
                  ON e.src_kind = 'persistent' AND e.src_id = w.nid
                 AND e.valid_until IS NULL
                WHERE w.hop < %(hops)s
            )
            SELECT DISTINCT ON (p.id)
                   p.id, p.fact, p.domains, p.confidence, w.score, w.hop
            FROM walk w
            JOIN arteries.persistent p ON p.id = w.nid::uuid
            WHERE p.project_id IN (SELECT project_id FROM scope)
              AND p.valid_until IS NULL
              AND w.hop > 0
            ORDER BY p.id, w.score DESC
            LIMIT %(limit)s
            """,
            {"project": project_id, "seeds": [str(s) for s in seed_ids],
             "hops": hops, "decay": decay, "limit": limit},
        )
        return [dict(r) for r in cur.fetchall()]


def stats(project_id: str, db_config: dict | None = None) -> dict[str, Any]:
    from arteries.config import DB_CONFIG
    conn = psycopg2.connect(**(db_config or DB_CONFIG))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT rel, count(*) FROM arteries.memory_edges "
                        "WHERE valid_until IS NULL GROUP BY rel ORDER BY 2 DESC")
            edges = dict(cur.fetchall())
            cur.execute("SELECT kind, count(*), count(*) FILTER (WHERE ontology_valid) "
                        "FROM arteries.entities GROUP BY kind ORDER BY 2 DESC")
            entities = {k: {"total": t, "grounded": g} for k, t, g in cur.fetchall()}
        return {"edges": edges, "entities": entities}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    from arteries import scope
    from arteries.config import DB_CONFIG

    parser = argparse.ArgumentParser(prog="art graph", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="edge and entity counts")

    p_ent = sub.add_parser("entities", help="entities in this project's scope")
    p_ent.add_argument("--limit", type=int, default=30)
    p_ent.add_argument("--kind", choices=ENTITY_KINDS)

    p_why = sub.add_parser("why", help="edges touching one memory, by id prefix")
    p_why.add_argument("memory_id")

    args = parser.parse_args(argv)

    project = scope.current_project()

    if args.cmd == "stats":
        st = stats(project)
        if not st["edges"] and not st["entities"]:
            print("graph is empty -- run `art compile` to populate it")
            return 0
        print("edges:")
        for rel, n in st["edges"].items():
            print(f"  {rel:<16} {n:>6}")
        print("entities:")
        for kind, c in st["entities"].items():
            print(f"  {kind:<16} {c['total']:>6}   {c['grounded']} grounded in the ontology")
        return 0

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        if args.cmd == "entities":
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT e.kind, e.name, e.ontology_class, e.ontology_valid,
                           count(m.id) AS mentions
                    FROM arteries.entities e
                    LEFT JOIN arteries.memory_edges m
                      ON m.dst_kind = 'entity' AND m.dst_id = e.id::text
                     AND m.valid_until IS NULL
                    WHERE e.scope_id = %s AND (%s IS NULL OR e.kind = %s)
                    GROUP BY e.id ORDER BY mentions DESC, e.name
                    LIMIT %s
                    """,
                    (scope.scope_for(project) or project, args.kind, args.kind, args.limit),
                )
                for r in cur.fetchall():
                    mark = "*" if r["ontology_valid"] else " "
                    print(f" {mark}{r['kind']:<12} {r['name'][:44]:<46} {r['mentions']:>3} mentions")
            print("\n* = grounded in the loaded ontology")
            return 0

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, fact FROM arteries.persistent WHERE id::text LIKE %s LIMIT 2",
                        (args.memory_id + "%",))
            hits = cur.fetchall()
            if len(hits) != 1:
                print(f"{'no' if not hits else 'ambiguous'} memory for {args.memory_id!r}")
                return 1
            mid, fact = str(hits[0]["id"]), hits[0]["fact"]
            print(f"{fact}\n")
            cur.execute(
                """
                SELECT e.rel, e.dst_kind, e.dst_id, e.metadata,
                       coalesce(p.fact, ent.name, e.dst_id) AS target
                FROM arteries.memory_edges e
                LEFT JOIN arteries.persistent p ON e.dst_kind='persistent' AND p.id = e.dst_id::uuid
                LEFT JOIN arteries.entities ent ON e.dst_kind='entity' AND ent.id = e.dst_id::uuid
                WHERE e.src_id = %s AND e.valid_until IS NULL
                ORDER BY e.rel
                """,
                (mid,),
            )
            for r in cur.fetchall():
                reason = (r["metadata"] or {}).get("reason", "")
                print(f"  --{r['rel']}--> [{r['dst_kind']}] {str(r['target'])[:60]}")
                if reason:
                    print(f"      because: {reason[:100]}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
