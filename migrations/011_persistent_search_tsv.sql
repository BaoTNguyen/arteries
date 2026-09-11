-- The lexical channel. Memory queries in a coding session are full of exact
-- identifiers -- frame_compat.py:76, UndefinedColumn, EMBED_DIM -- and an
-- embedding compresses those into the same 0.45-0.55 band as all other technical
-- prose. That is why the measured top hit per query sits at p50 0.55, exactly on
-- the floor: dense retrieval cannot see the one token that would have settled it.
--
-- Generated rather than triggered: a stored generated column cannot drift from
-- the row it describes, and there is no trigger to forget to install on a fresh
-- database.
--
-- 'english' matches how capillaries indexes, so the two stacks stem alike.
ALTER TABLE arteries.persistent
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(fact, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_persistent_search
    ON arteries.persistent USING gin (search_tsv);
