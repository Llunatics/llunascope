"""Unit tests for llunascope.pivot — signal logic with network mocked.

All external I/O (CT logs, DNS, RIPE, RDAP, openssl downloads) is stubbed:
these tests prove the pivot ENGINE logic — edge creation, penalties, bounds —
without touching the internet.
"""

import unittest
from unittest import mock

from llunascope import pivot
from llunascope.pivot import CertInfo, PivotResult


def _cert(cid, sans, issuer="Example CA"):
    return CertInfo(cert_id=cid, issuer_o=issuer, san_roots=set(sans))


class PivotTestBase(unittest.TestCase):
    def setUp(self):
        p = mock.patch.multiple(
            pivot,
            fetch_certs=mock.DEFAULT,
            resolve_ips=mock.DEFAULT,
            ip_to_asn=mock.DEFAULT,
            rdap_org_handle=mock.DEFAULT,
            enrich_subject_o=mock.DEFAULT,
        )
        self.m = p.start()
        self.addCleanup(p.stop)
        self.m["fetch_certs"].return_value = []
        self.m["resolve_ips"].return_value = []
        self.m["ip_to_asn"].return_value = None
        self.m["rdap_org_handle"].return_value = ""

    def run_pivot(self, seeds, certs_by_domain=None, **kw):
        if certs_by_domain is not None:
            self.m["fetch_certs"].side_effect = (
                lambda d, limit=500: list(certs_by_domain.get(d, [])))
        return pivot.pivot(seeds, **kw)


class TestSanOverlap(PivotTestBase):
    def test_shared_cert_creates_edge(self):
        certs = {"seed.com": [_cert("1", {"seed.com", "child.com"})]}
        res = self.run_pivot(["seed.com"], certs)
        self.assertEqual(len(res.edges), 1)
        e = res.edges[0]
        self.assertEqual(e.target, "child.com")
        self.assertEqual(e.via, "seed.com")
        self.assertEqual(e.hop, 1)
        self.assertTrue(any(s.signal == "san_overlap" for s in e.evidences))
        self.assertEqual(e.confidence, 40.0)

    def test_only_polluted_cert_means_no_edge(self):
        # The only shared cert covers >30 roots (CDN/WAF): too weak, skipped.
        sans = {"seed.com"} | {f"tenant{i}.com" for i in range(40)}
        certs = {"seed.com": [_cert("1", sans)]}
        res = self.run_pivot(["seed.com"], certs)
        self.assertEqual(res.edges, [])

    def test_polluted_cert_penalizes_edge(self):
        shared = _cert("1", {"seed.com", "child.com"})
        polluted = _cert("2", {"seed.com", "child.com"} |
                         {f"tenant{i}.com" for i in range(40)})
        certs = {"seed.com": [shared, polluted]}
        res = self.run_pivot(["seed.com"], certs)
        self.assertEqual(len(res.edges), 1)
        e = res.edges[0]
        self.assertTrue(e.penalized)
        self.assertAlmostEqual(e.confidence, round(40.0 * 0.2, 1))

    def test_no_self_edge(self):
        # A cert whose only SAN root is the domain itself yields no edge.
        certs = {"seed.com": [_cert("1", {"seed.com"})]}
        res = self.run_pivot(["seed.com"], certs)
        self.assertEqual(res.edges, [])


class TestBounds(PivotTestBase):
    def test_chain_terminates_at_max_hops(self):
        # a->b->c->d->e->f chain; must terminate, never loop.
        chain = ["s.com", "b.com", "c.com", "d.com", "e.com", "f.com"]
        certs = {chain[i]: [_cert(str(i), {chain[i], chain[i + 1]})]
                 for i in range(len(chain) - 1)}
        res = self.run_pivot(["s.com"], certs)
        self.assertLessEqual(len(res.nodes), pivot.MAX_NODES)
        hops = {e.hop for e in res.edges}
        self.assertTrue(hops)  # sanity: edges were created
        self.assertLessEqual(max(hops), pivot.MAX_HOPS + 1)
        # e.com is discovered as a node but never expanded (hop 4 > MAX_HOPS),
        # so f.com never even becomes a node.
        self.assertIn("e.com", res.nodes)
        self.assertNotIn("f.com", res.nodes)

    def test_cycle_does_not_loop_forever(self):
        certs = {
            "a.com": [_cert("1", {"a.com", "b.com"})],
            "b.com": [_cert("2", {"b.com", "a.com"})],
        }
        res = self.run_pivot(["a.com"], certs)
        self.assertEqual(res.nodes, {"a.com", "b.com"})
        self.assertEqual(len(res.edges), 2)  # a->b and b->a, each once


