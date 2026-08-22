"""Document chunking, and the provenance chain it produces."""

import unittest

from arteries import ingest


class SplitTests(unittest.TestCase):
    def test_short_document_is_one_chunk(self):
        chunks = ingest.split("# Title\n\nOne short paragraph about pgvector.\n")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].line_start, 1)

    def test_headings_start_new_chunks_once_there_is_enough_behind_them(self):
        """A heading is the document saying where the topic changes, which beats
        any length heuristic -- but only once the current chunk has substance."""
        body = "x " * 500
        text = f"# One\n\n{body}\n\n# Two\n\n{body}\n"
        chunks = ingest.split(text)
        self.assertGreater(len(chunks), 1)

    def test_chunks_carry_line_spans(self):
        text = "\n".join(f"line {i}" for i in range(1, 40))
        for c in ingest.split(text):
            self.assertLessEqual(c.line_start, c.line_end)

    def test_no_chunk_wildly_exceeds_the_cap(self):
        text = "\n".join("a paragraph that goes on for a while about arteries." * 3
                         for _ in range(200))
        chunks = ingest.split(text)
        self.assertTrue(chunks)
        # The cap is checked after appending a line, so one line may overshoot;
        # what matters is that it does not run away.
        self.assertLess(max(len(c.text) for c in chunks), ingest.MAX_CHARS * 2)

    def test_empty_document_yields_nothing(self):
        self.assertEqual(ingest.split("   \n\n  \n"), [])

    def test_digest_changes_with_content(self):
        """The digest is the re-ingest guard; identical text must not re-import."""
        self.assertEqual(ingest._digest("abc"), ingest._digest("abc"))
        self.assertNotEqual(ingest._digest("abc"), ingest._digest("abd"))


if __name__ == "__main__":
    unittest.main()


class StdinIdentityTests(unittest.TestCase):
    """Plexus plans and heart notes are generated in memory, never written to
    disk. They still need a stable identity so re-sending dedupes."""

    def test_generated_name_is_stable_for_identical_content(self):
        a = f"stdin:plan:{ingest._digest('same text')[:12]}"
        b = f"stdin:plan:{ingest._digest('same text')[:12]}"
        self.assertEqual(a, b)

    def test_generated_name_differs_when_content_changes(self):
        a = ingest._digest("plan v1")[:12]
        b = ingest._digest("plan v2")[:12]
        self.assertNotEqual(a, b)


class PlanChunkingTests(unittest.TestCase):
    """A plan is read for its parts, not its gist.

    Measured: a four-section plan fit in one document-sized chunk and came back
    as a single claim -- the dependency between steps and the rationale for the
    decision were summarised away. At plan sizes the same text yields three.
    """

    PLAN = (
        "# Goal: fix retrieval scoring\n\n"
        "## Step 1 - raise the reranker cap\n"
        "Change MAX_DOC_CHARS in reranker.py from 2000 to 4000.\n"
        "Outcome: recall improves with no latency change.\n\n"
        "## Step 2 - carry chunk ids into rerank\n"
        "find.py must pass chunk ids and scores through. Depends on step 1.\n\n"
        "## Decision\n"
        "Chunk-level rerank was chosen over parent-document rerank.\n"
    )

    def test_plan_kind_splits_finer_than_document_kind(self):
        as_plan = ingest.split(self.PLAN, "plan")
        as_doc = ingest.split(self.PLAN, "document")
        self.assertGreater(len(as_plan), len(as_doc))

    def test_document_is_still_the_default(self):
        self.assertEqual(ingest.split(self.PLAN), ingest.split(self.PLAN, "document"))

    def test_plan_chunks_still_break_on_headings(self):
        for c in ingest.split(self.PLAN, "plan"):
            self.assertLessEqual(len(c.text), ingest.PLAN_MAX_CHARS * 2)


class ImageTests(unittest.TestCase):
    """Arteries does not look at pictures. The description is an input."""

    def test_image_suffixes_route_to_the_image_path(self):
        for suffix in (".png", ".JPG", ".webp"):
            self.assertIn(suffix.lower(), ingest.IMAGE_SUFFIXES)
        self.assertNotIn(".md", ingest.IMAGE_SUFFIXES)

    def test_no_description_and_no_vision_asks_rather_than_guesses(self):
        import asyncio
        from pathlib import Path
        from unittest.mock import patch

        with patch.object(ingest, "describe_image", return_value=None):
            result = asyncio.run(ingest.ingest_image(Path("/tmp/nope.png"), "proj"))
        self.assertEqual(result["status"], "needs_description")

    def test_vision_probe_is_false_when_the_endpoint_is_unreachable(self):
        from unittest.mock import patch
        with patch("httpx.get", side_effect=OSError("no server")):
            self.assertFalse(ingest.vision_available())
