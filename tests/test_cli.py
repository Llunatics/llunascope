"""Unit tests for llunascope.cli — arg parsing and --seed handling. Stdlib only.

Network and pivot are monkeypatched: these tests exercise the CLI plumbing,
never the internet.
"""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from llunascope import cli, net, pivot
from llunascope.scoring import Edge


def _fake_pivot(seeds, verbose_cb=None, org_tuple_hops=1):
    res = pivot.PivotResult()
    for s in seeds:
        res.nodes.add(s)
    e = Edge("child.example", seeds[0], 1)
    e.add("san_overlap", "1 shared cert")
    res.edges.append(e.finalize())
    res.nodes.add("child.example")
    return res


class TestCliBasics(unittest.TestCase):
    def test_version(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--version"])
        self.assertEqual(rc, 0)
        self.assertIn("llunascope", buf.getvalue())

    def test_weights(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--weights"])
        self.assertEqual(rc, 0)
        self.assertIn("san_overlap", buf.getvalue())

    def test_no_args_prints_help_and_exits_2(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = cli.main([])
        self.assertEqual(rc, 2)

    def test_offline_exits_3(self):
        with mock.patch.object(net, "check_online", return_value=False):
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = cli.main(["Acme", "--non-interactive"])
        self.assertEqual(rc, 3)
        self.assertIn("no internet", buf.getvalue())


class TestSeedFlag(unittest.TestCase):
    def _run(self, argv):
        with mock.patch.object(net, "check_online", return_value=True), \
             mock.patch("llunascope.cli.pivot", side_effect=_fake_pivot):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_seed_normalizes_case_and_trailing_dot(self):
        rc, out, _ = self._run(
            ["--seed", "WWW.Example.COM.", "--roots-only", "Acme"])
        self.assertEqual(rc, 0)
        self.assertIn("example.com", out.split())
        self.assertNotIn("WWW.Example.COM.", out)

    def test_seed_strips_subdomain_and_url(self):
        rc, out, _ = self._run(
            ["--seed", "www.example.co.id,https://shop.example.com/x",
             "--roots-only", "Acme"])
        self.assertEqual(rc, 0)
        lines = out.split()
        self.assertIn("example.co.id", lines)
        self.assertIn("example.com", lines)

    def test_seed_garbage_is_dropped(self):
        rc, out, _ = self._run(
            ["--seed", "not a domain!!!,example.com", "--roots-only",
             "Acme"])
        self.assertEqual(rc, 0)
        self.assertIn("example.com", out.split())

    def test_seed_all_garbage_exits_2(self):
        rc, _, err = self._run(
            ["--seed", "!!!,???", "Acme"])
        self.assertEqual(rc, 2)
        self.assertIn("no valid domains", err)

    def test_seed_duplicates_collapsed(self):
        rc, out, _ = self._run(
            ["--seed", "Example.COM,example.com", "--roots-only",
             "Acme"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.split().count("example.com"), 1)


if __name__ == "__main__":
    unittest.main()


class TestDisambiguate(unittest.TestCase):
    def test_no_entities(self):
        self.assertIsNone(cli._disambiguate([], "Acme", True))

    def test_single_entity_auto(self):
        e = {"label": "Acme", "qid": "Q1", "sim": 0.9}
        self.assertEqual(cli._disambiguate([e], "Acme", True), e)

    def test_multi_non_interactive_picks_best(self):
        ents = [{"label": "Acme", "qid": "Q1", "sim": 0.5},
                {"label": "Acme Corp", "qid": "Q2", "sim": 0.9}]
        err = io.StringIO()
        with redirect_stderr(err):
            chosen = cli._disambiguate(ents, "Acme", True)
        self.assertEqual(chosen["qid"], "Q2")
        self.assertIn("auto-selected", err.getvalue())

    def test_multi_interactive_choice(self):
        ents = [{"label": "Acme", "qid": "Q1", "sim": 0.5},
                {"label": "Acme Corp", "qid": "Q2", "sim": 0.9}]
        with mock.patch.object(cli, "_is_tty", return_value=True), \
             mock.patch("builtins.input", return_value="2"):
            err = io.StringIO()
            with redirect_stderr(err):
                chosen = cli._disambiguate(ents, "Acme", False)
        self.assertEqual(chosen["qid"], "Q2")

    def test_interactive_reject_all(self):
        ents = [{"label": "A", "qid": "Q1", "sim": 0.5},
                {"label": "B", "qid": "Q2", "sim": 0.4}]
        with mock.patch.object(cli, "_is_tty", return_value=True), \
             mock.patch("builtins.input", return_value="0"):
            with redirect_stderr(io.StringIO()):
                self.assertIsNone(cli._disambiguate(ents, "Acme", False))
