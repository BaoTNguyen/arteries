-- Finding 8: compilation marks a row 'cleared' and `get_ephemeral` filters on
-- status = 'uncompiled', so promoting a fact deletes it from the tier that
-- serves in-session recall. Live state when measured: 599 cleared, 28
-- uncompiled -- the entire working set for the project was 28 rows.
--
-- Promotion lag over 2030 rows was bimodal: median 3d 13h, but 0.16s minimum.
-- The multi-day median is backlog from the 44% generator failure rate. When
-- llama-server is healthy a row is visible to its own session for a fraction of
-- a second, which means in-session recall worked only while the compiler was
-- broken.
--
-- `compiled_at` records the promotion without hiding the row. Visibility moves
-- to a time window; `status` keeps its job of telling the compiler what still
-- needs claiming.
--
-- Additive: `main` never names this column.
ALTER TABLE arteries.ephemeral ADD COLUMN IF NOT EXISTS compiled_at TIMESTAMPTZ;

-- Backfill what is already known: a cleared row was compiled, and source_ts is
-- the only timestamp it has. Approximate, and better than NULL -- NULL would
-- read as "never compiled" for 599 rows that were.
UPDATE arteries.ephemeral
SET compiled_at = source_ts
WHERE status = 'cleared' AND compiled_at IS NULL;
