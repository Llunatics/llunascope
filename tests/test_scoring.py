"""Unit tests for llunascope.scoring — confidence math. Stdlib only."""

import unittest

from llunascope.scoring import (Edge, Evidence, MAX_CONFIDENCE, SAN_CEILING,
                                WEIGHTS)


def _edge(*signals):
    e = Edge("target.example", "seed.example", 1)
    for s in signals:
        e.add(s, f"test evidence for {s}")
    return e.finalize()


class TestWeights(unittest.TestCase):
    def test_single_signal(self):
        e = _edge("san_overlap")
        self.assertEqual(e.confidence, WEIGHTS["san_overlap"])

    def test_sum_of_signals(self):
        e = _edge("san_overlap", "asn_match")
        self.assertEqual(e.confidence,
                         WEIGHTS["san_overlap"] + WEIGHTS["asn_match"])

    def test_capped_at_99(self):
        # All signals at once would exceed 100 without the cap.
        e = _edge("san_overlap", "org_tuple", "asn_match", "rdap_org")
        total = sum(WEIGHTS[s] for s in
                    ("san_overlap", "org_tuple", "asn_match", "rdap_org"))
        self.assertGreater(total, MAX_CONFIDENCE)
        self.assertEqual(e.confidence, MAX_CONFIDENCE)

    def test_unknown_signal_weights_zero(self):
        e = _edge("definitely_not_a_signal")
        self.assertEqual(e.confidence, 0.0)

    def test_shared_cert_penalty(self):
        e = Edge("target.example", "seed.example", 1)
        e.add("san_overlap", "2 shared certs")
        e.evidences.append(Evidence("__san_ceiling__", "shared CDN cert"))
        e.finalize()
        self.assertTrue(e.penalized)
        self.assertAlmostEqual(
            e.confidence,
            round(WEIGHTS["san_overlap"] * 0.2, 1))
        self.assertIn("shared-cert penalty", e.summary())

    def test_summary_skips_internal_signals(self):
        e = Edge("target.example", "seed.example", 1)
        e.add("asn_match", "both in AS1")
        s = e.summary()
        self.assertIn("asn_match", s)
        self.assertNotIn("__", s)

    def test_ceiling_constant_sanity(self):
        self.assertEqual(SAN_CEILING, 30)

    def test_adversarial_evidence_clamped(self):
        # Empty evidence produces 0.0 float
        e_empty = Edge("target.example", "seed.example", 1).finalize()
        self.assertEqual(e_empty.confidence, 0.0)
        self.assertIsInstance(e_empty.confidence, float)

        # Negative weight is clamped to 0.0
        e_neg = Edge("target.example", "seed.example", 1)
        e_neg.evidences.append(Evidence("custom_neg", "negative signal", -50.0))
        e_neg.finalize()
        self.assertEqual(e_neg.confidence, 0.0)
        self.assertIsInstance(e_neg.confidence, float)

        # Extreme positive weight clamped to MAX_CONFIDENCE (99.0)
        e_huge = Edge("target.example", "seed.example", 1)
        e_huge.evidences.append(Evidence("custom_huge", "huge signal", 999.0))
        e_huge.finalize()
        self.assertEqual(e_huge.confidence, 99.0)
        self.assertIsInstance(e_huge.confidence, float)


if __name__ == "__main__":
    unittest.main()
