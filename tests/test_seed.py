"""Unit tests for llunascope.seed — Hop-0 resolution helpers. Stdlib only."""

import unittest
from unittest import mock

from llunascope import seed
from llunascope.seed import SeedCandidate


class TestDomainFromUrl(unittest.TestCase):
    def test_plain_url(self):
        self.assertEqual(seed._domain_from_url("https://www.bankmandiri.co.id/"),
                         "bankmandiri.co.id")

    def test_bare_domain(self):
        self.assertEqual(seed._domain_from_url("example.com"), "example.com")

    def test_garbage_returns_empty(self):
        self.assertEqual(seed._domain_from_url("not a url"), "")
        self.assertEqual(seed._domain_from_url(""), "")


class TestResolveSeedFlag(unittest.TestCase):
    def test_seed_domains_bypass_and_normalize(self):
        domains, others, entities = seed.resolve(
            "Acme", seed_domains=["WWW.Example.COM.", "example.co.id"])
        self.assertEqual([d.value for d in domains],
                         ["example.com", "example.co.id"])
        self.assertTrue(all(d.source == "seed-flag" for d in domains))
        self.assertEqual(others, [])
        self.assertEqual(entities, [])

    def test_seed_garbage_dropped(self):
        domains, _, _ = seed.resolve("Acme", seed_domains=["!!!"])
        self.assertEqual(domains, [])


class TestWikidataSeeds(unittest.TestCase):
    def _claims(self, qid):
        return {"websites": ["https://acme.example/"],
                "subsidiaries": [(qid + "S", "Acme Subsidiary Ltd")],
                "parents": []}

    def test_hints_returned_alongside_domains(self):
        with mock.patch.object(seed, "_wikidata_label_search",
                               return_value=[{"id": "Q1", "label": "Acme",
                                              "sim": 0.9}]), \
             mock.patch.object(seed, "_wikidata_claims", side_effect=self._claims):
            domains, hints, entities = seed.wikidata_seeds("Acme")
        self.assertEqual([d.value for d in domains], ["acme.example"])
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0].kind, "hint")
        self.assertEqual(hints[0].value, "Acme Subsidiary Ltd")
        self.assertEqual(entities[0]["qid"], "Q1")

    def test_resolve_surfaces_hints_in_others(self):
        with mock.patch.object(seed, "_wikidata_label_search",
                               return_value=[{"id": "Q1", "label": "Acme",
                                              "sim": 0.9}]), \
             mock.patch.object(seed, "_wikidata_claims", side_effect=self._claims), \
             mock.patch.object(seed, "edgar_seeds", return_value=[]), \
             mock.patch.object(seed, "peeringdb_seeds", return_value=[]):
            domains, others, _ = seed.resolve("Acme")
        self.assertEqual([d.value for d in domains], ["acme.example"])
        self.assertEqual([h.value for h in others], ["Acme Subsidiary Ltd"])


class TestEdgarSeeds(unittest.TestCase):
    def test_parses_ticker_file(self):
        fake = {"0": [12345, "Acme Corporation", "ACME"],
                "1": ["notalist"]}
        with mock.patch("llunascope.seed.net.fetch_json", return_value=fake):
            out = seed.edgar_seeds("Acme Corporation")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "hint")
        self.assertIn("ACME", out[0].evidence)

    def test_dead_source_returns_empty(self):
        with mock.patch("llunascope.seed.net.fetch_json", return_value=None):
            self.assertEqual(seed.edgar_seeds("Acme"), [])


class TestPeeringdbSeeds(unittest.TestCase):
    def test_asn_candidates(self):
        fake = {"data": [{"name": "Acme Networks", "asn": [1234, 5678]}]}
        with mock.patch("llunascope.seed.net.fetch_json", return_value=fake):
            out = seed.peeringdb_seeds("Acme Networks")
        self.assertEqual([c.value for c in out], ["AS1234", "AS5678"])
        self.assertTrue(all(c.kind == "asn" for c in out))

    def test_low_similarity_org_skipped(self):
        fake = {"data": [{"name": "Totally Unrelated Org", "asn": [9999]}]}
        with mock.patch("llunascope.seed.net.fetch_json", return_value=fake):
            self.assertEqual(seed.peeringdb_seeds("Acme"), [])


if __name__ == "__main__":
    unittest.main()


class TestQidValidation(unittest.TestCase):
    def test_malformed_qid_rejected(self):
        # A hostile API response must not inject SPARQL.
        self.assertEqual(seed._wikidata_claims('Q1} DELETE WHERE {'), {})
        self.assertEqual(seed._wikidata_claims(''), {})
        self.assertEqual(seed._wikidata_claims(None), {})
