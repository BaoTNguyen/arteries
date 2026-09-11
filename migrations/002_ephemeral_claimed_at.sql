-- Finding 1: the stale-claim sweep measured `source_ts`, when the row was
-- written, not when it was claimed. A row that queued longer than
-- STALE_CLAIM_MINUTES was born stale, so the next pass released it while the
-- holding worker was still mid-generation and both wrote. 70 ephemeral rows
-- contributed to persistent facts written in more than one pass.
--
-- Additive: `main` never names this column, so it cannot see it.
ALTER TABLE arteries.ephemeral ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

-- Partial: only claimed rows are ever swept, and they are a handful against
-- hundreds. Nothing scans this index for uncompiled or cleared rows.
CREATE INDEX IF NOT EXISTS idx_ephemeral_claimed
    ON arteries.ephemeral (claimed_at)
    WHERE status = 'compiling';
