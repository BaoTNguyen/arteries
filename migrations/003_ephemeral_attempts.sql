-- Finding 10/13: a pass that fails releases its claim, and the next pass claims
-- the same rows and fails the same way. With llama-server down that is an
-- unbounded retry loop writing two queue events per turn for the whole outage.
-- Counting attempts is what makes a poison batch stop being retried forever.
--
-- Additive: `main` never names these columns.
ALTER TABLE arteries.ephemeral ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0;
ALTER TABLE arteries.ephemeral ADD COLUMN IF NOT EXISTS quarantined_at TIMESTAMPTZ;

-- The claim query filters on this, so it earns an index. Partial for the same
-- reason as idx_ephemeral_claimed: only uncompiled rows are ever claimed.
CREATE INDEX IF NOT EXISTS idx_ephemeral_attempts
    ON arteries.ephemeral (project_id, attempts)
    WHERE status = 'uncompiled' AND quarantined_at IS NULL;
