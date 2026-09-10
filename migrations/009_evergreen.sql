-- The evergreen tier: durable, scope-wide claims with a graph over them.
--
-- `arteries.evergreen` has never existed. Three scripts call `python -m
-- arteries.evergreen`, a module deleted with the tier, and `scripts/watch.sh`
-- calls `storage.get_evergreen`, which does not exist either. capillaries'
-- rework doc still says "Arteries already holds durable user facts in
-- arteries.evergreen". None of that was ever true.
--
-- Keyed on `scope_id`, not `project_id`: one graph per scope group, so a
-- constraint recorded in arteries is visible when planning heart. A standalone
-- repo is a scope of one and gets a graph to itself, which reads the same as
-- per-project.
--
-- Rows are tombstoned via `valid_until`, never deleted, so a superseded claim
-- stays answerable as "what did we used to think".
-- An empty table of this name already exists on the live database. d40ff8e
-- recreated it when a feature branch dropped it out from under the main
-- checkout -- "recreated empty, which is precisely what main saw before, since
-- the tier never held a row". It has a different shape: id, fact, embedding,
-- domains, confidence, source_ts, access_count, parent_ids, superseded_by,
-- source_meta.
--
-- So CREATE TABLE IF NOT EXISTS is not enough on its own. It silently does
-- nothing when a *differently shaped* table already has the name, and the index
-- below then fails on columns that do not exist -- which is exactly what
-- happened running this against live the first time.
--
-- Every column is therefore also added individually. Additive, so `main` keeps
-- the columns it knows about even though it reads none of them, and the old ones
-- are left alone rather than dropped: dropping is a contract migration and this
-- is not one.
CREATE TABLE IF NOT EXISTS arteries.evergreen (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id        TEXT,
    fact            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'fact',   -- fact|decision|preference|constraint
    domains         JSONB NOT NULL DEFAULT '[]',
    confidence      REAL NOT NULL DEFAULT 1.0,
    embedding       VECTOR(EMBED_DIM),

    -- Where it came from and how it got in.
    source_project_id TEXT,
    parent_ids      UUID[] NOT NULL DEFAULT '{}',   -- the persistent rows promoted
    -- Inherited from the parents when they agree, so a fact keeps the run that
    -- produced it. RL exclusion needs this, and so does supersede ordering.
    episode_id      TEXT,
    task_id         TEXT,

    -- `core` is the project's own specification: seeded before any conversation,
    -- exempt from the score, exempt from eviction.
    core            BOOLEAN NOT NULL DEFAULT false,
    origin          TEXT NOT NULL DEFAULT 'promoted', -- promoted|authored
    -- NULL, not 0, for a row that never scored. Zero would read as "scored badly
    -- and got in anyway".
    incrementality  REAL,

    access_count    INT NOT NULL DEFAULT 0,
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until     TIMESTAMPTZ
);

-- The same shape again, for the case where the table pre-exists. On a fresh
-- database every one of these is a no-op.
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS scope_id TEXT;
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'fact';
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS source_project_id TEXT;
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS parent_ids UUID[] NOT NULL DEFAULT '{}';
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS episode_id TEXT;
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS task_id TEXT;
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS core BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'promoted';
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS incrementality REAL;
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS access_count INT NOT NULL DEFAULT 0;
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ;
ALTER TABLE arteries.evergreen ADD COLUMN IF NOT EXISTS embedding VECTOR(EMBED_DIM);

-- NOT NULL only once the column is there and the table is empty of rows that
-- would violate it. Skipped rather than failed if anything is already stored.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM arteries.evergreen WHERE scope_id IS NULL) THEN
        ALTER TABLE arteries.evergreen ALTER COLUMN scope_id SET NOT NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_evergreen_scope
    ON arteries.evergreen (scope_id)
    WHERE valid_until IS NULL;

CREATE INDEX IF NOT EXISTS idx_evergreen_embedding
    ON arteries.evergreen USING hnsw (embedding vector_cosine_ops);

-- One row per claim per scope. `fact_hash` would be better but promotion writes
-- the compiler's wording, which is stable per parent, so the parent set is the
-- identity that matters: promoting the same persistent row twice is the bug this
-- prevents.
CREATE UNIQUE INDEX IF NOT EXISTS idx_evergreen_parents
    ON arteries.evergreen (scope_id, parent_ids)
    WHERE valid_until IS NULL AND array_length(parent_ids, 1) > 0;
