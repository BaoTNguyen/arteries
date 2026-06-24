import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from arteries import evergreen


class EvergreenReviewTests(unittest.TestCase):
    def test_extract_writes_human_editable_review_and_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text(
                """# Project Rules

Arteries must use capillaries MemoryFrame as the integration contract.

Temporary debug notes are not stable enough.
""",
                encoding="utf-8",
            )
            out = root / "evergreen_review.md"

            candidates = evergreen.extract_candidates(root, ["AGENTS.md"])
            evergreen.write_review(root, out, candidates, import_id="test-import")

            review_text = out.read_text(encoding="utf-8")
            self.assertIn("## Accepted Memories", review_text)
            self.assertIn("### mem_001", review_text)
            self.assertIn("Source: AGENTS.md:3-3", review_text)
            self.assertTrue(evergreen.sidecar_path(out).exists())

    def test_parse_review_tracks_accepted_rejected_and_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "evergreen_review.md"
            evergreen.write_review(
                root,
                review,
                [
                    evergreen.CandidateMemory(
                        memory_id="mem_001",
                        fact="Arteries uses capillaries MemoryFrame.",
                        domains=["technical", "AI"],
                        confidence=0.9,
                        source=evergreen.SourceSpan(
                            path="AGENTS.md",
                            line_start=1,
                            line_end=1,
                            digest="sha256:test",
                        ),
                    )
                ],
                import_id="test-import",
            )
            review.write_text(
                """---
import_id: test-import
project_root: /tmp/project
status: pending_review
---

# Evergreen Memory Review

## Accepted Memories

### mem_001

Arteries uses capillaries MemoryFrame as the stable integration contract.

Source: AGENTS.md:1-1
Domains: technical, AI
Confidence: 0.95

### mem_manual

User prefers explicit Markdown review before evergreen imports.

Source: manual
Domains: intent
Confidence: 1.00

## Rejected Memories

### mem_002

The current embedding threshold is 0.50.

Source: memory_project_handoff.md:42-55
Domains: technical
Confidence: 0.80
Reason: implementation detail
""",
                encoding="utf-8",
            )

            _header, blocks = evergreen.parse_review(review)
            summary = evergreen.import_review(review, write=False)

            self.assertEqual(len(blocks), 3)
            self.assertEqual(summary["accepted"], 2)
            self.assertEqual(summary["rejected"], 1)
            self.assertEqual(summary["edited"], 1)
            self.assertEqual(summary["manual"], 1)
            self.assertEqual(summary["inserted"], 0)

    @patch.object(evergreen.storage, "get_evergreen", return_value=[])
    @patch.object(evergreen.storage, "insert_evergreen", return_value="evergreen-id")
    def test_import_review_writes_accepted_memories_with_source_meta(self, insert_evergreen, _get_evergreen):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "evergreen_review.md"
            evergreen.write_review(
                root,
                review,
                [
                    evergreen.CandidateMemory(
                        memory_id="mem_001",
                        fact="Arteries uses capillaries MemoryFrame.",
                        domains=["technical"],
                        confidence=0.9,
                        source=evergreen.SourceSpan(
                            path="AGENTS.md",
                            line_start=1,
                            line_end=1,
                            digest="sha256:test",
                        ),
                    )
                ],
                import_id="test-import",
            )
            review.write_text(
                """---
import_id: test-import
project_root: /tmp/project
status: pending_review
---

# Evergreen Memory Review

## Accepted Memories

### mem_001

Arteries uses capillaries MemoryFrame as the stable integration contract.

Source: AGENTS.md:1-1
Domains: technical, AI
Confidence: 0.95

## Rejected Memories
""",
                encoding="utf-8",
            )

            summary = evergreen.import_review(review, write=True)

            self.assertEqual(summary["inserted"], 1)
            insert_evergreen.assert_called_once()
            kwargs = insert_evergreen.call_args.kwargs
            self.assertEqual(kwargs["fact"], "Arteries uses capillaries MemoryFrame as the stable integration contract.")
            self.assertEqual(kwargs["domains"], ["technical", "AI"])
            self.assertTrue(kwargs["source_meta"]["edited"])
            self.assertEqual(kwargs["source_meta"]["review_id"], "test-import")
            self.assertEqual(kwargs["source_meta"]["source"]["path"], "AGENTS.md")
            self.assertEqual(kwargs["source_meta"]["source_file"], "AGENTS.md")
            self.assertEqual(kwargs["source_meta"]["source_hash"], "sha256:test")

    @patch.object(evergreen.storage, "get_evergreen", return_value=[])
    @patch.object(evergreen.storage, "insert_evergreen", return_value="evergreen-id")
    def test_import_review_reports_duplicate_memory_ids_and_blocks_write(self, insert_evergreen, _get_evergreen):
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / "evergreen_review.md"
            evergreen.sidecar_path(review).write_text('{"memories": []}', encoding="utf-8")
            review.write_text(
                """---
import_id: test-import
project_root: /tmp/project
status: pending_review
---

# Evergreen Memory Review

## Accepted Memories

### mem_001

Accepted copy should not import while duplicate ID exists.

Source: manual
Domains: technical
Confidence: 1.00

## Rejected Memories

### mem_001

Rejected copy with same ID should trigger a review error.

Source: manual
Domains: technical
Confidence: 1.00
Reason: user moved this but forgot to remove accepted copy
""",
                encoding="utf-8",
            )

            preview = evergreen.import_review(review, write=False)
            written = evergreen.import_review(review, write=True)

            self.assertEqual(preview["duplicate_ids"], ["mem_001"])
            self.assertTrue(preview["errors"])
            self.assertEqual(written["inserted"], 0)
            insert_evergreen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
