-- The arbiter. Separate from 006 because a unique index has to be created after
-- the column it indexes exists and is populated by the code that writes it --
-- one migration doing both would index a column nothing had written yet.
--
-- `coalesce(session_id, '')` because NULL never equals NULL, so a bare
-- `session_id` column would make the index vacuous for exactly the rows that
-- have no session -- which is every row `main` writes.
--
-- Partial on `fact_hash IS NOT NULL` so pre-006 rows stay outside it, and on
-- `valid_until IS NULL` so a tombstoned claim does not block its own
-- replacement.
CREATE UNIQUE INDEX IF NOT EXISTS idx_eph_dedupe
    ON arteries.ephemeral (project_id, coalesce(session_id, ''), fact_hash)
    WHERE valid_until IS NULL AND fact_hash IS NOT NULL;
