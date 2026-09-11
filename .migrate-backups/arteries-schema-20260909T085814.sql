--
-- PostgreSQL database dump
--

\restrict 8qgtoH9KVncqxIbkGSLQo76zUwc390UTdeRiKiEBVbY4wLv0jZI2ukM1xdOYIzX

-- Dumped from database version 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: arteries; Type: SCHEMA; Schema: -; Owner: bao-tn
--

CREATE SCHEMA arteries;


ALTER SCHEMA arteries OWNER TO "bao-tn";

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_events; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.agent_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_id uuid,
    project_id text NOT NULL,
    turn_id text,
    event_type text NOT NULL,
    source text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE arteries.agent_events OWNER TO "bao-tn";

--
-- Name: agent_runs; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.agent_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id text NOT NULL,
    repo_path text,
    cli text,
    agent_id text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


ALTER TABLE arteries.agent_runs OWNER TO "bao-tn";

--
-- Name: chunks; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.chunks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    project_id text NOT NULL,
    ord integer NOT NULL,
    text text NOT NULL,
    line_start integer,
    line_end integer,
    embedding public.vector(1024)
);


ALTER TABLE arteries.chunks OWNER TO "bao-tn";

--
-- Name: decisions; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.decisions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    episode_id text,
    run_id uuid,
    turn_id text,
    project_id text NOT NULL,
    agent_id text,
    decision_type text NOT NULL,
    observation jsonb DEFAULT '{}'::jsonb NOT NULL,
    available_actions jsonb DEFAULT '[]'::jsonb NOT NULL,
    chosen_action text NOT NULL,
    cost jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE arteries.decisions OWNER TO "bao-tn";

--
-- Name: documents; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.documents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id text NOT NULL,
    path text NOT NULL,
    digest text NOT NULL,
    kind text DEFAULT 'markdown'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE arteries.documents OWNER TO "bao-tn";

