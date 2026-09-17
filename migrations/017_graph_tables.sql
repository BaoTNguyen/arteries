-- The graph tables schema.sql has and migrations/ never had.
--
-- schema.sql's "Knowledge graph" section grew five tables -- ontology_terms,
-- entities, documents, chunks, memory_edges -- and none of them ever got a
-- migration. A fresh database gets them from `art setup-db`; a database kept
-- current with `art migrate apply` does not, and every graph read path raises
-- UndefinedTable against it. That is precisely the drift the runner exists to
-- prevent, sitting in the runner's own directory.
--
-- Copied verbatim from schema.sql:303-465, minus two compatibility shims that
-- only fire for a database that already has these tables from setup-db (the
-- ontology_terms.aliases backfill and the entities.project_id -> scope_id
-- rename): a database reaching these tables through this migration creates them
-- with the current shape in the first place.

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
    src_kind        TEXT NOT NULL,   -- ephemeral|persistent|evergreen|entity|chunk|document|literal
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
