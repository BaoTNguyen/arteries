-- Finding 7: nothing decays. `access_count` is tracked and never acted on, so
-- write-side filtering is the only thing bounding the store -- and write-side
-- filtering is never perfect, which is why read-side decay exists.
--
-- The clock is activity days, not wall-clock days. Wall clock punishes being
-- away: come back after three weeks off and a working set that was never wrong
-- has aged out on a calendar nobody was reading. An activity day is a day this
-- project was actually worked on, so retention measures sessions rather than
-- weekends.
CREATE TABLE IF NOT EXISTS arteries.project_activity (
    project_id  TEXT NOT NULL,
    day         DATE NOT NULL,
    PRIMARY KEY (project_id, day)
);

-- Which activity day a claim was last useful on. Written at promotion and bumped
-- when a row is surfaced, so "unused for N activity days" is answerable without
-- a join per row.
ALTER TABLE arteries.persistent ADD COLUMN IF NOT EXISTS last_activity_day INT;

CREATE INDEX IF NOT EXISTS idx_persistent_activity
    ON arteries.persistent (project_id, last_activity_day)
    WHERE valid_until IS NULL;
