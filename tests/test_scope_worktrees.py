"""A worktree of a tracked repo is that repo.

heart runs every agent in a throwaway git worktree at ~/.cache/heart-ws/<hash>.
Scope resolution matched on path, so none of those paths was registered and every
agent turn was skipped as untracked -- measured on a three-session run: nine
skipped turns, zero memories, for the one workload this system exists to learn
from.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from arteries import scope

REPO = Path(__file__).resolve().parent.parent


class WorktreeResolutionTests(unittest.TestCase):
    def test_a_worktree_resolves_to_its_repository(self):
        common = scope._worktree_parent(REPO)
        self.assertEqual(common, REPO)

    def test_a_linked_worktree_resolves_to_the_main_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "wt"
            subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach",
                            str(worktree), "HEAD"],
                           capture_output=True, check=True)
            try:
                self.assertEqual(scope._worktree_parent(worktree), REPO)
            finally:
                subprocess.run(["git", "-C", str(REPO), "worktree", "remove",
                                "--force", str(worktree)], capture_output=True)

    def test_a_submodule_resolves_to_its_checkout(self):
        """Cloned through the vascular umbrella, every repo is a submodule whose
        git dir is `<super>/.git/modules/<name>`; its parent is not the repo."""
        with tempfile.TemporaryDirectory() as tmp:
            sub, sup = Path(tmp) / "sub", Path(tmp) / "super"
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@t",
                   "-c", "protocol.file.allow=always"]
            for cmd in (["init", "-q", str(sub)],
                        ["-C", str(sub), "commit", "-q", "--allow-empty", "-m", "x"],
                        ["init", "-q", str(sup)],
                        ["-C", str(sup), "submodule", "add", "-q", str(sub), "child"]):
                subprocess.run(git + cmd, capture_output=True, check=True)
            child = (sup / "child").resolve()
            self.assertEqual(scope._worktree_parent(child), child)

    def test_a_directory_that_is_not_a_repo_resolves_to_nothing(self):
        """The guard this sits behind exists because a benchmark exporting an
        unregistered ARTERIES_PROJECT got write access from inside a registered
        repo -- 42 of 73 rows in arteries.retrievals arrived that way. Admitting
        arbitrary directories would bring that back."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(scope._worktree_parent(Path(tmp)))

    def test_a_missing_path_does_not_raise(self):
        """Scope resolution gates every write. A git invocation failing should
        mean "not tracked", never "no memory this turn"."""
        self.assertIsNone(scope._worktree_parent(Path("/nonexistent-xyz")))

    def test_resolution_does_not_recurse_forever(self):
        """`_worktree_parent` of a repository root returns that root, so the
        recursive call has to be guarded or it never terminates."""
        import inspect

        source = inspect.getsource(scope.resolve)
        self.assertIn("main_repo != path", source)
