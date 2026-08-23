-- Arteries memory storage
-- Shares the capillaries Postgres instance, own schema.

CREATE SCHEMA IF NOT EXISTS arteries;
CREATE EXTENSION IF NOT EXISTS vector;

-- Scope: which repos share a memory. `scope_members` is the source of truth;
-- reads resolve it with a CTE rather than a denormalized column, so regrouping
-- a repo is one UPDATE and no row can disagree with its group.
CREATE TABLE IF NOT EXISTS arteries.scopes (
    scope_id    TEXT PRIMARY KEY,
    label       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS arteries.scope_members (
    project_id  TEXT PRIMARY KEY,   -- the ARTERIES_PROJECT value
    scope_id    TEXT NOT NULL REFERENCES arteries.scopes(scope_id) ON DELETE CASCADE,
    repo_path   TEXT NOT NULL,      -- absolute; tracking resolves by path prefix
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scope_members_scope
    ON arteries.scope_members (scope_id);

-- Ephemeral: per-agent-process, per-project. High churn.
CREATE TABLE IF NOT EXISTS arteries.ephemeral (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact            TEXT NOT NULL,
    embedding       VECTOR(EMBED_DIM),
    domains         JSONB NOT NULL DEFAULT '[]',
    confidence      REAL NOT NULL DEFAULT 1.0,   -- unused here; the main checkout reads it
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
-- Kept, though this branch no longer writes it: the main checkout still selects
-- it, and both run against the same database. Drop it when main has moved.
ALTER TABLE arteries.ephemeral ADD COLUMN IF NOT EXISTS confidence REAL NOT NULL DEFAULT 1.0;

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
    scope           TEXT,             -- NULL = compiled | 'user' = art remember | 'reviewed' = art docs
    source_meta     JSONB NOT NULL DEFAULT '{}',
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until     TIMESTAMPTZ
);

-- Named `scope` for provenance (compiled | user | reviewed), which unhappily
-- collides with scope *groups*. Renaming it was attempted and reverted: the
-- main checkout reads this column against the same database, so a rename from
-- a feature branch breaks whatever branch is actually running.
ALTER TABLE arteries.persistent ADD COLUMN IF NOT EXISTS scope TEXT;
-- fact | decision | preference | constraint
ALTER TABLE arteries.persistent ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'fact';
-- Document provenance: path, line span, and digest for facts imported
-- through the `art docs` review flow. Was evergreen.source_meta.
ALTER TABLE arteries.persistent ADD COLUMN IF NOT EXISTS source_meta JSONB NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_per_project
    ON arteries.persistent (project_id)
    WHERE valid_until IS NULL;

CREATE INDEX IF NOT EXISTS idx_per_domains
    ON arteries.persistent USING gin (domains);

CREATE INDEX IF NOT EXISTS idx_per_embedding
    ON arteries.persistent USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

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
    -- What the episode actually cost. actionlog.log_reward has written these
    -- since 972bda2, which added them to the live database by hand and left
    -- schema.sql for later -- so every fresh install had an INSERT naming
    -- columns that did not exist.
    tokens_in      INT,
    tokens_out     INT,
    cost_usd       NUMERIC(12, 6),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE arteries.rewards ADD COLUMN IF NOT EXISTS tokens_in  INT;
ALTER TABLE arteries.rewards ADD COLUMN IF NOT EXISTS tokens_out INT;
ALTER TABLE arteries.rewards ADD COLUMN IF NOT EXISTS cost_usd   NUMERIC(12, 6);

CREATE INDEX IF NOT EXISTS idx_episodes_project_created
    ON arteries.episodes (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_decisions_episode_created
    ON arteries.decisions (episode_id, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_decisions_type_created
    ON arteries.decisions (decision_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_rewards_episode_created
    ON arteries.rewards (episode_id, created_at ASC);

-- ============================================================================
-- Knowledge graph: T-Box (the ontology) and A-Box (entities, edges, documents)
--
-- The tiers above stay tables with vectors. This is the layer that gives them
-- structure: canonical entities instead of restated strings, typed edges
-- instead of UUID[] columns, and provenance that survives a supersede.
-- ============================================================================

-- T-Box: the ontology, cached from an RDF file so nothing at runtime needs
-- rdflib. `art ontology load` writes this; everything else only reads it.
CREATE TABLE IF NOT EXISTS arteries.ontology_terms (
    uri             TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    normalized      TEXT NOT NULL,   -- lowercased, underscored: the match key
    parent_uri      TEXT,            -- rdfs:subClassOf / subPropertyOf, for BFS
    kind            TEXT NOT NULL DEFAULT 'class',  -- class | property | individual
    aliases         TEXT[] NOT NULL DEFAULT '{}',  -- extra match keys (URI fragment, altLabels)
    comment         TEXT,
    source          TEXT NOT NULL,   -- which ontology file it came from
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_onto_normalized ON arteries.ontology_terms (normalized);
CREATE INDEX IF NOT EXISTS idx_onto_kind ON arteries.ontology_terms (kind, normalized);
CREATE INDEX IF NOT EXISTS idx_onto_parent ON arteries.ontology_terms (parent_uri);

-- Must precede the index below: CREATE TABLE IF NOT EXISTS is a no-op on an
-- existing table, so a database created before aliases existed has no column
-- for the index to build on.
ALTER TABLE arteries.ontology_terms
    ADD COLUMN IF NOT EXISTS aliases TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_onto_aliases ON arteries.ontology_terms USING gin (aliases);

-- Entities: the canonical names a claim can be about. The UNIQUE constraint is
-- first-pass entity resolution -- two extractions that normalize to the same
-- name become one node. ontology_valid records whether the T-Box recognised it,
-- exactly as cognee flags it: unmatched entities are kept, not dropped.
CREATE TABLE IF NOT EXISTS arteries.entities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Scoped to the group, not the repo: "hook" means the CLI hook in a harness
    -- and useEffect in a React app, and a shared node would have to pick one
    -- ontology class and blend both meanings into one embedding.
    scope_id        TEXT NOT NULL,
    name            TEXT NOT NULL,   -- canonical (ontology label when matched)
    raw_name        TEXT NOT NULL,   -- what the extractor actually said
    kind            TEXT NOT NULL DEFAULT 'concept',  -- concept | module | dependency
    ontology_class  TEXT,            -- URI into ontology_terms, NULL if unmatched
    ontology_valid  BOOLEAN NOT NULL DEFAULT false,
    match_score     REAL,            -- difflib ratio that produced the match
    embedding       VECTOR(EMBED_DIM),
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'arteries' AND table_name = 'entities'
                 AND column_name = 'project_id') THEN
        DROP INDEX IF EXISTS arteries.idx_entities_canonical;
        ALTER TABLE arteries.entities RENAME COLUMN project_id TO scope_id;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_canonical
    ON arteries.entities (scope_id, kind, lower(name));
CREATE INDEX IF NOT EXISTS idx_entities_class ON arteries.entities (ontology_class);

-- Documents and chunks. These are *project docs that become claims*, not the
-- prompt corpus -- capillaries owns public.prompt_chunks for that.
CREATE TABLE IF NOT EXISTS arteries.documents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      TEXT NOT NULL,
    path            TEXT NOT NULL,
    digest          TEXT NOT NULL,   -- skip re-ingest when unchanged
    kind            TEXT NOT NULL DEFAULT 'markdown',
    metadata        JSONB NOT NULL DEFAULT '{}',
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_path
    ON arteries.documents (project_id, path);

CREATE TABLE IF NOT EXISTS arteries.chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES arteries.documents(id) ON DELETE CASCADE,
    project_id      TEXT NOT NULL,
    ord             INT NOT NULL,
    text            TEXT NOT NULL,
    line_start      INT,
    line_end        INT,
    embedding       VECTOR(EMBED_DIM)
);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON arteries.chunks (document_id, ord);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON arteries.chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Typed edges over every node kind above. src/dst ids are TEXT because the
-- things being linked have heterogeneous key types: UUID for memories and
-- entities, TEXT for episodes and prompt ids.
CREATE TABLE IF NOT EXISTS arteries.memory_edges (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      TEXT NOT NULL,
    src_kind        TEXT NOT NULL,   -- ephemeral|persistent|evergreen|entity|chunk|document
    src_id          TEXT NOT NULL,
    dst_kind        TEXT NOT NULL,
    dst_id          TEXT NOT NULL,
    rel             TEXT NOT NULL,   -- derived_from|supersedes|mentions|is_a|is_part_of...
    weight          REAL NOT NULL DEFAULT 1.0,   -- decays traversal score per hop
    ontology_valid  BOOLEAN NOT NULL DEFAULT false,
    metadata        JSONB NOT NULL DEFAULT '{}', -- e.g. {"reason": ...} on supersedes
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until     TIMESTAMPTZ      -- same tombstone convention as persistent
);

CREATE INDEX IF NOT EXISTS idx_edges_src
    ON arteries.memory_edges (project_id, src_kind, src_id) WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_dst
    ON arteries.memory_edges (project_id, dst_kind, dst_id) WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_rel
    ON arteries.memory_edges (project_id, rel) WHERE valid_until IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_unique
    ON arteries.memory_edges (project_id, src_kind, src_id, rel, dst_kind, dst_id)
    WHERE valid_until IS NULL;
