# Arteries Ontology

**Design specification — pre-implementation**

A vocabulary for the memory graph, designed backwards from one question: *does this
plan contradict something I already decided?*

- Date: 23 August 2026
- Schema `arteries`, database `capillaries`
- Verified against `src/arteries` at commit `bac2f69` and the live database
- Status: awaiting one open decision

---

## The storage verdict

> **One database, one schema, group filters. Splitting per project group would break
> the read path you already have.**

Everything is already central. The vector layer is pgvector columns on four tables,
the graph layer is `entities` plus `memory_edges` plus recursive CTEs, and the
relational layer is the same tables. There is no separate graph database and no
separate vector database, and neither would earn itself — three-hop traversal
measures 2.1 ms against an 80k-claim projection.

The scoping keys already differ per table, and that asymmetry *is* the group-filter
mechanism. Per-group databases would duplicate the T-Box, break `SCOPE_CTE`, and make
a cross-group question unanswerable by construction.

| Table | Scoping key | What a new group does to it | Cross-group leak? |
| --- | --- | --- | --- |
| `persistent` | project_id, read via SCOPE_CTE | Adds rows; reads stay inside the group | contained |
| `entities` | scope_id | New node namespace per group | contained |
| `memory_edges` | project_id (asserting repo) | Adds rows; scope resolved on the claim side at read | contained |
| `documents`, `chunks` | project_id | Per-repo only — no scope read path exists | contained |
| `ephemeral` | project_id + agent_process_id | Per-process, expires by compilation | contained |
| runs, events, episodes, decisions, rewards | project_id | Append-only telemetry | contained |
| `ontology_terms` | **none — global** | New vocabulary becomes visible to every scope at once | **LEAKS** |

### The one real leak

`ontology_terms` has no scope column, and `ontology._lookup()` caches the whole table
in a process-global dict. The moment a trading vocabulary loads, `position`,
`exposure`, and `signal` become groundable inside a coding scope, and a compile pass
will canonicalize an unrelated word onto a finance URI — asserting a false identity,
which is the one failure this system is explicitly built to avoid. Fixing this is a
prerequisite for Layer 2, not a follow-up.

### What would force a split later

A different Postgres host. A legally distinct retention class. Or an embedding model
change — `EMBED_DIM` is a single column width shared with capillaries, and the two
already drifted silently once.

---

## What the interrogation settled

Seven decisions, in the order they were made. Each one narrowed the next, so the
sequence is load-bearing — reopening an early one invalidates what follows.

**01 · Scope roster — harness plus many, growing over time**
Roughly 20 repos across unrelated domains. Layer 2 and the T-Box scope filter become
prerequisites rather than extension points.

**02 · Scope grain — hand-defined membership, arbitrary cardinality**
Mostly singletons, `harness` as the one cluster. No mechanism change — this is what
`art scope add` already does. Add `heart` and `plexus` now, before their first commit
mints a scope-of-one.

**03 · Target query — conflict detection at design time**
Not decision recall. When feature B is specced, surface the prior decisions and
constraints its details violate. This is what the entire ontology is designed
backwards from.

**04 · Capture path — plexus/heart, plus spec imports, plus your own planning turns**
Introduces the axis the model lacks: **prospective** claims (plans, constraints,
decisions — what must be true) versus **retrospective** ones (facts — what is true).
Conflict detection runs only against the prospective set, which keeps narration out
without the compiler needing to be clever.

**05 · Conflict class — direct contradiction and constraint violation only**
Dependency blast radius and unresolved tension are cut. `competes_with` is never
built; `depends_on` stays in the vocabulary but never fires an interrupt.

**06 · Entity grain — hierarchical, with `is_part_of` containment**
Extract at whatever level the claim names it, then roll up: `persistent.kind` →
`persistent` → `arteries`. Constraints inherit downward; match scores decay with
rollup depth, so a meeting at the root doesn't outrank a meeting at the column.

**07 · Validity, trigger, vocabulary, evaluation**
Tombstone with a readable supersede chain · `art conflicts` on demand · LLM proposes,
you approve · label each conflict useful or noise. Superseded claims never fire, but a
live conflict arrives with its revision history. No `MemoryFrame` change, so
capillaries absorbs nothing. Labels write to `arteries.rewards`, which marrow already
trains on.

