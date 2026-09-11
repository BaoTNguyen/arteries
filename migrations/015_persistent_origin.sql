-- Expand and migrate, of expand/migrate/contract. The drop is 016 and will
-- refuse to run until it is asked for explicitly.
--
-- `persistent.scope` holds provenance -- NULL compiled, 'user' from
-- `art remember`, 'reviewed' from `art docs` -- and collides with scope *groups*,
-- the concept evergreen is keyed on. The rename to `origin` was tried in e5d9df1
-- and reverted in d40ff8e, because a feature branch renamed a column the live
-- main checkout was reading and four of main's five read paths started raising
-- UndefinedColumn.
--
-- This is the same change done the way that works. `source_meta` is already on
-- the table, already JSONB, and already carries `art docs` provenance, so the
-- value moves there rather than into a second column: one non-NULL row out of
-- 527 does not earn a column, an index and a migration of its own.
UPDATE arteries.persistent
SET source_meta = source_meta || jsonb_build_object('origin', scope)
WHERE scope IS NOT NULL
  AND NOT (source_meta ? 'origin');