--
-- Name: entities; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.entities (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    scope_id text NOT NULL,
    name text NOT NULL,
    raw_name text NOT NULL,
    kind text DEFAULT 'concept'::text NOT NULL,
    ontology_class text,
    ontology_valid boolean DEFAULT false NOT NULL,
    match_score real,
    embedding public.vector(1024),
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE arteries.entities OWNER TO "bao-tn";

--
-- Name: ephemeral; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.ephemeral (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    fact text NOT NULL,
    domains jsonb DEFAULT '[]'::jsonb NOT NULL,
    source_ts timestamp with time zone DEFAULT now() NOT NULL,
    access_count integer DEFAULT 0 NOT NULL,
    project_id text NOT NULL,
    agent_process_id text NOT NULL,
    parent_agent_id text,
    source text DEFAULT 'user'::text NOT NULL,
    status text DEFAULT 'uncompiled'::text NOT NULL,
    valid_from timestamp with time zone DEFAULT now() NOT NULL,
    valid_until timestamp with time zone,
    embedding public.vector(1024),
    confidence real DEFAULT 1.0 NOT NULL,
    episode_id text,
    task_id text
);


ALTER TABLE arteries.ephemeral OWNER TO "bao-tn";

--
-- Name: episodes; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.episodes (
    id text NOT NULL,
    project_id text NOT NULL,
    agent_id text,
    task_id text,
    run_id uuid,
    status text DEFAULT 'running'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone
);


ALTER TABLE arteries.episodes OWNER TO "bao-tn";

--
-- Name: evergreen; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.evergreen (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    fact text NOT NULL,
    embedding public.vector(1024),
    domains jsonb DEFAULT '[]'::jsonb NOT NULL,
    confidence real DEFAULT 1.0 NOT NULL,
    source_ts timestamp with time zone DEFAULT now() NOT NULL,
    access_count integer DEFAULT 0 NOT NULL,
    parent_ids uuid[] DEFAULT '{}'::uuid[],
    superseded_by uuid,
    source_meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


ALTER TABLE arteries.evergreen OWNER TO "bao-tn";

--
-- Name: memory_edges; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.memory_edges (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id text NOT NULL,
    src_kind text NOT NULL,
    src_id text NOT NULL,
    dst_kind text NOT NULL,
    dst_id text NOT NULL,
    rel text NOT NULL,
    weight real DEFAULT 1.0 NOT NULL,
    ontology_valid boolean DEFAULT false NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    valid_until timestamp with time zone
);


ALTER TABLE arteries.memory_edges OWNER TO "bao-tn";

--
-- Name: ontology_terms; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.ontology_terms (
    uri text NOT NULL,
    label text NOT NULL,
    normalized text NOT NULL,
    parent_uri text,
    kind text DEFAULT 'class'::text NOT NULL,
    comment text,
    source text NOT NULL,
    loaded_at timestamp with time zone DEFAULT now() NOT NULL,
    aliases text[] DEFAULT '{}'::text[] NOT NULL
);


ALTER TABLE arteries.ontology_terms OWNER TO "bao-tn";

--
-- Name: persistent; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.persistent (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    fact text NOT NULL,
    domains jsonb DEFAULT '[]'::jsonb NOT NULL,
    confidence real DEFAULT 1.0 NOT NULL,
    source_ts timestamp with time zone DEFAULT now() NOT NULL,
    access_count integer DEFAULT 0 NOT NULL,
    project_id text NOT NULL,
    source_project_id text,
    parent_ids uuid[] DEFAULT '{}'::uuid[],
    child_ids uuid[] DEFAULT '{}'::uuid[],
    scope text,
    valid_from timestamp with time zone DEFAULT now() NOT NULL,
    valid_until timestamp with time zone,
    embedding public.vector(1024),
    source_meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    kind text DEFAULT 'fact'::text NOT NULL,
    episode_id text,
    task_id text
);


ALTER TABLE arteries.persistent OWNER TO "bao-tn";

--
-- Name: retrievals; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.retrievals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id text NOT NULL,
    agent_process_id text NOT NULL,
    prompt_id text NOT NULL,
    situation text NOT NULL,
    score real NOT NULL,
    relevance real DEFAULT 1.0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE arteries.retrievals OWNER TO "bao-tn";

--
-- Name: rewards; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.rewards (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    episode_id text,
    decision_id uuid,
    run_id uuid,
    project_id text NOT NULL,
    reward_type text NOT NULL,
    value real NOT NULL,
    components jsonb DEFAULT '{}'::jsonb NOT NULL,
    source text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tokens_in integer,
    tokens_out integer,
    cost_usd numeric(12,6)
);


ALTER TABLE arteries.rewards OWNER TO "bao-tn";

--
-- Name: schema_migrations; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.schema_migrations (
    version text NOT NULL,
    checksum text NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    baselined boolean DEFAULT false NOT NULL
);


ALTER TABLE arteries.schema_migrations OWNER TO "bao-tn";

--
-- Name: scope_members; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.scope_members (
    project_id text NOT NULL,
    scope_id text NOT NULL,
    repo_path text NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE arteries.scope_members OWNER TO "bao-tn";

--
-- Name: scopes; Type: TABLE; Schema: arteries; Owner: bao-tn
--

CREATE TABLE arteries.scopes (
    scope_id text NOT NULL,
    label text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE arteries.scopes OWNER TO "bao-tn";

--
-- Name: agent_events agent_events_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.agent_events
    ADD CONSTRAINT agent_events_pkey PRIMARY KEY (id);


--
-- Name: agent_runs agent_runs_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.agent_runs
    ADD CONSTRAINT agent_runs_pkey PRIMARY KEY (id);


--
-- Name: chunks chunks_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.chunks
    ADD CONSTRAINT chunks_pkey PRIMARY KEY (id);


--
-- Name: decisions decisions_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.decisions
    ADD CONSTRAINT decisions_pkey PRIMARY KEY (id);


--
-- Name: documents documents_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);


--
-- Name: entities entities_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.entities
    ADD CONSTRAINT entities_pkey PRIMARY KEY (id);


--
-- Name: ephemeral ephemeral_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.ephemeral
    ADD CONSTRAINT ephemeral_pkey PRIMARY KEY (id);


--
-- Name: episodes episodes_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.episodes
    ADD CONSTRAINT episodes_pkey PRIMARY KEY (id);


--
-- Name: evergreen evergreen_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.evergreen
    ADD CONSTRAINT evergreen_pkey PRIMARY KEY (id);


--
-- Name: memory_edges memory_edges_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.memory_edges
    ADD CONSTRAINT memory_edges_pkey PRIMARY KEY (id);


--
-- Name: ontology_terms ontology_terms_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.ontology_terms
    ADD CONSTRAINT ontology_terms_pkey PRIMARY KEY (uri);


--
-- Name: persistent persistent_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.persistent
    ADD CONSTRAINT persistent_pkey PRIMARY KEY (id);


--
-- Name: retrievals retrievals_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.retrievals
    ADD CONSTRAINT retrievals_pkey PRIMARY KEY (id);


--
-- Name: rewards rewards_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.rewards
    ADD CONSTRAINT rewards_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: scope_members scope_members_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.scope_members
    ADD CONSTRAINT scope_members_pkey PRIMARY KEY (project_id);


--
-- Name: scopes scopes_pkey; Type: CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.scopes
    ADD CONSTRAINT scopes_pkey PRIMARY KEY (scope_id);


--
-- Name: idx_agent_events_project_created; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_agent_events_project_created ON arteries.agent_events USING btree (project_id, created_at DESC);


--
-- Name: idx_agent_events_run_created; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_agent_events_run_created ON arteries.agent_events USING btree (run_id, created_at);


--
-- Name: idx_agent_events_search; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_agent_events_search ON arteries.agent_events USING gin (to_tsvector('english'::regconfig, ((COALESCE((payload ->> 'message_preview'::text), ''::text) || ' '::text) || COALESCE((payload ->> 'assistant_preview'::text), ''::text))));


--
-- Name: idx_agent_events_type; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_agent_events_type ON arteries.agent_events USING btree (event_type, created_at DESC);


--
-- Name: idx_agent_runs_project; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_agent_runs_project ON arteries.agent_runs USING btree (project_id, started_at DESC);


--
-- Name: idx_chunks_document; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_chunks_document ON arteries.chunks USING btree (document_id, ord);


--
-- Name: idx_chunks_embedding; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_chunks_embedding ON arteries.chunks USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: idx_decisions_episode_created; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_decisions_episode_created ON arteries.decisions USING btree (episode_id, created_at);


--
-- Name: idx_decisions_type_created; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_decisions_type_created ON arteries.decisions USING btree (decision_type, created_at DESC);


--
-- Name: idx_documents_path; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE UNIQUE INDEX idx_documents_path ON arteries.documents USING btree (project_id, path);


--
-- Name: idx_edges_dst; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_edges_dst ON arteries.memory_edges USING btree (project_id, dst_kind, dst_id) WHERE (valid_until IS NULL);


--
-- Name: idx_edges_rel; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_edges_rel ON arteries.memory_edges USING btree (project_id, rel) WHERE (valid_until IS NULL);


--
-- Name: idx_edges_src; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_edges_src ON arteries.memory_edges USING btree (project_id, src_kind, src_id) WHERE (valid_until IS NULL);


--
-- Name: idx_edges_unique; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE UNIQUE INDEX idx_edges_unique ON arteries.memory_edges USING btree (project_id, src_kind, src_id, rel, dst_kind, dst_id) WHERE (valid_until IS NULL);


--
-- Name: idx_entities_canonical; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE UNIQUE INDEX idx_entities_canonical ON arteries.entities USING btree (scope_id, kind, lower(name));


--
-- Name: idx_entities_class; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_entities_class ON arteries.entities USING btree (ontology_class);


--
-- Name: idx_eph_agent; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_eph_agent ON arteries.ephemeral USING btree (project_id, agent_process_id) WHERE (status = 'uncompiled'::text);


--
-- Name: idx_eph_domains; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_eph_domains ON arteries.ephemeral USING gin (domains);


--
-- Name: idx_eph_task; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_eph_task ON arteries.ephemeral USING btree (project_id, task_id) WHERE (task_id IS NOT NULL);


--
-- Name: idx_episodes_project_created; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_episodes_project_created ON arteries.episodes USING btree (project_id, created_at DESC);


--
-- Name: idx_onto_aliases; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_onto_aliases ON arteries.ontology_terms USING gin (aliases);


--
-- Name: idx_onto_kind; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_onto_kind ON arteries.ontology_terms USING btree (kind, normalized);


--
-- Name: idx_onto_normalized; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_onto_normalized ON arteries.ontology_terms USING btree (normalized);


--
-- Name: idx_onto_parent; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_onto_parent ON arteries.ontology_terms USING btree (parent_uri);


--
-- Name: idx_per_domains; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_per_domains ON arteries.persistent USING gin (domains);


--
-- Name: idx_per_embedding; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_per_embedding ON arteries.persistent USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: idx_per_project; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_per_project ON arteries.persistent USING btree (project_id) WHERE (valid_until IS NULL);


--
-- Name: idx_persistent_task; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_persistent_task ON arteries.persistent USING btree (project_id, task_id) WHERE (task_id IS NOT NULL);


--
-- Name: idx_ret_agent; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_ret_agent ON arteries.retrievals USING btree (project_id, agent_process_id, created_at DESC);


--
-- Name: idx_rewards_episode_created; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_rewards_episode_created ON arteries.rewards USING btree (episode_id, created_at);


--
-- Name: idx_scope_members_scope; Type: INDEX; Schema: arteries; Owner: bao-tn
--

CREATE INDEX idx_scope_members_scope ON arteries.scope_members USING btree (scope_id);


--
-- Name: agent_events agent_events_run_id_fkey; Type: FK CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.agent_events
    ADD CONSTRAINT agent_events_run_id_fkey FOREIGN KEY (run_id) REFERENCES arteries.agent_runs(id);


--
-- Name: chunks chunks_document_id_fkey; Type: FK CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.chunks
    ADD CONSTRAINT chunks_document_id_fkey FOREIGN KEY (document_id) REFERENCES arteries.documents(id) ON DELETE CASCADE;


--
-- Name: scope_members scope_members_scope_id_fkey; Type: FK CONSTRAINT; Schema: arteries; Owner: bao-tn
--

ALTER TABLE ONLY arteries.scope_members
    ADD CONSTRAINT scope_members_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES arteries.scopes(scope_id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict 8qgtoH9KVncqxIbkGSLQo76zUwc390UTdeRiKiEBVbY4wLv0jZI2ukM1xdOYIzX