---

## The ontology, in three layers

The finding that drives the design: **zero of 23 entities are grounded.** You have 136
T-Box terms loaded — PROV-O and SKOS — and not one can ever match `reranker.py`,
`pgvector`, `dev-wip`, or `MemoryFrame`. PROV-O describes lineage between records,
which arteries already expresses structurally as edges. The vocabulary you loaded
covers the layer that needs no naming and leaves bare the layer that does.

### Layer 0 — Lineage
*loaded · binds to `*`*

Keep PROV-O and SKOS, but bind them to *predicates*, not entity names. This is the
correction, not a new load.

```
derived_from    → prov:wasDerivedFrom
supersedes      → prov:wasRevisionOf
invalidated_by  → prov:wasInvalidatedBy
the compile run → prov:wasGeneratedBy
```

`memory_edges.ontology_valid` exists for exactly this and is never written. The column
is dead today.

### Layer 1 — The arteries core
*universal · binds to `*`*

Classes that map one-to-one onto columns that already exist, so nothing migrates.
`kind` stays the coarse column; `ontology_class` carries the fine type.

```
art:Claim  ⊃ Fact · Decision · Preference · Constraint
art:Entity ⊃ SoftwareModule · Service · Dependency ·
             Interface · Artifact · Concept · Metric · Environment

relations to add: applies_to · invalidated_by · causes · measured_at · alternative_to
```

The live data shows the current three entity kinds are too coarse to be useful:
`/agent/route` and `agent/route.py` are two nodes both typed `module`, `dev-wip` (a git
branch) is typed `concept`, and `Arteries`, `Capillaries`, `Heart`, `Marrow` are all
typed `module` when they are projects.

### Layer 2 — Domain vocabularies
*per scope · SKOS*

The nouns each scope actually argues about. `harness` gets MemoryFrame, gate, tier,
hook, adapter, episode, reward. A finance scope gets instrument, position, drawdown.
These must never resolve outside their own scope.

**Mechanism:** `ontology_terms.source` already exists, so this is one small table —
`ontology_bindings(scope_id, source)` — plus a scope filter threaded through
`_lookup()` and its process cache. Layers 0 and 1 bind to every scope.

---

## Why grounding is load-bearing here

Cosine similarity cannot find these conflicts. *"Feature A keys memories by UUID"* and
*"feature B keys by repo path"* are semantically distant and structurally
incompatible. What finds them is the shared-entity arm of `expand()`, where both
claims reach the same entity node.

Which is precisely why the T-Box matters: if one claim says `primary key` and the
other says `id column`, they have to collapse to one node or the conflict is
invisible. A 0% grounding rate is not a cosmetic gap — it is the reason the mechanism
cannot work today. Your own traversal already depends on this arm: 68 `mentions` edges
do more work than the 33 direct claim-to-claim edges combined.

---

## Effect on each store

| Layer | Today | After |
| --- | --- | --- |
| Relational | `persistent.kind` is free text defaulting to `fact`. 147 of 157 rows are facts; one row is a constraint. | Kind constrained to Layer 1 classes, fine type in `ontology_class`, and a prospective/retrospective split that makes the constraint set queryable. |
| Graph | 23 entities, none grounded. `ancestors()` returns nothing, so subclass expansion is inert. `depends_on` has zero live edges. | `metadata.ontology_ancestors` populates and `expand()` reaches a claim about `pgvector` from a query about a vector store. Containment gives constraints a rollup path. |
| Vector | `entities.embedding` is declared and **never written**. Synonyms are embedded separately and stay separate. | Grounding produces a canonical name worth embedding, and alias collapse cuts fragmentation *before* the vector is computed rather than after. |

---

## Findings in the current code

Verified against the repository and the live database. Each is a prerequisite or a
correction, not a nice-to-have.

**`ontology.py` · `_lookup()`**
The T-Box is global and process-cached. No scope column, no scope filter. Blocks
Layer 2 entirely.

