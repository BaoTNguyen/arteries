-- destructive
-- pulse P5 (DIRECTION A3): decisions are `decision.*` events carrying the
-- whole record, and arteries.decisions stays as a read-only archive that
-- recent_decisions tops up from. Contract-shaped like 020: apply only once
-- every live checkout has stopped inserting.
DROP TRIGGER IF EXISTS read_only_archive ON arteries.decisions;
CREATE TRIGGER read_only_archive BEFORE INSERT OR UPDATE OR DELETE ON arteries.decisions
    FOR EACH ROW EXECUTE FUNCTION arteries.archive_read_only();
