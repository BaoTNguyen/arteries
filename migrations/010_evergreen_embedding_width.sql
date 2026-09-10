-- Split out of 009 after 009 had already been applied to the live database.
-- Editing an applied migration is the one thing `art migrate status` refuses to
-- let pass quietly, and rewriting history another checkout has run is exactly
-- what that refusal is for. So the guard lands as its own version.
--
-- The embedding width is templated from EMBED_DIM, so ADD COLUMN gets it right
-- on a fresh table. On a pre-existing one it is a no-op and whatever width is
-- already there survives -- which is how capillaries moved to 1024 while
-- arteries stayed at 768 once already (config.py:30-45). Live happens to be
-- 1024 here; a rehearsal against a 768 table failed every insert with a
-- DataException, which is what this would have looked like in production.
--
-- Safe to correct only while the table is empty, and it always is: the tier has
-- never held a row.
DO $$
DECLARE actual TEXT;
BEGIN
    SELECT format_type(a.atttypid, a.atttypmod) INTO actual
    FROM pg_attribute a
    WHERE a.attrelid = 'arteries.evergreen'::regclass AND a.attname = 'embedding';

    IF actual IS NOT NULL AND actual <> 'vector(EMBED_DIM)'
       AND NOT EXISTS (SELECT 1 FROM arteries.evergreen) THEN
        ALTER TABLE arteries.evergreen DROP COLUMN embedding;
        ALTER TABLE arteries.evergreen ADD COLUMN embedding VECTOR(EMBED_DIM);
    END IF;
END $$;

