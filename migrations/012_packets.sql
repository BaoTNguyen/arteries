-- Finding 19: nothing chains. `previousSummary` is read for dedupe and never
-- written back, and no record exists of which rows entered which packet -- so a
-- packet cannot be incremental, and the same claim is re-sent every turn of a
-- session for as long as it keeps matching.
--
-- Recording membership also gives the compiler something it has never had: which
-- claims were actually surfaced, as opposed to which ones were stored.
CREATE TABLE IF NOT EXISTS arteries.packets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  TEXT NOT NULL,
    session_id  TEXT,
    agent_process_id TEXT,
    member_ids  TEXT[] NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The read is always "this session, recently", so that is the index.
CREATE INDEX IF NOT EXISTS idx_packets_session
    ON arteries.packets (project_id, session_id, created_at DESC);
