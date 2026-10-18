"""Unit tests for llunascope.render — output formats. Stdlib only."""

import io
import json
import unittest

from llunascope import render
from llunascope.scoring import Edge


def _edge(target, via, hop=1, signals=("san_overlap",)):
    e = Edge(target, via, hop)
    for s in signals:
        e.add(s, f"test {s} evidence")
    return e.finalize()


class TestPrintTree(unittest.TestCase):
    def setUp(self):
        render.RICH = False  # force the plain-ASCII path for determinism

    def test_tree_shows_children_of_seed_roots(self):
        """Regression: tree rendered empty when children keyed by seed name."""
        edges = [_edge("aol.com", "yahoo.com"),
                 _edge("cs.com", "aol.com", hop=2, signals=("asn_match",))]
        buf = io.StringIO()
        render.print_tree("yahoo.com", edges, file=buf,
                          seed_roots=["yahoo.com"])
        out = buf.getvalue()
        self.assertIn("yahoo.com", out)
        self.assertIn("aol.com", out)
        self.assertIn("cs.com", out)
        self.assertIn("san_overlap", out)

    def test_tree_multiple_seeds(self):
        edges = [_edge("aol.com", "yahoo.com"), _edge("x.com", "acme.com")]
        buf = io.StringIO()
        render.print_tree("2 seeds", edges, file=buf,
                          seed_roots=["yahoo.com", "acme.com"])
        out = buf.getvalue()
        self.assertIn("aol.com", out)
        self.assertIn("x.com", out)

    def test_tree_no_edges_is_just_root(self):
        buf = io.StringIO()
        render.print_tree("yahoo.com", [], file=buf, seed_roots=["yahoo.com"])
        self.assertEqual(buf.getvalue().strip(), "\U0001f319 yahoo.com")

    def test_children_sorted_by_confidence(self):
        hi = _edge("hi.com", "seed.com", signals=("san_overlap", "asn_match"))
        lo = _edge("lo.com", "seed.com", signals=("rdap_org",))
        buf = io.StringIO()
        render.print_tree("seed.com", [lo, hi], file=buf,
                          seed_roots=["seed.com"])
        out = buf.getvalue()
        self.assertLess(out.index("hi.com"), out.index("lo.com"))


class TestFormats(unittest.TestCase):
    def setUp(self):
        self.edges = [_edge("aol.com", "yahoo.com")]

    def test_mermaid_contains_edges(self):
        m = render.to_mermaid("yahoo.com", self.edges)
        self.assertTrue(m.startswith("flowchart TD"))
        self.assertIn("aol_com", m)
        self.assertIn("40%", m)

    def test_dot_is_valid_shape(self):
        d = render.to_dot("yahoo.com", self.edges)
        self.assertIn("digraph llunascope", d)
        self.assertIn('"yahoo.com" -> "aol.com"', d)

    def test_json_parses(self):
        j = render.to_json("yahoo.com", "Yahoo", self.edges,
                           {"yahoo.com", "aol.com"}, {})
        data = json.loads(j)
        self.assertEqual(data["company"], "Yahoo")
        self.assertEqual(data["nodes"], ["aol.com", "yahoo.com"])
        self.assertEqual(data["edges"][0]["target"], "aol.com")
        self.assertEqual(data["edges"][0]["evidence"][0]["signal"],
                         "san_overlap")

    def test_html_escapes_company(self):
        h = render.to_html("<script>alert(1)</script>", "yahoo.com",
                           self.edges, {"yahoo.com", "aol.com"}, {})
        self.assertNotIn("<script>alert(1)</script>", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertIn("mermaid", h)


if __name__ == "__main__":
    unittest.main()
