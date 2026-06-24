import unittest

from arteries.extract import extract_from_message


class ExtractFromMessageTests(unittest.TestCase):
    def test_preference_becomes_ephemeral_candidate(self):
        result = extract_from_message(
            "I prefer small standard-library Python fixes when debugging agentic coding systems."
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].signal_type, "preference")
        self.assertEqual(result[0].confidence, 0.8)
        self.assertEqual(result[0].domains, ["technical"])
        self.assertIn("standard-library Python fixes", result[0].fact)

    def test_short_messages_are_ignored(self):
        self.assertEqual(extract_from_message("thanks"), [])

    def test_domain_signal_is_lower_confidence_when_no_fact_pattern_matches(self):
        result = extract_from_message(
            "The backend API database testing workflow needs better architecture coverage"
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].signal_type, "domain_signal")
        self.assertEqual(result[0].confidence, 0.5)
        self.assertEqual(result[0].domains, ["technical"])

    def test_correction_has_high_confidence(self):
        result = extract_from_message(
            "No, not that, I meant the persistent memory should supersede stale facts."
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].signal_type, "correction")
        self.assertEqual(result[0].confidence, 0.9)


if __name__ == "__main__":
    unittest.main()
