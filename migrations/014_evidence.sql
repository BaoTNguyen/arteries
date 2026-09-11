-- Finding 22: the evidence ladder has one rung. Every claim is `stated` --
-- something was said in a session. Nothing is `observed`: the test actually
-- passed, the file actually existed, the command actually exited 0. So when two
-- claims contradict, the only tiebreak is recency, and a confident wrong claim
-- beats a quiet correct one.
--
--     user > observed > stated > inferred
--
-- `origin` is a different question and gets a different column elsewhere: origin
-- is which door a row came through, evidence is how strongly it is known. A
-- PostToolUse observation enters through the conversation door carrying
-- `observed`.
ALTER TABLE arteries.persistent ADD COLUMN IF NOT EXISTS evidence TEXT NOT NULL DEFAULT 'stated';
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS evidence TEXT NOT NULL DEFAULT 'stated';

-- Contradiction resolution orders on this, so it is indexed where that ordering
-- happens.
CREATE INDEX IF NOT EXISTS idx_persistent_evidence
    ON arteries.persistent (project_id, evidence)
    WHERE valid_until IS NULL;
