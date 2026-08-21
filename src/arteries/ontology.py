"""The T-Box: load an ontology, then canonicalize extracted names against it.

Without an ontology an LLM invents a fresh name every time it sees a concept --
"vector store", "vector database", "the pgvector store" become three unrelated
nodes and the graph fragments into synonyms. Grounding replaces the LLM's name
with a term from a vocabulary someone already thought hard about.

The split that matters here: `load()` needs rdflib, `resolve()` does not.
Loading happens once, by hand, from the CLI. Resolution happens inside every
compile pass, in a short-lived subprocess, so it reads the parsed vocabulary
back out of Postgres and matches with stdlib difflib. rdflib stays an optional
extra that the hot path never imports.

Unmatched names are kept and flagged, never dropped -- an ontology you are
still growing must not silently eat the facts it does not cover yet.

    art ontology load prov-o.ttl --source prov-o
    art ontology stats
"""

from __future__ import annotations

import argparse
import difflib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg2
import psycopg2.extras

from arteries.config import DB_CONFIG

logger = logging.getLogger(__name__)

# cognee matches at 0.8 and it is a reasonable default: high enough that
# "postgres" does not capture "postgrest", low enough to absorb plurals and
# word-order noise. Raise it if you see wrong canonicalizations, which are worse
# than misses -- a miss keeps the raw name, a bad match asserts a false identity.
DEFAULT_CUTOFF = 0.8

# Predicates worth caching. Anything else in the file is ignored rather than
# stored: this is a lookup table for name matching, not an RDF triple store.
_CLASS_TYPES = {
    "http://www.w3.org/2002/07/owl#Class",
    "http://www.w3.org/2000/01/rdf-schema#Class",
}
_PROPERTY_TYPES = {
    "http://www.w3.org/2002/07/owl#ObjectProperty",
    "http://www.w3.org/2002/07/owl#DatatypeProperty",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property",
    "http://www.w3.org/2002/07/owl#AnnotationProperty",
}


@dataclass(frozen=True)
class Match:
    """Outcome of grounding one extracted name."""

    name: str            # canonical name to store (ontology label, or the raw name)
    uri: str | None      # ontology term, None when unmatched
    score: float         # 1.0 exact, difflib ratio for fuzzy, 0.0 unmatched
    valid: bool          # did the T-Box recognise it
    kind: str | None = None   # class | property | individual


