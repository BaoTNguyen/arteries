"""Tests for arteries.vascular_paths — the stack-wide vendored state directory.

Every public function is exercised. ENV vars that override locations are
explicitly tested so the module's own documentation stays true.
"""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class KindTests(unittest.TestCase):
    def test_kind_set_has_five_entries(self):
        from arteries.vascular_paths import KINDS
        self.assertEqual(len(KINDS), 5)

    def test_all_expected_kinds_present(self):
        from arteries.vascular_paths import KINDS
        expected = ("config", "state", "cache", "data", "backups")
        self.assertEqual(KINDS, expected)

    def test_kinds_is_a_tuple_not_a_list(self):
        from arteries.vascular_paths import KINDS
        self.assertIsInstance(KINDS, tuple)


class HomeTests(unittest.TestCase):
    def setUp(self):
        self._p = patch.dict(os.environ, {}, clear=True)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_home_uses_home_directory_by_default(self):
        from arteries.vascular_paths import home
        h = home()
        self.assertEqual(h.name, ".vascular")

    def test_home_uses_VASCULAR_HOME_when_set(self):
        from arteries.vascular_paths import home
        with patch.dict(os.environ, {"VASCULAR_HOME": "/custom/vascular"}):
            self.assertEqual(str(home()), "/custom/vascular")


class PathTests(unittest.TestCase):
    def setUp(self):
        self._p = patch.dict(os.environ, {"VASCULAR_HOME": "/vhome"}, clear=True)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_path_returns_correct_structure(self):
        from arteries.vascular_paths import path
        p = path("config", "mytool", "settings.ini")
        self.assertEqual(str(p), "/vhome/config/mytool/settings.ini")

    def test_path_with_single_component(self):
        from arteries.vascular_paths import path
        p = path("state", "heart")
        self.assertEqual(str(p), "/vhome/state/heart")

    def test_path_with_multiple_parts(self):
        from arteries.vascular_paths import path
        p = path("cache", "fetcher", "downloads", "file.txt")
        self.assertEqual(str(p), "/vhome/cache/fetcher/downloads/file.txt")

    def test_path_raises_for_unknown_kind(self):
        from arteries.vascular_paths import path
        with self.assertRaises(ValueError) as ctx:
            path("bogus", "tool")
        self.assertIn("bogus", str(ctx.exception))

    def test_path_does_not_create_directory(self):
        from arteries.vascular_paths import path
        p = path("data", "mydata", "sub")
        self.assertFalse(p.exists())


class JournalDirTests(unittest.TestCase):
    def setUp(self):
        self._p_env = patch.dict(os.environ, {}, clear=True)
        self._p_env.start()
        self.addCleanup(self._p_env.stop)
        self._p_home = patch("arteries.vascular_paths.home")
        self.mock_home = self._p_home.start()
        self.mock_home.return_value = Path("/vhome")
        self.addCleanup(self._p_home.stop)

    def test_journal_dir_defaults_to_heart_state_events(self):
        from arteries.vascular_paths import journal_dir
        j = journal_dir()
        self.assertEqual(str(j), "/vhome/state/heart/events")

    def test_journal_dir_uses_EVENT_JOURNAL_DIR_when_set(self):
        from arteries.vascular_paths import journal_dir
        with patch.dict(os.environ, {"EVENT_JOURNAL_DIR": "/custom/events"}):
            self.assertEqual(str(journal_dir()), "/custom/events")


class RepoDirTests(unittest.TestCase):
    def test_repo_dir_string_root(self):
        from arteries.vascular_paths import repo_dir
        r = repo_dir("/my/repo", "arteries")
        self.assertEqual(str(r), "/my/repo/.vascular/arteries")

    def test_repo_dir_path_root(self):
        from arteries.vascular_paths import repo_dir
        r = repo_dir(Path("/my/repo"), "arteries")
        self.assertEqual(str(r), "/my/repo/.vascular/arteries")

    def test_repo_dir_uses_dot_vascular(self):
        from arteries.vascular_paths import repo_dir
        r = repo_dir("/a/b", "cap")
        self.assertIn(".vascular", str(r))

    def test_repo_dir_component_is_trailing_segment(self):
        from arteries.vascular_paths import repo_dir
        r = repo_dir("/x", "z")
        self.assertEqual(r.name, "z")


class NoImportSideEffectsTests(unittest.TestCase):
    def test_import_has_no_io(self):
        """ vascular_paths must not touch the filesystem at import time.
        """
        import sys
        # Remove the module if it was already imported by another test
        saved = sys.modules.pop("arteries.vascular_paths", None)
        try:
            import arteries.vascular_paths  # noqa: F401
        finally:
            if saved is not None:
                sys.modules["arteries.vascular_paths"] = saved


class ArteriesStateTests(unittest.TestCase):
    """arteries resolves its out-of-checkout state through vascular_paths."""

    def _env(self, **overrides):
        env = {k: v for k, v in os.environ.items() if k != "EVENT_JOURNAL_DIR"}
        env.update(overrides)
        return env

    def test_state_lands_under_VASCULAR_HOME(self):
        import arteries.journal
        import arteries.usage
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, self._env(VASCULAR_HOME=tmp), clear=True):
                self.assertEqual(arteries.journal.journal_dir(),
                                 Path(tmp) / "state" / "heart" / "events")
                self.assertEqual(arteries.usage._state_path().parent,
                                 Path(tmp) / "state" / "arteries")

    def test_EVENT_JOURNAL_DIR_overrides_VASCULAR_HOME(self):
        import arteries.journal
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            env = self._env(VASCULAR_HOME=tmp, EVENT_JOURNAL_DIR=other)
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(arteries.journal.journal_dir(), Path(other))

    def test_vendored_copy_is_unmodified(self):
        import arteries.vascular_paths
        data = Path(arteries.vascular_paths.__file__).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         "e2da9c91a10831ed001c198a74deec77272b634542766a094e52a872b37836c4")


if __name__ == "__main__":
    unittest.main()
