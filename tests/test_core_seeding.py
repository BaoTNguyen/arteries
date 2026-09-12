"""Documents are the route into evergreen; `art remember --core` is the exception.

The first seeding path split markdown with `normalize.atoms` and wrote the
sentences straight in, throwing away everything `ingest.py` already does:
chunking, the compile call that assigns `kind` and extracts entities,
`derived_from` edges back to the chunk and document, and a digest that makes
re-seeding an unchanged file a no-op. Two extractors for one job, and the worse
one was the default.
"""

import inspect
import unittest

from arteries import evergreen, ingest, remember


class SeedRoutingTests(unittest.TestCase):
    def test_seed_delegates_to_ingest_rather_than_splitting_text(self):
        source = inspect.getsource(evergreen.main)
        self.assertIn("ingest.ingest_file", source)
        # The call, not the word: a comment explaining what was removed and why
        # is the most useful line in that function.
        self.assertNotIn("normalize.atoms(", source)

    def test_seed_asks_for_core(self):
        self.assertIn("core=True", inspect.getsource(evergreen.main))

    def test_planning_documents_are_the_default(self):
        self.assertEqual(evergreen.DEFAULT_SPEC_GLOBS, ("planning/*.md",))
        self.assertEqual(evergreen._default_specs(), ["planning/*.md"])

    def test_agents_md_is_opt_in(self):
        """A later artefact: it describes how agents work in a repo that already
        exists, so it is seeded on demand rather than at project creation."""
        self.assertNotIn("AGENTS.md", evergreen._default_specs())
        self.assertIn("AGENTS.md", evergreen._default_specs(agents=True))


class CoreFieldTests(unittest.TestCase):
    """`core` is a boolean, not a kind. Conflating them would write
    kind='core', which is outside the enum and which validate_response
    rejects -- the same way it rejected kind='strategy' in a live pass."""

    def test_ingest_takes_core_as_a_flag_not_a_kind(self):
        params = inspect.signature(ingest.ingest_file).parameters
        self.assertIn("core", params)
        self.assertIs(params["core"].default, False)

    def test_kind_stays_the_compilers_call(self):
        """A seeded document produces core facts, core decisions and core
        constraints -- the flag says nothing about what each claim is."""
        source = inspect.getsource(ingest._promote_core)
        self.assertIn('row.get("kind")', source)
        self.assertNotIn("'core'", source.split("INSERT")[0])

    def test_a_core_row_is_authored_and_never_scored(self):
        source = inspect.getsource(ingest._promote_core)
        self.assertIn("true, 'authored', 'user'", source)
        self.assertNotIn("incrementality", source)

    def test_core_keeps_its_document_provenance(self):
        """A core node in the graph still answers "which paragraph of which
        document said this"."""
        source = inspect.getsource(ingest._promote_core)
        self.assertIn("DERIVED_FROM", source)
        self.assertIn("chunk", source)

    def test_remember_core_writes_persistent_first(self):
        """Promotion goes one level at a time. An authored write is exempt from
        that rule only because a human named the tier; the lineage still has to
        be real, so parent_ids points at the persistent row."""
        source = inspect.getsource(remember._do_add)
        self.assertLess(source.index("insert_persistent"), source.index("evergreen"))
        self.assertIn("[pid]", source)

    def test_remember_core_is_opt_in(self):
        self.assertIn("--core", inspect.getsource(remember._build_parser)
                      if hasattr(remember, "_build_parser")
                      else inspect.getsource(remember))
