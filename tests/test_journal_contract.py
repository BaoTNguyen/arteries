"""The journal contract, asserted independently in each repo that writes to it.

arteries, heart and capillaries each resolve the journal path themselves --
none of them depends on the others, so none can import a shared constant. The
duplication is deliberate; drift is what is dangerous. A rename this session
proved it: with plexus reading the old variable while arteries and heart wrote
the new one, events went to two directories and the only symptom was a test
failing with "spine events not written".

These constants ARE the contract. Change one repo and its own test fails, at the
point of the change rather than in production. Change all of them together and
the rename is complete by construction.
"""
import os
from pathlib import Path
from unittest.mock import patch

from arteries.journal import journal_dir

JOURNAL_ENV = "EVENT_JOURNAL_DIR"
JOURNAL_DEFAULT = Path.home() / ".local" / "share" / "heart" / "events"


def test_the_default_path_matches_the_contract():
    env = {k: v for k, v in os.environ.items() if k != JOURNAL_ENV}
    with patch.dict(os.environ, env, clear=True):
        assert journal_dir() == JOURNAL_DEFAULT


def test_the_environment_variable_overrides_it():
    with patch.dict(os.environ, {JOURNAL_ENV: "/tmp/elsewhere"}):
        assert journal_dir() == Path("/tmp/elsewhere")
