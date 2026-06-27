import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from arteries import packet


class PacketTests(unittest.TestCase):
    def test_build_packet_includes_memory_tiers_and_rules(self):
        with patch.object(packet.storage, "get_ephemeral", return_value=[{
            "id": "e1",
            "fact": "Recent user wants Pi first.",
            "domains": ["context"],
            "confidence": 0.8,
        }]), patch.object(packet.storage, "get_persistent", return_value=[{
            "id": "p1",
            "fact": "Project integrates coding CLIs through hooks.",
            "domains": ["technical"],
            "confidence": 0.9,
        }]), patch.object(packet.storage, "get_evergreen", return_value=[{
            "id": "g1",
            "fact": "Prefer scoped implementations.",
            "domains": ["preference"],
            "confidence": 1.0,
        }]):
            text = packet.build_packet("manual compact", budget=4000)

        self.assertIn("## Current Context", text)
        self.assertIn("Recent user wants Pi first.", text)
        self.assertIn("Project integrates coding CLIs through hooks.", text)
        self.assertIn("Prefer scoped implementations.", text)
        self.assertIn("Treat this packet as continuity context", text)

    def test_pi_json_format_wraps_summary(self):
        out = io.StringIO()
        with patch.object(packet, "build_packet", return_value="summary"):
            with redirect_stdout(out):
                self.assertEqual(packet.main(["--format", "pi-compaction-json", "--message", "auto"]), 0)

        payload = json.loads(out.getvalue())
        self.assertEqual(payload["summary"], "summary")
        self.assertEqual(payload["details"]["source"], "arteries")


if __name__ == "__main__":
    unittest.main()
