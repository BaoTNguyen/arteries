"""Document chunking, and the provenance chain it produces."""

import unittest

from arteries import ingest_docs


class SplitTests(unittest.TestCase):
    def test_short_document_is_one_chunk(self):
        chunks = ingest_docs.split("# Title\n\nOne short paragraph about pgvector.\n")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].line_start, 1)

    def test_headings_start_new_chunks_once_there_is_enough_behind_them(self):
        """A heading is the document saying where the topic changes, which beats
        any length heuristic -- but only once the current chunk has substance."""
        body = "x " * 500
        text = f"# One\n\n{body}\n\n# Two\n\n{body}\n"
        chunks = ingest_docs.split(text)
        self.assertGreater(len(chunks), 1)

    def test_chunks_carry_line_spans(self):
        text = "\n".join(f"line {i}" for i in range(1, 40))
        for c in ingest_docs.split(text):
            self.assertLessEqual(c.line_start, c.line_end)

    def test_no_chunk_wildly_exceeds_the_cap(self):
        text = "\n".join("a paragraph that goes on for a while about arteries." * 3
                         for _ in range(200))
        chunks = ingest_docs.split(text)
        self.assertTrue(chunks)
        # The cap is checked after appending a line, so one line may overshoot;
        # what matters is that it does not run away.
        self.assertLess(max(len(c.text) for c in chunks), ingest_docs.MAX_CHARS * 2)

    def test_empty_document_yields_nothing(self):
        self.assertEqual(ingest_docs.split("   \n\n  \n"), [])

    def test_digest_changes_with_content(self):
        """The digest is the re-ingest guard; identical text must not re-import."""
        self.assertEqual(ingest_docs._digest("abc"), ingest_docs._digest("abc"))
        self.assertNotEqual(ingest_docs._digest("abc"), ingest_docs._digest("abd"))


if __name__ == "__main__":
    unittest.main()
