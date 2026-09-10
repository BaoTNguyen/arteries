-- Dedupe as a database constraint rather than application code. Two sessions
-- writing the same sentence resolve inside a unique index -- no lock, no
-- read-then-write, and the loser gets a counter bump instead of a second row.
--
-- `fact_hash` is left NULL on existing rows on purpose. NULLs never conflict, so
-- the 737 rows already here are simply outside the index, and there is no
-- backfill to get wrong. The alternative was reimplementing normalize.py in SQL
-- so old hashes matched new ones; if the two implementations ever disagreed by a
-- character, the failure is a fact silently not stored. Dedupe applies going
-- forward, which is the only place it can do any good anyway.
--
-- Additive: `main` never names these columns.
ALTER TABLE arteries.ephemeral ADD COLUMN IF NOT EXISTS fact_hash TEXT;
ALTER TABLE arteries.ephemeral ADD COLUMN IF NOT EXISTS seen_count INT NOT NULL DEFAULT 1;
ALTER TABLE arteries.ephemeral ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ;
