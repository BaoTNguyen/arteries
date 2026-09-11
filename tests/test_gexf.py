"""GEXF export.

A graph you cannot see is a graph you cannot debug. `art graph stats` says there
are 3219 edges; it cannot say that 1757 of them are provenance pointing at
ephemeral rows, which is the shape of the problem rather than its size.
"""

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory

from arteries import gexf

NS = {"g": "http://gexf.net/1.3"}


def _nodes(count=2):
    return [{"id": f"n{i}", "tier": "persistent", "label": f"claim {i}",
             "fact": f"claim {i}", "kind": "fact"} for i in range(count)]


def _write(nodes, edges):
    with TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "g.gexf")
        written = gexf.write(nodes, edges, path)
        return written, ET.parse(path).getroot()


class WriteTests(unittest.TestCase):
    def test_a_graph_round_trips_as_valid_xml(self):
        edges = [{"src_kind": "persistent", "src_id": "n0", "dst_kind": "persistent",
                  "dst_id": "n1", "rel": "refines", "weight": 1.0,
                  "ontology_valid": False}]
        written, root = _write(_nodes(), edges)
        self.assertEqual(written, 2)
        self.assertEqual(len(root.findall(".//g:node", NS)), 2)
        self.assertEqual(len(root.findall(".//g:edge", NS)), 1)

    def test_an_edge_to_a_missing_node_is_dropped_here_not_by_gephi(self):
        """Gephi drops a dangling edge silently. Dropping it here means the
        count printed is the count exported."""
        edges = [{"src_kind": "persistent", "src_id": "n0", "dst_kind": "persistent",
                  "dst_id": "nowhere", "rel": "refines", "weight": 1.0,
                  "ontology_valid": False}]
        _written, root = _write(_nodes(), edges)
        self.assertEqual(len(root.findall(".//g:edge", NS)), 0)

    def test_the_relation_survives_as_an_attribute(self):
        """Gephi colours by attribute, so the relation has to be data rather
        than a pre-baked colour."""
        edges = [{"src_kind": "persistent", "src_id": "n0", "dst_kind": "persistent",
                  "dst_id": "n1", "rel": "contradicts", "weight": 1.0,
                  "ontology_valid": True}]
        _written, root = _write(_nodes(), edges)
        values = [v.get("value") for v in root.findall(".//g:edge//g:attvalue", NS)]
        self.assertIn("contradicts", values)
        self.assertIn("True", values)

    def test_core_and_incrementality_reach_the_file(self):
        nodes = [{"id": "e0", "tier": "evergreen", "label": "spec", "fact": "spec",
                  "kind": "fact", "core": True, "incrementality": None}]
        _written, root = _write(nodes, [])
        values = [v.get("value") for v in root.findall(".//g:node//g:attvalue", NS)]
        self.assertIn("True", values)

    def test_a_none_attribute_is_omitted_rather_than_written_as_none(self):
        """A seeded core row never scored, and "None" in a float column makes
        Gephi refuse the file."""
        nodes = [{"id": "e0", "tier": "evergreen", "label": "spec", "fact": "spec",
                  "kind": "fact", "core": True, "incrementality": None}]
        _written, root = _write(nodes, [])
        values = [v.get("value") for v in root.findall(".//g:node//g:attvalue", NS)]
        self.assertNotIn("None", values)

    def test_an_empty_graph_is_still_a_valid_file(self):
        written, root = _write([], [])
        self.assertEqual(written, 0)
        self.assertEqual(len(root.findall(".//g:node", NS)), 0)


class LabelTests(unittest.TestCase):
    def test_an_unlabelled_node_kind_still_gets_an_id_label(self):
        """chunk, document and episode are real nodes with no label table. Shown
        by id rather than dropped -- a dangling edge misleads more than an
        unnamed node."""
        labelled = gexf._label(None, "document", ["abcdef12-0000-0000-0000-000000000000"])
        self.assertEqual(len(labelled), 1)
        self.assertTrue(labelled[0]["label"].startswith("document:"))
