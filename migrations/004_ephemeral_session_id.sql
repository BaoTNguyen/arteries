-- Finding 9: ephemeral is keyed on `agent_process_id`, which falls back to
-- str(os.getpid()) when ARTERIES_AGENT_ID is unset. 84 of 717 rows carry a bare
-- PID across 83 distinct ids -- one row each, and no later process will ever
-- match a dead PID. Those rows are unretrievable by `get_ephemeral` and
-- unclaimable by `_claim_ephemeral`, permanently.
--
-- A session id is stable across the turns of one session and survives the
-- process dying, which is what both paths actually need. Hooks already export
-- ARTERIES_SESSION_ID (cli_normalize.py:117); nothing read it here.
--
-- Additive: `main` never names this column.
ALTER TABLE arteries.ephemeral ADD COLUMN IF NOT EXISTS session_id TEXT;

CREATE INDEX IF NOT EXISTS idx_ephemeral_session
    ON arteries.ephemeral (project_id, session_id)
    WHERE status = 'uncompiled';
