"""GEXF export, so the graph can be looked at in Gephi.

A graph you cannot see is a graph you cannot debug. `art graph stats` says there
are 3219 edges; it cannot say that 1757 of them are provenance pointing at
ephemeral rows, which is the shape of the problem rather than its size.

Bounded by hops on purpose. The whole graph laid out at once is the picture
everyone produces first and nobody learns anything from -- the ontology work
already recorded node-link layouts being illegible at scale. Two hops from a seed
is a neighbourhood someone can read.

    art graph export --out graph.gexf              # everything, one hop deep
    art graph export --seed <id> --hops 2          # a neighbourhood

Stdlib only. GEXF is XML and `xml.etree` writes XML; a library for this would be
a dependency bought for one function.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import psycopg2
import psycopg2.extras

from arteries.config import DB_CONFIG
from arteries.scope import SCOPE_CTE

# Gephi colours by attribute, so the useful thing to export is the attributes
# that separate the tiers rather than a pre-baked colour.
NODE_ATTRS = (("tier", "string"), ("kind", "string"), ("fact", "string"),
              ("core", "boolean"), ("incrementality", "float"))
EDGE_ATTRS = (("rel", "string"), ("ontology_valid", "boolean"))


def collect(project_id: str, seed: str | None = None, hops: int = 1,
            limit: int = 2000, db_config: dict | None = None
            ) -> tuple[list[dict], list[dict]]:
    """Nodes and edges within `hops` of the seed, or the whole scope if no seed.

    Tombstoned rows are absent rather than greyed out. A picture of what is true
    now is the thing being asked for; "what did we used to think" is a question
    for `art graph why`, which can answer it without crowding the layout.
    """
    conn = psycopg2.connect(**(db_config or DB_CONFIG))
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if seed:
                # Recursive walk outward. Depth is capped in SQL rather than by
                # fetching everything and filtering, because the whole point is
                # not to materialise the whole graph.
                cur.execute(
                    """
                    WITH RECURSIVE walk(id, depth) AS (
                        SELECT %(seed)s::text, 0
                      UNION
                        SELECT CASE WHEN e.src_id = w.id THEN e.dst_id ELSE e.src_id END,
                               w.depth + 1
                        FROM walk w
                        JOIN arteries.memory_edges e
                          ON (e.src_id = w.id OR e.dst_id = w.id)
                         AND e.valid_until IS NULL
                        WHERE w.depth < %(hops)s
                    )
                    SELECT DISTINCT id FROM walk
                    """,
                    {"seed": seed, "hops": hops},
                )
                ids = [r["id"] for r in cur.fetchall()]
                if not ids:
                    return [], []
                cur.execute(
                    """
                    SELECT src_kind, src_id, dst_kind, dst_id, rel, weight,
                           ontology_valid
                    FROM arteries.memory_edges
                    WHERE valid_until IS NULL
                      AND src_id = ANY(%s) AND dst_id = ANY(%s)
                    LIMIT %s
                    """,
                    (ids, ids, limit),
                )
            else:
                cur.execute(
                    SCOPE_CTE + """
                    SELECT e.src_kind, e.src_id, e.dst_kind, e.dst_id, e.rel,
                           e.weight, e.ontology_valid
                    FROM arteries.memory_edges e
                    WHERE e.valid_until IS NULL
                      AND e.project_id IN (SELECT project_id FROM scope)
                    LIMIT %(limit)s
                    """,
                    {"project": project_id, "limit": limit},
                )
            edges = [dict(r) for r in cur.fetchall()]

        wanted: dict[str, set[str]] = {}
        for edge in edges:
            wanted.setdefault(edge["src_kind"], set()).add(edge["src_id"])
            wanted.setdefault(edge["dst_kind"], set()).add(edge["dst_id"])

        nodes = []
        for tier, ids in wanted.items():
            nodes.extend(_label(conn, tier, sorted(ids)))
        return nodes, edges
    finally:
        conn.close()


def _label(conn, tier: str, ids: list[str]) -> list[dict[str, Any]]:
    """Give each node something readable. An unlabelled node is a dot."""
    queries = {
        "persistent": "SELECT id::text, fact, kind FROM arteries.persistent "
                      "WHERE id = ANY(%s::uuid[])",
        "evergreen": "SELECT id::text, fact, kind, core, incrementality "
                     "FROM arteries.evergreen WHERE id = ANY(%s::uuid[])",
        "ephemeral": "SELECT id::text, fact, 'turn' AS kind FROM arteries.ephemeral "
                     "WHERE id = ANY(%s::uuid[])",
        "entity": "SELECT id::text, name AS fact, kind FROM arteries.entities "
                  "WHERE id = ANY(%s::uuid[])",
    }
    sql = queries.get(tier)
    if sql is None:
        # chunk, document, episode -- real nodes with no label table here. Shown
        # by id rather than dropped: a dangling edge is more misleading than an
        # unnamed node.
        return [{"id": i, "tier": tier, "label": f"{tier}:{i[:8]}", "fact": "",
                 "kind": tier} for i in ids]
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        try:
            cur.execute(sql, (ids,))
        except psycopg2.Error:
            conn.rollback()
            return [{"id": i, "tier": tier, "label": f"{tier}:{i[:8]}",
                     "fact": "", "kind": tier} for i in ids]
        rows = [dict(r) for r in cur.fetchall()]
    return [{"id": r["id"], "tier": tier,
             "label": (r.get("fact") or "")[:60] or f"{tier}:{r['id'][:8]}",
             "fact": r.get("fact") or "", "kind": r.get("kind") or tier,
             "core": bool(r.get("core")),
             "incrementality": r.get("incrementality")}
            for r in rows]


def write(nodes: list[dict], edges: list[dict], path: str) -> int:
    """Write GEXF 1.3. Returns the node count."""
    root = ET.Element("gexf", {"xmlns": "http://gexf.net/1.3", "version": "1.3"})
    graph = ET.SubElement(root, "graph", {"mode": "static", "defaultedgetype": "directed"})

    node_attrs = ET.SubElement(graph, "attributes", {"class": "node"})
    for index, (name, kind) in enumerate(NODE_ATTRS):
        ET.SubElement(node_attrs, "attribute",
                      {"id": str(index), "title": name, "type": kind})
    edge_attrs = ET.SubElement(graph, "attributes", {"class": "edge"})
    for index, (name, kind) in enumerate(EDGE_ATTRS):
        ET.SubElement(edge_attrs, "attribute",
                      {"id": str(index), "title": name, "type": kind})

    known = {n["id"] for n in nodes}
    nodes_el = ET.SubElement(graph, "nodes")
    for node in nodes:
        el = ET.SubElement(nodes_el, "node",
                           {"id": node["id"], "label": node["label"]})
        values = ET.SubElement(el, "attvalues")
        for index, (name, _kind) in enumerate(NODE_ATTRS):
            value = node.get(name)
            if value is None:
                continue
            ET.SubElement(values, "attvalue",
                          {"for": str(index), "value": str(value)})

    edges_el = ET.SubElement(graph, "edges")
    for index, edge in enumerate(edges):
        # Gephi drops an edge whose endpoints it has never seen, silently. Doing
        # it here instead means the count printed is the count exported.
        if edge["src_id"] not in known or edge["dst_id"] not in known:
            continue
        el = ET.SubElement(edges_el, "edge", {
            "id": str(index), "source": edge["src_id"], "target": edge["dst_id"],
            "weight": str(edge.get("weight") or 1.0), "label": edge["rel"]})
        values = ET.SubElement(el, "attvalues")
        ET.SubElement(values, "attvalue", {"for": "0", "value": edge["rel"]})
        ET.SubElement(values, "attvalue",
                      {"for": "1", "value": str(bool(edge.get("ontology_valid")))})

    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return len(nodes)
