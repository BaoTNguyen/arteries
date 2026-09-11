"""The compile claim lease.

Finding 1: the sweep measured `source_ts` -- when the row was written -- so a
row that queued for longer than the threshold was born stale. The next pass
released it while the holding worker was still generating, and both wrote. 70
ephemeral rows contributed to persistent facts written in more than one pass.
"""

import re
import unittest

from arteries import compile as compile_mod


def _sql(func_source: str) -> str:
    return re.sub(r"\s+", " ", func_source)


class LeaseSqlTests(unittest.TestCase):
    """Read the SQL rather than a live table: these are statements about which
    column the lease is measured on, which is exactly what regressed."""

    def setUp(self):
        import inspect

        self.release = _sql(inspect.getsource(compile_mod._release_stale_claims))
        self.claim = _sql(inspect.getsource(compile_mod._claim_ephemeral))
        self.rollback = _sql(inspect.getsource(compile_mod._release_claimed))

    def test_the_sweep_measures_the_claim_not_the_row(self):
        self.assertIn("coalesce(claimed_at, source_ts) <", self.release)

    def test_the_sweep_no_longer_measures_source_ts_alone(self):
        self.assertNotIn("AND source_ts < now()", self.release)

    def test_claiming_stamps_the_lease(self):
        self.assertIn("SET status = 'compiling', claimed_at = now()", self.claim)

    def test_releasing_clears_the_lease(self):
        """A released row that keeps its old claimed_at would be swept again the
        moment it is re-claimed."""
        self.assertIn("claimed_at = NULL", self.release)
        self.assertIn("claimed_at = NULL", self.rollback)

    def test_the_sweep_is_not_scoped_to_the_calling_agent(self):
        """A claim is stranded precisely when its process is gone, so scoping
        the sweep to the caller means nobody ever releases it."""
        # The predicate, not the word: the docstring explains why it is absent.
        self.assertNotIn("agent_process_id =", self.release)


class LeaseDurationTests(unittest.TestCase):
    """Finding 11: at 2 minutes the lease was 120s against a 60s generation
    ceiling, so a pass queued behind llama-server's other slot could be swept
    while still holding."""

    def test_the_lease_is_at_least_three_times_the_generation_ceiling(self):
        lease = compile_mod.STALE_CLAIM_MINUTES * 60
        self.assertGreaterEqual(lease, 3 * compile_mod.COMPILE_TIMEOUT)
