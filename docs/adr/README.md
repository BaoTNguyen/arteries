# Decisions

Why arteries is shaped the way it is, one file per decision in trimmed MADR 4.
Format and rules: [vascular/docs/adr](https://github.com/BaoTNguyen/vascular/tree/main/docs/adr).
The "In force" table is generated from each record's front matter: run
`python3 docs/adr/index.py` after adding or superseding one. CI fails when the
table is stale.

## In force

<!-- adr-index:start -->
| Decision | Date |
|---|---|
| [A retraction renders in one packet; persistent memory carries it after](2026-09-15-retraction-renders-once.md) | 2026-09-15 |
| [Retraction uses deterministic detectors, a fixed precedence, and dry-run first](2026-09-15-retraction-detectors.md) | 2026-09-15 |
| [Packets chain by covered range, not by text overlap](2026-09-15-packet-chaining-by-range.md) | 2026-09-15 |
| [Mid-session decisions come from existing signals, not an intake classifier](2026-09-15-mid-session-decisions.md) | 2026-09-15 |
| [One packet builder, two renderers: retrieval tiers and compaction state](2026-09-15-compaction-renderer-split.md) | 2026-09-15 |
| [A compaction packet never drops its critical fields silently](2026-09-15-compaction-never-drops-silently.md) | 2026-09-15 |
| [Versioned SQL migrations, run by art migrate, schema live before code](2026-09-05-migrations.md) | 2026-09-05 |
| [Three memory tiers, sentence atoms, and activity-day retention](2026-09-05-memory-tiers-and-retention.md) | 2026-09-05 |
| [Dedupe in three layers: unique index, compiler judgement, retrieval screen](2026-09-05-dedupe-layers.md) | 2026-09-05 |
| [One Postgres holds memory's vectors, graph and relations](2026-06-08-one-postgres-store.md) | 2026-06-08 |
<!-- adr-index:end -->

## Stack decisions that bind arteries

| Decision | Date |
|---|---|
| [A feature lives in the repo whose question it answers](https://github.com/BaoTNguyen/vascular/blob/main/docs/adr/2026-07-20-ownership-by-question.md) | 2026-07-20 |
| [Seams are pinned by contract tests in each repo](https://github.com/BaoTNguyen/vascular/blob/main/docs/adr/2026-07-20-contract-tests.md) | 2026-07-20 |
| [GitHub enforces PR plus CI on main](https://github.com/BaoTNguyen/vascular/blob/main/docs/adr/2026-10-30-protect-main.md) | 2026-10-30 |
