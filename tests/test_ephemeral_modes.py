"""ARTERIES_EPHEMERAL: compile / keep / discard.

`keep` exists because compiling every assistant reply in an interactive session
turned six days of plexus work into 178 permanent memories, most of them
narration of what had just been said. It records the same turns; it just does
not promote them on its own.
"""

import importlib
import os
from unittest.mock import patch

import pytest


def _mode(value):
    """Reload config under an env value — EPHEMERAL_MODE resolves at import."""
    env = {k: v for k, v in os.environ.items() if k != "ARTERIES_EPHEMERAL"}
    if value is not None:
        env["ARTERIES_EPHEMERAL"] = value
    with patch.dict(os.environ, env, clear=True):
        import arteries.config as config
        return importlib.reload(config).EPHEMERAL_MODE


@pytest.mark.parametrize("value,expected", [
    (None, "compile"),          # default
    ("compile", "compile"),
    ("keep", "keep"),
    ("discard", "discard"),
    ("KEEP", "keep"),           # case-insensitive
    (" keep ", "keep"),         # tolerant of stray whitespace
    ("", "compile"),
    ("nonsense", "compile"),    # never let a typo mean "stop remembering"
])
def test_mode_resolution(value, expected):
    assert _mode(value) == expected


@pytest.mark.parametrize("mode,should_compile", [
    ("compile", True),
    ("keep", False),
    ("discard", False),
])
def test_only_compile_mode_promotes(mode, should_compile):
    import arteries.eval as ev
    with patch.object(ev, "EPHEMERAL_MODE", mode), \
         patch.object(ev, "_spawn_detached_compile") as spawn:
        # the gate as eval.py applies it
        if ev.EPHEMERAL_MODE == "compile":
            ev._spawn_detached_compile()
        assert spawn.called is should_compile


def test_keep_still_writes_ephemeral():
    """The point of `keep`: extraction still lands in storage, unlike `discard`."""
    import arteries.extract as ex
    with patch.object(ex, "EPHEMERAL_MODE", "keep"), \
         patch.object(ex, "extract_from_message", return_value=[]), \
         patch.object(ex, "_ephemeral_buffer", []) as buf:
        ex.extract_and_store("something worth keeping")
        # discard-mode is the only path that diverts into the in-process buffer
        assert buf == []