class TestAsnSignal(PivotTestBase):
    def test_ignored_asn_never_pivots(self):
        self.m["resolve_ips"].return_value = ["1.2.3.4"]
        self.m["ip_to_asn"].return_value = pivot.AsnInfo(13335, "1.2.3.0/24",
                                                        2 ** 8)
        certs = {"a.com": [_cert("1", {"a.com"})],
                 "b.com": [_cert("2", {"b.com"})]}
        # b.com is already a known node via a seed edge; ASN must not link it.
        res = self.run_pivot(["a.com", "b.com"], certs)
        asn_edges = [e for e in res.edges
                     if any(s.signal == "asn_match" for s in e.evidences)]
        self.assertEqual(asn_edges, [])

    def test_shared_nonignored_asn_links_nodes(self):
        self.m["resolve_ips"].return_value = ["1.2.3.4"]
        self.m["ip_to_asn"].return_value = pivot.AsnInfo(47611, "1.2.3.0/24",
                                                        2 ** 8)
        res = self.run_pivot(["a.com", "b.com"], {})
        asn_edges = [e for e in res.edges
                     if any(s.signal == "asn_match" for s in e.evidences)]
        self.assertTrue(asn_edges)
        self.assertEqual(asn_edges[0].confidence, 25.0)

    def test_huge_prefix_not_pivotable(self):
        self.m["resolve_ips"].return_value = ["1.2.3.4"]
        self.m["ip_to_asn"].return_value = pivot.AsnInfo(47611, "1.0.0.0/8",
                                                        2 ** 24)
        res = self.run_pivot(["a.com", "b.com"], {})
        asn_edges = [e for e in res.edges
                     if any(s.signal == "asn_match" for s in e.evidences)]
        self.assertEqual(asn_edges, [])


class TestOrgTuple(PivotTestBase):
    def test_identical_subject_o_links_roots(self):
        def enrich(certs, limit=3):
            for c in certs:
                c.subject_o = "Acme Corp"
        self.m["enrich_subject_o"].side_effect = enrich
        certs = {"a.com": [_cert("1", {"a.com"})],
                 "b.com": [_cert("2", {"b.com"})]}
        res = self.run_pivot(["a.com", "b.com"], certs)
        org_edges = [e for e in res.edges
                     if any(s.signal == "org_tuple" for s in e.evidences)]
        self.assertTrue(org_edges)
        self.assertEqual(res.org_identities["a.com"], "Acme Corp")


class TestCertFetchLimits(PivotTestBase):
    """Deep-8 M14: hop-0 gets the large CT-log limit, deeper hops the small one."""

    def test_hop0_gets_500_deeper_hops_get_150(self):
        seen = {}

        def rec(d, limit=500):
            seen[d] = limit
            if d == "s.com":
                return [_cert("1", {"s.com", "b.com"})]
            return []

        self.m["fetch_certs"].side_effect = rec
        self.run_pivot(["s.com"])
        self.assertEqual(seen.get("s.com"), 500)
        self.assertEqual(seen.get("b.com"), 150)


class TestAsnPrefixBoundary(PivotTestBase):
    """Deep-8 M17: a prefix of exactly /16 is still pivotable (<=, not <)."""

    def test_slash16_prefix_is_still_pivotable(self):
        self.m["resolve_ips"].return_value = ["1.2.3.4"]
        self.m["ip_to_asn"].return_value = pivot.AsnInfo(47611, "1.2.0.0/16",
                                                        2 ** 16)
        res = self.run_pivot(["a.com", "b.com"], {})
        asn_edges = [e for e in res.edges
                     if any(s.signal == "asn_match" for s in e.evidences)]
        self.assertTrue(asn_edges)


if __name__ == "__main__":
    unittest.main()
