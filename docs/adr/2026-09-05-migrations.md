---
status: accepted
date: 2026-09-05
approved-by: operator
drafted-by: operator
retire-evidence: migration count or header grammar outgrowing about 120 lines
switch: none
---
# Versioned SQL migrations, run by art migrate, schema live before code

## Context and Problem Statement

Schema changes reached the live database by hand. One contract migration
(`016_drop_persistent_scope`) broke every persistent read, because code still
named the dropped column.

## Decision Drivers

* Schema must be live before the code that needs it reaches main
* Destructive steps must come after nothing reads the old shape

## Considered Options

* Ordered SQL files, `schema_migrations` with checksums, an advisory lock, expand/migrate/contract, run by `art migrate` (~120 lines)
* Alembic: one dependency for what is a directory listing and a table

## Decision Outcome

Chosen option: "versioned files and `art migrate`", with a real test database
and contract steps only once no code still names the old shape (the 016 lesson).

### Consequences

* Good, because every change is a reviewable file applied once, in order.
* Bad, because a shadow database is still optional; worth standing up before
  the first backfill on a large table.

### Confirmation

`src/arteries/migrate.py`, `migrations/`.
