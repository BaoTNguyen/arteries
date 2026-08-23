"""Scope resolution: which repos share memory, and which are watched at all.

Path resolution is pure once `members()` is stubbed, so most of this runs
anywhere. The round trip through Postgres sits behind ARTERIES_LIVE_TESTS.
"""

import os
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from arteries import scope

LIVE_TESTS = os.getenv("ARTERIES_LIVE_TESTS") == "1"

FAKE = [
    scope.Member("arteries", "harness", Path("/repos/arteries")),
    scope.Member("capillaries", "harness", Path("/repos/capillaries")),
    scope.Member("sideproject", "solo", Path("/repos/sideproject")),
    scope.Member("vendored", "harness", Path("/repos/arteries/vendor/vendored")),
]


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self._p = patch.object(scope, "members", return_value=FAKE)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_repo_root_resolves_to_itself(self):
        self.assertEqual(scope.resolve("/repos/arteries").project_id, "arteries")

    def test_subdirectory_inherits_its_repo(self):
        """A hook fired from a subdirectory must report the repo, not the subdir.

        project_id otherwise defaults to cwd.name (setup_cli.py:79), so this
        would have been project "arteries" in one place and "src" in another.
        """
        m = scope.resolve("/repos/arteries/src/arteries")
        self.assertEqual(m.project_id, "arteries")

    def test_string_prefix_is_not_a_match(self):
        """'/repos/arteries' is a string prefix of '/repos/arteries-rework',
        which is a different repo that exists in this very checkout."""
        self.assertIsNone(scope.resolve("/repos/arteries-rework"))

    def test_nested_repo_wins_over_its_parent(self):
        m = scope.resolve("/repos/arteries/vendor/vendored/src")
        self.assertEqual(m.project_id, "vendored")

    def test_untracked_path_resolves_to_nothing(self):
        self.assertIsNone(scope.resolve("/tmp/somewhere-else"))
        self.assertFalse(scope.is_tracked("/tmp/somewhere-else"))

    def test_is_tracked_true_inside_a_member(self):
        self.assertTrue(scope.is_tracked("/repos/capillaries/src"))


@unittest.skipUnless(LIVE_TESTS, "set ARTERIES_LIVE_TESTS=1 for the Postgres round trip")
class LiveScopeTests(unittest.TestCase):
    def setUp(self):
        self.scope_id = f"test-{uuid4().hex[:8]}"
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for m in scope.members(self.scope_id):
            scope.remove(m.project_id)
        scope._execute("DELETE FROM arteries.scopes WHERE scope_id = %s", (self.scope_id,))

    def test_add_groups_projects_and_siblings_are_mutual(self):
        base = Path("/tmp") / f"scope-{uuid4().hex[:8]}"
        a, b = base / "alpha", base / "beta"
        scope.add(self.scope_id, [str(a), str(b)])

        self.assertEqual(sorted(scope.sibling_projects("alpha")), ["alpha", "beta"])
        self.assertEqual(sorted(scope.sibling_projects("beta")), ["alpha", "beta"])
        self.assertEqual(scope.scope_for("alpha"), self.scope_id)

    def test_unregistered_project_sees_only_itself(self):
        """The fallback that keeps an unconfigured install working."""
        self.assertEqual(scope.sibling_projects("nobody-registered-this"),
                         ["nobody-registered-this"])

    def test_move_regroups_without_losing_the_member(self):
        base = Path("/tmp") / f"scope-{uuid4().hex[:8]}"
        scope.add(self.scope_id, [str(base / "gamma")])
        other = f"{self.scope_id}-other"
        try:
            self.assertTrue(scope.move("gamma", other))
            self.assertEqual(scope.scope_for("gamma"), other)
        finally:
            scope.remove("gamma")
            scope._execute("DELETE FROM arteries.scopes WHERE scope_id = %s", (other,))


if __name__ == "__main__":
    unittest.main()
