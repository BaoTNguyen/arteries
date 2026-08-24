"""`art remember` wrote to whatever config.PROJECT_ID said.

That is "default" for anyone not running under a hook, so a human typing
`art remember add` filed the memory in a project nothing reads, and
`art remember rm` reported "Not found" for claims that were plainly there.
"""
import unittest
from unittest.mock import patch

from arteries import remember


class ProjectResolutionTests(unittest.TestCase):
    def test_add_files_under_the_path_resolved_project(self):
        with patch("arteries.scope.current_project", return_value="arteries"), \
             patch.object(remember, "_embed", return_value=None), \
             patch("arteries.storage.insert_persistent", return_value="a" * 36) as insert:
            remember.main(["add", "prefer stdlib"])
        self.assertEqual(insert.call_args.kwargs["project_id"], "arteries")

    def test_rm_targets_the_resolved_project(self):
        row = {"id": "348d379b-e329-4231-adf4-b4958c3ec6ed", "project_id": "arteries"}
        with patch("arteries.scope.current_project", return_value="arteries"), \
             patch("arteries.storage.get_persistent", return_value=[row]), \
             patch("arteries.storage.remove_persistent", return_value=True) as rm:
            remember.main(["rm", "348d379b"])
        self.assertEqual(rm.call_args.args[1], "arteries")


class CrossProjectTests(unittest.TestCase):
    """Reads span the scope, writes do not. A sibling's claim resolves here and
    must be refused by name, not reported missing."""

    def test_a_sibling_claim_is_refused_with_its_owner_named(self):
        row = {"id": "208318fb-7213-4eb2-b285-2ddc4b45dbd6", "project_id": "capillaries"}
        with patch("arteries.scope.current_project", return_value="arteries"), \
             patch("arteries.storage.get_persistent", return_value=[row]), \
             patch("builtins.print") as out:
            self.assertIsNone(remember._resolve_id("208318fb"))
        said = " ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("capillaries", said)
        self.assertNotIn("No memory matching", said)

    def test_a_compiled_claim_resolves_even_though_list_shows_only_user_rows(self):
        # scope="user" on the lookup made every compiled claim unreachable by id,
        # so a contradiction doctor reported could never be retired by hand.
        row = {"id": "348d379b-e329-4231-adf4-b4958c3ec6ed", "project_id": "arteries"}
        with patch("arteries.scope.current_project", return_value="arteries"), \
             patch("arteries.storage.get_persistent", return_value=[row]) as get:
            remember._resolve_id("348d379b")
        self.assertNotIn("scope", get.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
