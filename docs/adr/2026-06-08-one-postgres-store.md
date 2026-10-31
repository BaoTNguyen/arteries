---
status: accepted
date: 2026-06-08
approved-by: operator
drafted-by: operator
retire-evidence: a different Postgres host, a legally distinct retention class, or an embedding model change
switch: none
---
# One Postgres holds memory's vectors, graph and relations

## Context and Problem Statement

Memory needs vector search, a graph of claims and entities, and relational
lineage across tiers, for many agent processes writing at once.

## Decision Drivers

* Concurrent writers: every agent process writes shared memory
* Lineage, per-project compilation and cross-tier foreign keys
* Cross-group questions must stay answerable

## Considered Options

* Postgres with pgvector; the graph as `entities` + `memory_edges` + recursive CTEs; one database, one schema, scope filters
* SQLite + sqlite-vec: a single writer becomes the bottleneck with many agent processes
* DuckDB + vss: experimental persistence, an OLAP engine for OLTP churn, no pre-filtered vector search
* LanceDB or ChromaDB: non-relational; lineage and cross-tier keys move into application code
* Neo4j for the graph: three-hop traversal measures 2.1 ms in Postgres; Gephi covers viewing
* A database per project group: duplicates the T-Box, breaks `SCOPE_CTE`, makes cross-group questions unanswerable

## Decision Outcome

Chosen option: "one Postgres", because it serves all three layers inside one
transaction model, and scoping keys per table already contain each group.

### Consequences

* Good, because there is no second store to keep in sync.
* Bad, because `ontology_terms` is global, so a new vocabulary is visible to every scope
  until it gets a scope column (the one leak found, 2026-08-25).

### Confirmation

`src/arteries/schema.sql` and `src/arteries/storage.py`.
