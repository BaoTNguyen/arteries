-- Expand only. pulse P5 (DIRECTION A4): agent_events is a projection of the
-- journal and runs are events, so agent_events.run_id no longer points at a
-- row in agent_runs. The FK made one event naming an unknown run sink its
-- whole batch; the drain worked around it by blanking run ids.
ALTER TABLE arteries.agent_events DROP CONSTRAINT IF EXISTS agent_events_run_id_fkey;
