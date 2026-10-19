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

    def test_mermaid_connects_seed_to_hop1(self):
        m = render.to_mermaid("yahoo.com", self.edges)
        seed_id = m.splitlines()[1].split("[")[0].strip()
        self.assertIn(f"{seed_id} --> aol_com", m)

    def test_mermaid_escapes_quotes_in_labels(self):
        m = render.to_mermaid('evil"seed', self.edges)
        # A raw '"' would break the quoted mermaid label syntax.
        self.assertNotIn('["\U0001f319 evil"seed"]', m)
        self.assertIn("#quot;", m)

    def test_mermaid_node_ids_are_unique(self):
        # 'a-b.com' and 'a_b.com' both naive-sanitize to 'a_b_com' — they
        # must not collapse into one graph node.
        edges = [_edge("a-b.com", "seed.com"), _edge("a_b.com", "seed.com")]
        m = render.to_mermaid("seed.com", edges)
        self.assertIn("a_b_com", m)
        self.assertIn("a_b_com_2", m)

    def test_dot_is_valid_shape(self):
        d = render.to_dot("yahoo.com", self.edges)
        self.assertIn("digraph llunascope", d)
        self.assertIn('"yahoo.com" -> "aol.com"', d)

    def test_dot_escapes_quotes(self):
        evil = 'evil"seed'
        d = render.to_dot(evil, self.edges)
        # A raw '"' inside a DOT label would break the syntax; it must be
        # backslash-escaped.
        self.assertIn("evil" + chr(92) + '"seed', d)
        seed_line = [ln for ln in d.splitlines()
                     if ln.strip().startswith('"SEED"')][0]
        # After removing escaped quotes, quotes must be balanced.
        stripped = seed_line.replace(chr(92) + '"', "")
        self.assertEqual(stripped.count('"') % 2, 0)

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

    def test_html_escapes_mermaid_block(self):
        # The mermaid source is embedded in the HTML body: a hostile company
        # name must not break out of the <div class="mermaid">.
        payload = '</div><script>alert(1)</script><div class="'
        h = render.to_html(payload, payload, self.edges,
                           {"yahoo.com", "aol.com"}, {})
        self.assertNotIn(payload, h)
        self.assertIn("&lt;/div&gt;", h)


if __name__ == "__main__":
    unittest.main()


class TestAnsiInjection(unittest.TestCase):
    def test_ascii_tree_strips_escapes(self):
        from llunascope.scoring import Edge, Evidence
        e = Edge(target="evil\x1b[31m.com", via="seed.com", hop=1,
                 evidences=[Evidence(signal="san", detail="x\x1b[2Jy",
                                     weight=40)],
                 confidence=40.0)
        buf = io.StringIO()
        render.print_tree("seed\x07.com", [e], file=buf, seed_roots=["seed.com"])
        out = buf.getvalue()
        # No C0 control chars survive (the ESC is gone; the leftover
        # "[31m" is inert printable text, not an escape sequence).
        self.assertFalse(any(ord(c) < 32 and c != "\n" or ord(c) == 127
                             for c in out))
        self.assertIn("evil", out)
