"""Findings 14, 23 and 24. Small, each with the same shape: a number or a
sentence that was true once and silently stopped being true."""

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from arteries import compile as compile_mod
from arteries import doctor, packet
from arteries.setup_cli import _codex_compact_prompt

HOOKS = Path(__file__).resolve().parent.parent / "hooks"


class HookTimeoutTests(unittest.TestCase):
    """Finding 14: the JS gave Python 5000ms while hooks.json allowed 10s, so
    the embedding call, the write and corpus retrieval all had to finish inside
    half the budget or the write was killed mid-flight."""

    def test_every_hook_fits_inside_its_own_declared_budget(self):
        """Each script against its own entry, not against the smallest one. The
        first version of this test compared the observe hook to PostToolUse's
        5s and failed for the wrong reason."""
        import json

        config = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
        checked = 0
        for entries in config.values():
            for entry in entries:
                for hook in entry["hooks"]:
                    name = re.search(r"([\w.-]+\.js)", hook["command"])
                    script = HOOKS / name.group(1)
                    if not script.is_file():
                        continue
                    inner = re.findall(r"timeout:\s*(\d+)", script.read_text())
                    if not inner:
                        continue
                    checked += 1
                    self.assertLess(max(int(m) for m in inner),
                                    hook["timeout"] * 1000,
                                    f"{script.name} outlives its own hook budget")
        self.assertGreater(checked, 0, "no hook scripts were actually checked")

    def test_the_inner_budget_leaves_room_for_start_up(self):
        js = (HOOKS / "arteries-observe.js").read_text()
        self.assertIn("9000", js)


class CompressionRatioTests(unittest.TestCase):
    """Finding 23: the docstring called the compile pass a decomposer at 1.43
    facts per row. It compresses -- 0.84 measured, 0.62 when the audit ran."""

    def test_the_docstring_no_longer_claims_expansion(self):
        text = compile_mod._reject_duplicates.__doc__
        self.assertNotIn("53 ephemeral\n    rows produced 76 facts", text or "")
        self.assertIn("compresses", text)

    def test_doctor_recomputes_the_ratio(self):
        """Checkable rather than remembered, so the docstring cannot go stale
        again without something disagreeing with it."""
        import inspect

        self.assertIn("facts_per_claimed_row", inspect.getsource(doctor.integrity))


class CompactPromptTests(unittest.TestCase):
    """Finding 24: the prompt named the v1 layout. A prompt describing sections
    that no longer exist tells the model to preserve headings it will never see,
    and nothing says so, because a prompt cannot fail."""

    def test_the_prompt_is_generated_from_the_packet_sections(self):
        """STATE_SECTION_TITLES, not SECTION_TITLES: this prompt only ever
        fires on compaction, and the renderer split
        (planning/compaction_v3.md §2) gave that path its own layout."""
        prompt = _codex_compact_prompt()
        for title in packet.STATE_SECTION_TITLES:
            self.assertIn(title, prompt)

    def test_the_prompt_carries_the_schema_version(self):
        self.assertIn(f"packet-schema: v{packet.PACKET_SCHEMA_VERSION}",
                      _codex_compact_prompt())

    def test_doctor_notices_a_stale_prompt(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".arteries" / "codex"
            path.mkdir(parents=True)
            (path / "compact_prompt.txt").write_text("packet-schema: v1\nold layout")
            with patch.object(Path, "cwd", staticmethod(lambda: Path(tmp))):
                self.assertTrue(doctor._compact_prompt_stale())

    def test_a_current_prompt_is_not_flagged(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".arteries" / "codex"
            path.mkdir(parents=True)
            (path / "compact_prompt.txt").write_text(_codex_compact_prompt())
            with patch.object(Path, "cwd", staticmethod(lambda: Path(tmp))):
                self.assertFalse(doctor._compact_prompt_stale())

    def test_a_missing_prompt_is_not_stale(self):
        with TemporaryDirectory() as tmp:
            with patch.object(Path, "cwd", staticmethod(lambda: Path(tmp))):
                self.assertFalse(doctor._compact_prompt_stale())


class StrandedClaimTests(unittest.TestCase):
    def test_doctor_measures_the_lease_not_the_row(self):
        """Same correction as the sweep: a row that queued a long time before
        being claimed is not stranded, and counting it as stranded made this
        number alarming and wrong."""
        import inspect

        source = inspect.getsource(doctor.integrity)
        self.assertIn("coalesce(claimed_at, source_ts)", source)
