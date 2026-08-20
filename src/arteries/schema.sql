-- Arteries memory storage
-- Shares the capillaries Postgres instance, own schema.

CREATE SCHEMA IF NOT EXISTS arteries;
CREATE EXTENSION IF NOT EXISTS vector;

-- Ephemeral: per-agent-process, per-project. High churn.
CREATE TABLE IF NOT EXISTS arteries.ephemeral (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact            TEXT NOT NULL,
    embedding       VECTOR(EMBED_DIM),
    domains         JSONB NOT NULL DEFAULT '[]',
    confidence      REAL NOT NULL DEFAULT 1.0,
    source_ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    access_count    INT NOT NULL DEFAULT 0,
    project_id      TEXT NOT NULL,
    agent_process_id TEXT NOT NULL,
    parent_agent_id  TEXT,
    source          TEXT NOT NULL DEFAULT 'user',        -- user | assistant
    status          TEXT NOT NULL DEFAULT 'uncompiled',  -- uncompiled | compiling | cleared
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_eph_agent
    ON arteries.ephemeral (project_id, agent_process_id)
    WHERE status = 'uncompiled';

ALTER TABLE arteries.ephemeral ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'user';

CREATE INDEX IF NOT EXISTS idx_eph_domains
    ON arteries.ephemeral USING gin (domains);

-- Persistent: per-project, shared across agents. Compiled from ephemeral.
CREATE TABLE IF NOT EXISTS arteries.persistent (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact            TEXT NOT NULL,
    embedding       VECTOR(EMBED_DIM),
    domains         JSONB NOT NULL DEFAULT '[]',
    confidence      REAL NOT NULL DEFAULT 1.0,
    source_ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    access_count    INT NOT NULL DEFAULT 0,
    project_id      TEXT NOT NULL,
    source_project_id TEXT,
    parent_ids      UUID[] DEFAULT '{}',   -- lineage: ephemeral records compiled from
    child_ids       UUID[] DEFAULT '{}',   -- lineage: evergreen records compiled into
    scope           TEXT,             -- NULL = auto-compiled, 'user' = art remember
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until     TIMESTAMPTZ
);

ALTER TABLE arteries.persistent ADD COLUMN IF NOT EXISTS scope TEXT;

CREATE INDEX IF NOT EXISTS idx_per_project
    ON arteries.persistent (project_id)
    WHERE valid_until IS NULL;

CREATE INDEX IF NOT EXISTS idx_per_domains
    ON arteries.persistent USING gin (domains);

CREATE INDEX IF NOT EXISTS idx_per_embedding
    ON arteries.persistent USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Evergreen: global, cross-project. Promoted from persistent.
CREATE TABLE IF NOT EXISTS arteries.evergreen (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact            TEXT NOT NULL,
    embedding       VECTOR(EMBED_DIM),
    domains         JSONB NOT NULL DEFAULT '[]',
    confidence      REAL NOT NULL DEFAULT 1.0,
    source_ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    access_count    INT NOT NULL DEFAULT 0,
    parent_ids      UUID[] DEFAULT '{}',   -- lineage: persistent records compiled from
    superseded_by   UUID,                  -- overwrite-with-lineage for contradictions
    source_meta     JSONB NOT NULL DEFAULT '{}'
);

ALTER TABLE arteries.evergreen
    ADD COLUMN IF NOT EXISTS source_meta JSONB NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_evg_domains
    ON arteries.evergreen USING gin (domains);

CREATE INDEX IF NOT EXISTS idx_evg_embedding
    ON arteries.evergreen USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_evg_source_meta
    ON arteries.evergreen USING gin (source_meta);

-- Retrieval log: tracks what was surfaced and whether it was used.
-- Feeds prior_retrievals in the MemoryFrame and RLVR reward signal.
CREATE TABLE IF NOT EXISTS arteries.retrievals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      TEXT NOT NULL,
    agent_process_id TEXT NOT NULL,
    prompt_id       TEXT NOT NULL,
    situation       TEXT NOT NULL,
    score           REAL NOT NULL,
    relevance       REAL NOT NULL DEFAULT 1.0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ret_agent
    ON arteries.retrievals (project_id, agent_process_id, created_at DESC);

-- Run telemetry: shared append-only events for debugging and offline eval.
CREATE TABLE IF NOT EXISTS arteries.agent_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      TEXT NOT NULL,
    repo_path       TEXT,
    cli             TEXT,
    agent_id        TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    metadata        JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_project
    ON arteries.agent_runs (project_id, started_at DESC);

CREATE TABLE IF NOT EXISTS arteries.agent_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID REFERENCES arteries.agent_runs(id),
    project_id      TEXT NOT NULL,
    turn_id         TEXT,
    event_type      TEXT NOT NULL,
    source          TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_events_project_created
    ON arteries.agent_events (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_events_run_created
    ON arteries.agent_events (run_id, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_agent_events_type
    ON arteries.agent_events (event_type, created_at DESC);

-- Full-text search over observed turn previews (`art search`)
CREATE INDEX IF NOT EXISTS idx_agent_events_search
    ON arteries.agent_events
    USING gin (to_tsvector('english',
        coalesce(payload->>'message_preview', '') || ' ' ||
        coalesce(payload->>'assistant_preview', '')));

-- Decision/action ledger: events say what happened; decisions say what was
-- available, what was chosen, and what it cost. episode ids are TEXT because
-- heart generates its own (timestamp-hex) episode identifiers.

CREATE TABLE IF NOT EXISTS arteries.episodes (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    agent_id        TEXT,
    task_id         TEXT,
    run_id          UUID,
    status          TEXT NOT NULL DEFAULT 'running',
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS arteries.decisions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id        TEXT,
    run_id            UUID,
    turn_id           TEXT,
    project_id        TEXT NOT NULL,
    agent_id          TEXT,
    decision_type     TEXT NOT NULL,
    observation       JSONB NOT NULL DEFAULT '{}',
    available_actions JSONB NOT NULL DEFAULT '[]',
    chosen_action     TEXT NOT NULL,
    cost              JSONB NOT NULL DEFAULT '{}',
    metadata          JSONB NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS arteries.rewards (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id     TEXT,
    decision_id    UUID,
    run_id         UUID,
    project_id     TEXT NOT NULL,
    reward_type    TEXT NOT NULL,
    value          REAL NOT NULL,
    components     JSONB NOT NULL DEFAULT '{}',
    source         TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_episodes_project_created
    ON arteries.episodes (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_decisions_episode_created
    ON arteries.decisions (episode_id, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_decisions_type_created
    ON arteries.decisions (decision_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_rewards_episode_created
    ON arteries.rewards (episode_id, created_at ASC);
