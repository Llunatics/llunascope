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
        self.assertIn("cytoscape", h)

    def test_html_escapes_json_block(self):
        # The graph data rides in a <script type="application/json"> block:
        # a hostile company name must not break out of it. Every '<' in
        # the JSON is Unicode-escaped, so no literal '</script' survives.
        payload = '</script><script>alert(1)</script>'
        h = render.to_html(payload, payload, self.edges,
                           {"yahoo.com", "aol.com"}, {})
        self.assertNotIn(payload, h)
        # Exactly three real script tags: the Cytoscape CDN include, the
        # JSON data block, and the inline controller. A fourth would mean
        # the payload broke out.
        self.assertEqual(h.count("<script"), 3)
        self.assertEqual(h.count("</script>"), 3)
        self.assertIn("&lt;/script&gt;", h)

    def test_html_has_no_mermaid(self):
        # v1.2.0: the static Mermaid embed is replaced by Cytoscape.js.
        h = render.to_html("Yahoo", "yahoo.com", self.edges,
                           {"yahoo.com", "aol.com"}, {})
        self.assertNotIn("mermaid", h.lower())
        self.assertIn("cytoscape", h)


class TestHtmlGraph(unittest.TestCase):
    """Interactive Cytoscape.js graph embedded in --format html."""

    def setUp(self):
        self.edges = [
            _edge("aol.com", "SEED", hop=1, signals=("san_overlap",)),
            _edge("cs.com", "aol.com", hop=2,
                  signals=("san_overlap", "asn_match")),
        ]
        self.nodes = {"yahoo.com", "aol.com", "cs.com"}
        self.html = render.to_html("Yahoo", "yahoo.com", self.edges,
                                   self.nodes, {})

    def _payload(self):
        """Parse the embedded graph JSON out of the HTML."""
        marker = '<script type="application/json" id="llunascope-graph">'
        start = self.html.index(marker) + len(marker)
        end = self.html.index("</script>", start)
        return json.loads(self.html[start:end])

    def test_graph_json_parses(self):
        data = self._payload()
        self.assertEqual(data["company"], "Yahoo")
        self.assertEqual(data["seed_domains"], ["yahoo.com"])
        # SEED meta-node + 2 discovered domains + 2 edges (the seed domain
        # itself is not a graph node — the SEED node represents it).
        self.assertEqual(len(data["elements"]), 1 + 2 + 2)

    def test_seed_node_and_hop1_edges(self):
        data = self._payload()
        by_id = {e["data"]["id"]: e["data"] for e in data["elements"]}
        by_target = {e["data"]["target"]: e["data"] for e in data["elements"]
                     if "target" in e["data"]}
        self.assertEqual(by_id["SEED"]["kind"], "seed")
        self.assertEqual(by_id["aol.com"]["kind"], "domain")
        self.assertEqual(by_id["aol.com"]["hop"], 1)
        self.assertEqual(by_id["cs.com"]["hop"], 2)
        self.assertEqual(by_target["aol.com"]["source"], "SEED")
        self.assertEqual(by_target["cs.com"]["source"], "aol.com")

    def test_edge_carries_confidence_and_evidence(self):
        data = self._payload()
        by_id = {e["data"]["id"]: e["data"] for e in data["elements"]}
        by_target = {e["data"]["target"]: e["data"] for e in data["elements"]
                     if "target" in e["data"]}
        # cs.com edge: san_overlap(40) + asn_match(25) = 65.
        self.assertEqual(by_target["cs.com"]["confidence"], 65)
        signals = [x["signal"] for x in by_target["cs.com"]["evidence"]]
        self.assertEqual(signals, ["san_overlap", "asn_match"])
        # Node confidence = strongest incoming edge.
        self.assertEqual(by_id["cs.com"]["confidence"], 65)
        self.assertEqual(by_id["aol.com"]["confidence"], 40)

    def test_controls_present(self):
        for token in ("layout-select", "conf-slider", "search",
                      "fit-btn", "reset-btn", "details", "id=\"cy\""):
            self.assertIn(token, self.html)

    def test_hostile_labels_cannot_break_json_block(self):
        evil = 'x</script><script>alert(1)</script>'
        h = render.to_html(evil, evil, self.edges, self.nodes, {})
        self.assertNotIn(evil, h)
        self.assertEqual(h.count("<script"), 3)
        # The escaped payload still decodes back to the original string.
        marker = '<script type="application/json" id="llunascope-graph">'
        start = h.index(marker) + len(marker)
        end = h.index("</script>", start)
        data = json.loads(h[start:end])
        self.assertEqual(data["company"], evil)

    def test_pivot_shaped_edges_rewrite_hop1_source_to_seed(self):
        # The real pivot records hop-1 parents as the SEED DOMAIN (via=
        # 'yahoo.com'), never as 'SEED' (pivot.py builds Edge(rd, domain,
        # hop+1); test_pivot.py asserts e.via == 'seed.com'). Seed domains
        # are not graph nodes, so the payload must rewrite such sources
        # to the SEED meta-node — Cytoscape.js throws on an edge whose
        # source node does not exist and the whole graph renders empty.
        edges = [_edge("aol.com", "yahoo.com", hop=1),
                 _edge("cs.com", "aol.com", hop=2,
                       signals=("san_overlap", "asn_match"))]
        h = render.to_html("Yahoo", "yahoo.com", edges,
                           {"yahoo.com", "aol.com", "cs.com"}, {})
        marker = '<script type="application/json" id="llunascope-graph">'
        start = h.index(marker) + len(marker)
        end = h.index("</script>", start)
        data = json.loads(h[start:end])
        ids = {e["data"]["id"] for e in data["elements"]}
        for el in data["elements"]:
            d = el["data"]
            if "source" in d:
                self.assertIn(d["source"], ids,
                              f"edge {d['id']} dangles: source {d['source']!r} is not a node")
                self.assertIn(d["target"], ids)
        by_target = {e["data"]["target"]: e["data"] for e in data["elements"]
                     if "target" in e["data"]}
        self.assertEqual(by_target["aol.com"]["source"], "SEED")

    def test_template_tokens_round_trip(self):
        # A company/evidence string containing a template token must come
        # back verbatim: sequential str.replace() used to rescan inserted
        # text, so '%%ROWS%%' in the company filled the title with table
        # rows + graph JSON and rewrote the embedded JSON payload.
        evil = "%%ROWS%% %%GRAPH_JSON%% %%N_NODES%% %%N_EDGES%% %%COMPANY_HTML%%"
        e = Edge("aol.com", "SEED", 1)
        e.add("san_overlap", "saw " + evil)
        e = e.finalize()
        h = render.to_html(evil, "seed.com", [e], {"seed.com", "aol.com"}, {})
        title = h.split("<title>")[1].split("</title>")[0]
        self.assertNotIn("<tr>", title)
        self.assertNotIn("elements", title)
        marker = '<script type="application/json" id="llunascope-graph">'
        start = h.index(marker) + len(marker)
        end = h.index("</script>", start)
        data = json.loads(h[start:end])
        self.assertEqual(data["company"], evil)
        self.assertEqual(data["elements"][-1]["data"]["evidence"][0]["detail"],
                         "saw " + evil)

    def test_inline_controller_parses(self):
        # Guard the controller JS (incl. the try/catch around the
        # Cytoscape init) against syntax errors: extract the last
        # <script> block and run node --check on it.
        import subprocess
        import tempfile
        import os
        scripts = []
        rest = self.html
        while "<script>" in rest:
            s = rest.index("<script>") + len("<script>")
            e = rest.index("</script>", s)
            scripts.append(rest[s:e])
            rest = rest[e + len("</script>"):]
        self.assertTrue(scripts)
        with tempfile.NamedTemporaryFile("w", suffix=".js",
                                        delete=False) as f:
            f.write(scripts[-1])
            path = f.name
        try:
            proc = subprocess.run(["node", "--check", path],
                                  capture_output=True, text=True)
        except FileNotFoundError:
            self.skipTest("node not available")
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_empty_graph_still_valid(self):
        h = render.to_html("Acme", "acme.com", [], {"acme.com"}, {})
        marker = '<script type="application/json" id="llunascope-graph">'
        start = h.index(marker) + len(marker)
        end = h.index("</script>", start)
        data = json.loads(h[start:end])
        self.assertEqual(data["seed_domains"], ["acme.com"])
        self.assertEqual(len(data["elements"]), 1)  # SEED meta-node only
        self.assertIn("Cytoscape.js failed to load", h)


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