def normalize(text: str) -> str:
    """Match key: lowercase, non-alphanumerics collapsed to single underscores.

    Also splits camelCase, because ontologies name properties `wasDerivedFrom`
    while an LLM writes "was derived from" -- without the split those never meet.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return re.sub(r"[^a-z0-9]+", "_", spaced.lower()).strip("_")


# -- load (needs rdflib) -------------------------------------------------------

def load(path: str | Path, source: str | None = None, db_config: dict | None = None) -> dict:
    """Parse an ontology file into arteries.ontology_terms.

    Format is inferred from the extension by rdflib: .ttl, .owl, .rdf, .nt,
    .jsonld all work. Reloading the same source replaces its terms, so editing
    your .ttl and re-running is the expected workflow.
    """
    try:
        from rdflib import RDF, RDFS, Graph, OWL, URIRef
        from rdflib.namespace import SKOS
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise SystemExit(
            "loading an ontology needs rdflib: pip install 'arteries[ontology]'\n"
            "(only this command needs it; resolution at runtime does not)"
        ) from exc

    SKOS_ALT_LABEL = SKOS.altLabel

    path = Path(path)
    source = source or path.stem
    graph = Graph()
    graph.parse(str(path))

    terms: dict[str, dict] = {}

    def _label(subject) -> str:
        for obj in graph.objects(subject, RDFS.label):
            return str(obj)
        # No rdfs:label: fall back to the URI fragment, which is what the term
        # is actually called in most well-formed ontologies anyway.
        text = str(subject)
        return text.rsplit("#", 1)[-1].rsplit("/", 1)[-1]

    def _record(subject, kind: str) -> None:
        if not isinstance(subject, URIRef):
            return  # blank nodes have no stable identity to match against
        uri = str(subject)
        label = _label(subject)
        comment = next((str(o) for o in graph.objects(subject, RDFS.comment)), None)
        parent = next(
            (str(o) for o in graph.objects(subject, RDFS.subClassOf) if isinstance(o, URIRef)),
            None,
        ) or next(
            (str(o) for o in graph.objects(subject, RDFS.subPropertyOf) if isinstance(o, URIRef)),
            None,
        )
        # Index the URI fragment alongside the label. SKOS labels skos:broader
        # as "has broader", so a query for "broader" scores 0.74 against the
        # label and misses at any sane cutoff -- while the fragment is an exact
        # hit. cognee only matches labels; this is the gap that showed up the
        # first time PROV-O and SKOS were loaded together.
        fragment = uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        aliases = {normalize(fragment)}
        for alt in graph.objects(subject, SKOS_ALT_LABEL):
            aliases.add(normalize(str(alt)))
        aliases.discard(normalize(label))
        terms[uri] = {
            "uri": uri, "label": label, "normalized": normalize(label),
            "aliases": sorted(a for a in aliases if a),
            "parent_uri": parent, "kind": kind, "comment": comment, "source": source,
        }

    for type_uri in _CLASS_TYPES:
        for subject in graph.subjects(RDF.type, URIRef(type_uri)):
            _record(subject, "class")
    for type_uri in _PROPERTY_TYPES:
        for subject in graph.subjects(RDF.type, URIRef(type_uri)):
            _record(subject, "property")
    # Named individuals are the A-Box half of an ontology -- cognee keeps a
    # separate lookup for them and so do we, since an extracted name is at least
    # as likely to be an instance as a class.
    for subject in graph.subjects(RDF.type, OWL.NamedIndividual):
        _record(subject, "individual")

    conn = psycopg2.connect(**(db_config or DB_CONFIG))
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM arteries.ontology_terms WHERE source = %s", (source,))
            psycopg2.extras.execute_batch(
                cur,
                """
                INSERT INTO arteries.ontology_terms
                    (uri, label, normalized, aliases, parent_uri, kind, comment, source)
                VALUES (%(uri)s, %(label)s, %(normalized)s, %(aliases)s, %(parent_uri)s,
                        %(kind)s, %(comment)s, %(source)s)
                ON CONFLICT (uri) DO UPDATE SET
                    label = EXCLUDED.label, normalized = EXCLUDED.normalized,
                    aliases = EXCLUDED.aliases,
                    parent_uri = EXCLUDED.parent_uri, kind = EXCLUDED.kind,
                    comment = EXCLUDED.comment, source = EXCLUDED.source,
                    loaded_at = now()
                """,
                list(terms.values()),
            )
        conn.commit()
    finally:
        conn.close()

    counts: dict[str, int] = {}
    for term in terms.values():
        counts[term["kind"]] = counts.get(term["kind"], 0) + 1
    return {"source": source, "terms": len(terms), **counts}


# -- resolve (stdlib only) -----------------------------------------------------

# key -> (uri, kind, label). Populated once per process.
_cache: dict[str, tuple[str, str, str]] | None = None

# When two terms normalize to the same key -- PROV-O has both the class
# prov:Entity and the property prov:entity -- prefer this order. A name being
# grounded is nearly always a thing, not a relation between things.
_KIND_PRIORITY = {"class": 0, "individual": 1, "property": 2}


def _lookup(db_config: dict | None = None) -> dict[str, tuple[str, str, str]]:
    """match key -> (uri, kind, label), loaded once per process.

    A compile pass grounds every extracted name against this; re-querying per
    name would be a round trip each. The whole T-Box is a few thousand rows.
    """
    global _cache
    if _cache is not None:
        return _cache
    # Any failure here -- no database, no schema, no ontology_terms table --
    # is the same situation as no ontology loaded: everything grounds unmatched
    # and nothing raises. Grounding is an enhancement, never a gate, and it runs
    # inside compile passes that must not die because a table is missing.
    conn = None
    try:
        conn = psycopg2.connect(**(db_config or DB_CONFIG))
        with conn.cursor() as cur:
            cur.execute("SELECT uri, label, normalized, aliases, kind FROM arteries.ontology_terms")
            table: dict[str, tuple[str, str, str]] = {}
            for uri, label, normalized, aliases, kind in cur.fetchall():
                for key in (normalized, *(aliases or [])):
                    if not key:
                        continue
                    incumbent = table.get(key)
                    if incumbent and _KIND_PRIORITY.get(incumbent[1], 9) <= _KIND_PRIORITY.get(kind, 9):
                        continue
                    table[key] = (uri, kind, label)
            _cache = table
    except Exception:
        logger.debug("ontology lookup unavailable; grounding disabled", exc_info=True)
        _cache = {}
    finally:
        if conn is not None:
            conn.close()
    return _cache


def reset_cache() -> None:
    """Drop the in-process T-Box cache. For tests and for after a reload."""
    global _cache
    _cache = None


def resolve(
    name: str,
    cutoff: float = DEFAULT_CUTOFF,
    kind: str | None = None,
    db_config: dict | None = None,
) -> Match:
    """Ground one extracted name against the loaded ontology.

    Exact match first, then fuzzy. Pass `kind` to restrict to classes,
    properties, or individuals -- without it, a name that collides across kinds
    resolves by _KIND_PRIORITY. An unmatched name comes back with the raw text,
    no URI, and valid=False; callers store it as-is rather than dropping it.
    """
    raw = (name or "").strip()
    if not raw:
        return Match(name="", uri=None, score=0.0, valid=False)

    table = _lookup(db_config)
    if kind:
        table = {k: v for k, v in table.items() if v[1] == kind}
    if not table:
        return Match(name=raw, uri=None, score=0.0, valid=False)

    key = normalize(raw)
    if key in table:
        uri, term_kind, label = table[key]
        return Match(name=label, uri=uri, score=1.0, valid=True, kind=term_kind)

    close = difflib.get_close_matches(key, table.keys(), n=1, cutoff=cutoff)
    if not close:
        return Match(name=raw, uri=None, score=0.0, valid=False)
    uri, term_kind, label = table[close[0]]
    return Match(
        name=label,
        uri=uri,
        score=difflib.SequenceMatcher(None, key, close[0]).ratio(),
        valid=True,
        kind=term_kind,
    )


def ancestors(uri: str, db_config: dict | None = None) -> list[str]:
    """The subclass chain above a term, nearest first.

    This is cognee's subgraph expansion: if the ontology says ElectricCar is a
    Car, a claim about an ElectricCar is also reachable from Car. One recursive
    CTE, no graph library.
    """
    conn = psycopg2.connect(**(db_config or DB_CONFIG))
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH RECURSIVE chain AS (
                    SELECT uri, parent_uri, 0 AS depth
                    FROM arteries.ontology_terms WHERE uri = %s
                    UNION ALL
                    SELECT t.uri, t.parent_uri, c.depth + 1
                    FROM arteries.ontology_terms t
                    JOIN chain c ON t.uri = c.parent_uri
                    WHERE c.depth < 16   -- ponytail: guards a cyclic ontology
                )
                SELECT uri FROM chain WHERE depth > 0 ORDER BY depth
                """,
                (uri,),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def stats(db_config: dict | None = None) -> list[dict]:
    conn = psycopg2.connect(**(db_config or DB_CONFIG))
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT source, kind, count(*) AS terms, max(loaded_at) AS loaded_at
                FROM arteries.ontology_terms
                GROUP BY source, kind ORDER BY source, kind
                """
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="art ontology", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    load_cmd = sub.add_parser("load", help="parse an ontology file into the T-Box")
    load_cmd.add_argument("path")
    load_cmd.add_argument("--source", help="name for this vocabulary (default: filename)")

    sub.add_parser("stats", help="what is loaded")

    resolve_cmd = sub.add_parser("resolve", help="ground one name, for checking a load")
    resolve_cmd.add_argument("name")
    resolve_cmd.add_argument("--cutoff", type=float, default=DEFAULT_CUTOFF)

    args = parser.parse_args(argv)

    if args.cmd == "load":
        result = load(args.path, args.source)
        kinds = ", ".join(f"{k}={v}" for k, v in result.items()
                          if k not in ("source", "terms"))
        print(f"loaded {result['terms']} terms from {result['source']} ({kinds})")
        return 0

    if args.cmd == "stats":
        try:
            rows = stats()
        except Exception as exc:
            print(f"cannot read the T-Box: {exc.__class__.__name__} -- "
                  f"run `art setup-db` first")
            return 1
        if not rows:
            print("no ontology loaded -- everything will ground as unmatched")
            return 0
        for row in rows:
            print(f"  {row['source']:<20} {row['kind']:<12} {row['terms']:>5} terms"
                  f"   loaded {row['loaded_at']:%Y-%m-%d}")
        return 0

    match = resolve(args.name, cutoff=args.cutoff)
    if match.valid:
        print(f"  {args.name!r} -> {match.name!r}  ({match.score:.2f})  {match.uri}")
    else:
        print(f"  {args.name!r} -> unmatched, kept as-is")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
