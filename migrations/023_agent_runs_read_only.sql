-- destructive
-- pulse P5 (DIRECTION A4): a run is its run.started / run.resumed / run.ended
-- events, and arteries.agent_runs stays as a read-only archive the lifecycle
-- readers fall back to for runs older than those events. Contract-shaped like
-- 020/021: apply only once every live checkout has stopped writing it.
DROP TRIGGER IF EXISTS read_only_archive ON arteries.agent_runs;
CREATE TRIGGER read_only_archive BEFORE INSERT OR UPDATE OR DELETE ON arteries.agent_runs
    FOR EACH ROW EXECUTE FUNCTION arteries.archive_read_only();