**`compile.py:518-524`**
`chose` and `over` edges point at literal strings, not entities. Eight decision claims
exist and none can be aggregated, because the alternatives are free text that never
canonicalizes.

**`schema.sql` · decisions, rewards, persistent**
The decision ledger has no edge into `persistent`. Heart's decisions and rewards sit
in the same database as the claims they concern, entirely disconnected. This is what
blocks "which decisions actually worked" — deferred, but worth knowing the shape of.

**`graph.py` · `add_edge()`**
`memory_edges.ontology_valid` is never written, and `supersedes` metadata carrying the
compiler's reason is never read back. Layer 0 and the supersede chain both depend on
these.

**`scope_members` · live data**
270 junk rows in a `demo` scope point at deleted `/tmp` directories, left by tests.
Three real members exist. Clean before the entity namespace starts mattering.

**`extract.py` · `compile.py`**
Entity extraction under-produces by roughly an order of magnitude: 23 entities across
157 claims, about 0.15 per claim where two to five is typical. No amount of vocabulary
fixes a graph with no nodes in it.

---

## Failure modes to design against

- **False identity.** A wrong canonicalization asserts something untrue; a miss costs
  nothing. Keep the 0.8 cutoff, keep unmatched names with `ontology_valid = false`, and
  never let the ontology gate a write.
- **Rollup noise.** Containment inheritance re-creates the coarse-entity problem if a
  match at the `arteries` root scores like a match at `persistent.kind`. Depth decay is
  not optional.
- **Vocabulary drift.** Two scopes independently name the same concept differently, and
  cross-scope reach silently stops working. The review flow has to surface near-duplicate
  terms across scopes, not just within one.
- **Preserved fragmentation.** An approval loop that only ever *adds* terms will canonize
  `/agent/route` and `agent/route.py` as two permanent terms. Merging is the part that
  needs a human.
- **T-Box sprawl.** Fifteen scopes with an unbounded proposal loop ends in thousands of
  terms and a difflib scan that stops being cheap. Budget terms per scope and prune by
  mention count.
- **The mute failure.** A conflict detector that cries wolf gets ignored in a week, and
  you will not notice it happening. This is what the useful/noise label exists to catch
  early.

---

## The one open decision

**Retention and privacy across scopes in one database.** A career scope holds
compensation figures and a trading scope holds account data, in the same Postgres as
your code memory, with the same backups and the same local LLM reading every compile
batch.

My recommendation is to keep one database and add a `sensitivity` flag at the scope
level that does three things: excludes the scope from any cross-scope read, keeps its
claims out of the ontology review batches sent to a model, and marks its rows for a
shorter retention sweep. That is one column and one filter, and it avoids a second
database you would then have to keep in sync.

Say the word if you'd rather those scopes live somewhere else entirely — it changes the
storage verdict for them specifically, and nothing else in this document.

---

## Implementation order

Sequenced so each step is verifiable before the next depends on it. Nothing here is
built yet.

1. **Clean and register scopes.** Drop the 270 `demo` rows; add `heart` and `plexus` to
   `harness`.
2. **Scope the T-Box.** Add `ontology_bindings`, thread scope through `_lookup()` and its
   cache. Prerequisite for everything downstream.
3. **Fix entity extraction.** Raise yield and emit containment. Without nodes, nothing
   else is measurable — watch entities-per-claim, not grounding rate, at this stage.
4. **Write Layer 1 as a small `.ttl`** and load it. Grounding rate becomes non-zero here
   for the first time.
5. **Seed the `harness` Layer 2 vocabulary by hand** from the ungrounded backlog —
   roughly 20 terms. Do one by hand before automating the loop, so you know what good
   output looks like.
6. **Ground decision alternatives as entities** instead of literals, and add `applies_to`
   for constraints.
7. **Build `art conflicts`** over containment rollup, restricted to the prospective claim
   set, with the supersede chain in its output.
8. **Add the useful/noise label** writing to `arteries.rewards`. Now it is measurable.
9. **Automate the review loop** once you have a labelled sample worth learning from.

---

*Counts cited are live, not projected. HTML version:
https://claude.ai/code/artifact/ee5fd9c6-8b7f-45ef-85eb-32a4cf4c2dc3*
