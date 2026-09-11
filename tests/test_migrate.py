"""The migration runner, and the invariant it exists to protect.

Two checkouts share one database. A migration that cannot be rehearsed is a
migration tested in production, which is how `persistent.scope` got renamed out
from under the live checkout (reverted in d40ff8e).
"""

import unittest
from pathlib import Path

import pytest

from arteries import migrate


class FileTests(unittest.TestCase):
    def test_migrations_are_ordered_by_filename(self):
        versions = [v for v, _sql in migrate.available()]
        self.assertEqual(versions, sorted(versions))

    def test_every_migration_is_numbered(self):
        for version, _sql in migrate.available():
            self.assertRegex(version, r"^\d{3}_", f"{version} has no ordering prefix")

    def test_vector_width_is_rendered_not_literal(self):
        """setup_db.py templates VECTOR(EMBED_DIM) before executing. A runner
        that ships raw SQL to Postgres sends the literal string and gets a
        syntax error, so any migration adding a vector column fails outright."""
        from arteries.config import EMBED_DIM

        self.assertIn(f"VECTOR({EMBED_DIM})",
                      migrate.render("ALTER TABLE x ADD COLUMN e VECTOR(EMBED_DIM)"))

    def test_no_migration_hardcodes_an_embedding_width(self):
        for version, sql in migrate.available():
            self.assertNotRegex(sql, r"VECTOR\(\d+\)",
                                f"{version} pins a width instead of templating it")

    def test_directives_are_read_from_the_first_line(self):
        self.assertIn("no-transaction", migrate._directives("-- no-transaction\nCREATE INDEX"))
        self.assertIn("destructive", migrate._directives("-- destructive\nDROP COLUMN x"))
        self.assertEqual(migrate._directives("ALTER TABLE x ADD COLUMN y INT"), set())

    def test_editing_an_applied_migration_changes_its_checksum(self):
        """`status` reports CHANGED so history cannot be rewritten quietly under
        a checkout that already ran it."""
        self.assertNotEqual(migrate._checksum("ALTER TABLE a ADD COLUMN b INT"),
                            migrate._checksum("ALTER TABLE a ADD COLUMN c INT"))


class DriftTests(unittest.TestCase):
    """schema.sql and migrations/ must describe the same database.

    They drift the moment someone edits one and not the other, and the drift is
    invisible until a fresh checkout builds a schema the migrations never
    produce. `art setup` applies schema.sql, so the invariant is: applying every
    migration on top of a fresh schema.sql must be a no-op that leaves nothing
    pending.
    """

    @pytest.fixture(autouse=True)
    def _db(self, test_db):
        self.db = test_db

    def test_migrations_apply_cleanly_on_top_of_schema_sql(self):
        self.assertEqual(migrate.apply(), [])

    def test_nothing_is_pending_after_baseline(self):
        pending = [v for v, state in migrate.status() if state != "applied"]
        self.assertEqual(pending, [])

    def test_baseline_is_idempotent(self):
        self.assertEqual(migrate.baseline(through=migrate.available()[-1][0]), 0)

    def test_a_destructive_migration_is_refused_without_contract(self, tmp_path=None):
        """Destructive changes wait for a contract migration, after main is on
        the new code. This is the rule d40ff8e was written to enforce."""
        original = migrate.MIGRATIONS_DIR
        staged = Path(original).parent / "migrations_test_destructive"
        staged.mkdir(exist_ok=True)
        (staged / "999_drop.sql").write_text("-- destructive\nSELECT 1;\n")
        migrate.MIGRATIONS_DIR = staged
        try:
            with self.assertRaises(RuntimeError) as caught:
                migrate.apply(contract=False)
            self.assertIn("destructive", str(caught.exception))
        finally:
            migrate.MIGRATIONS_DIR = original
            (staged / "999_drop.sql").unlink()
            staged.rmdir()


class RefusalTests(unittest.TestCase):
    """A refusal is a decision the operator has to make, so it has to be
    readable. Both refusals used to arrive as a traceback with the sentence
    explaining them buried in the middle."""

    def test_an_edited_migration_refuses_with_a_message(self):
        import io
        from contextlib import redirect_stderr
        from unittest.mock import patch

        err = io.StringIO()
        with patch.object(migrate, "apply", side_effect=RuntimeError("boom")), \
             redirect_stderr(err):
            code = migrate.main(["apply"])
        self.assertEqual(code, 1)
        self.assertIn("refused: boom", err.getvalue())


class BaselineScopeTests(unittest.TestCase):
    """`baseline` names a cutoff because the two ways of getting it wrong are
    not symmetric. Stamp too few and a migration re-runs, which IF NOT EXISTS
    usually survives. Stamp too many and a migration never runs at all, and the
    column it was going to add is missing with nothing saying so."""

    def test_baseline_requires_a_cutoff(self):
        import io
        from contextlib import redirect_stderr

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            migrate.main(["baseline"])

    def test_an_unknown_cutoff_is_refused(self):
        with self.assertRaises(RuntimeError) as caught:
            migrate.baseline("999_not_a_migration")
        self.assertIn("unknown migration", str(caught.exception))


class PartialApplicationTests(unittest.TestCase):
    """A refusal used to report only itself. A run that applied 015 and refused
    016 printed "refused" and nothing else, so the operator had no way to know
    half the work had landed -- silent partial success, in the runner built to
    prevent exactly that."""

    def test_a_refusal_carries_what_it_applied_first(self):
        refused = migrate.Refused("no", applied=["015_x"])
        self.assertEqual(refused.applied, ["015_x"])

    def test_the_cli_prints_both(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch

        out, err = io.StringIO(), io.StringIO()
        with patch.object(migrate, "apply",
                          side_effect=migrate.Refused("destructive", ["015_x"])), \
             redirect_stdout(out), redirect_stderr(err):
            code = migrate.main(["apply"])
        self.assertEqual(code, 1)
        self.assertIn("applied 1: 015_x", out.getvalue())
        self.assertIn("refused: destructive", err.getvalue())

    def test_a_refusal_with_nothing_applied_says_only_that(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch

        out, err = io.StringIO(), io.StringIO()
        with patch.object(migrate, "apply", side_effect=migrate.Refused("edited")), \
             redirect_stdout(out), redirect_stderr(err):
            migrate.main(["apply"])
        self.assertNotIn("applied", out.getvalue())
        self.assertIn("refused: edited", err.getvalue())


class ContractMigrationTests(unittest.TestCase):
    """expand / migrate / contract, with the contract half staged and refused."""

    def test_the_drop_is_marked_destructive(self):
        by_version = dict(migrate.available())
        drop = by_version.get("016_drop_persistent_scope")
        self.assertIsNotNone(drop, "the contract migration is missing")
        self.assertIn("destructive", migrate._directives(drop))

    def test_the_backfill_is_not(self):
        by_version = dict(migrate.available())
        self.assertNotIn("destructive",
                         migrate._directives(by_version["015_persistent_origin"]))

    def test_the_drop_names_its_precondition(self):
        """d40ff8e reverted this exact change because it ran before main could
        read the new location. The file has to say so."""
        by_version = dict(migrate.available())
        self.assertIn("main", by_version["016_drop_persistent_scope"])
