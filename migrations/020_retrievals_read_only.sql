-- destructive
-- pulse P5 (DIRECTION A2): retrievals are written as `retrieval.logged` events
-- only, and arteries.retrievals stays as a read-only archive the readers still
-- top up from. Not destructive to data, but contract-shaped all the same: run
-- it only once every live checkout has stopped inserting (main included),
-- because the trigger turns a missed writer's INSERT into an error.
CREATE OR REPLACE FUNCTION arteries.archive_read_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is a read-only archive (pulse P5); write an event instead', TG_TABLE_NAME;
END $$;
DROP TRIGGER IF EXISTS read_only_archive ON arteries.retrievals;
CREATE TRIGGER read_only_archive BEFORE INSERT OR UPDATE OR DELETE ON arteries.retrievals
    FOR EACH ROW EXECUTE FUNCTION arteries.archive_read_only();
