"""Unit tests for llunascope.watch — snapshots, diffing, radar — and the
CLI plumbing for --watch / --snapshot / --state-dir / --radar.

Stdlib only. Network and pivot are monkeypatched: these tests exercise the
snapshot/diff/radar logic and CLI exit codes, never the internet.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from llunascope import cli, net, pivot, watch
from llunascope.scoring import Edge


# ---------------------------------------------------------------------------
# Helpers: fake PivotResults and snapshots
# ---------------------------------------------------------------------------

def _edge(target, via, signals=("san_overlap",), conf=40.0):
    e = Edge(target, via, 1)
    for s in signals:
        e.add(s, f"{s} evidence for {target}")
    return e.finalize()


def _result(nodes, edges, asn=None, certs=None, orgs=None):
    res = pivot.PivotResult()
    res.nodes = set(nodes)
    res.edges = list(edges)
    res.asn_by_node = dict(asn or {})
    res.cert_map = {k: set(v) for k, v in (certs or {}).items()}
    res.org_identities = dict(orgs or {})
    return res


def _snap(company, nodes, edges, asn=None, certs=None, created="2026-11-20T00:00:00+00:00"):
    res = _result(nodes, edges, asn, certs)
    snap = watch.snapshot_from_result(company, sorted(nodes)[:1] or ["seed.example"], res)
    snap["created_at"] = created  # pin for deterministic assertions
    return snap


def _write(path, snap):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap), encoding="utf-8")


def _fake_pivot_factory(res):
    def _fake(seeds, verbose_cb=None, org_tuple_hops=1):
        return res
    return _fake


class TestSnapshotRoundTrip(unittest.TestCase):
    def test_save_load_round_trip(self):
        snap = _snap("Acme", ["acme.com", "child.example"],
                     [_edge("child.example", "acme.com")],
                     asn={"acme.com": 1234, "child.example": 1234},
                     certs={"555": {"acme.com", "child.example"}})
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "acme.json"
            watch.save_snapshot(p, snap)
            loaded = watch.load_snapshot(p)
        self.assertEqual(loaded, snap)

    def test_snapshot_is_deterministic(self):
        # Same run, different insertion order -> identical file (except ts).
        r1 = _result(["b.example", "a.example"],
                     [_edge("b.example", "a.example"),
                      _edge("a.example", "seed.example")])
        r2 = _result(["a.example", "b.example"],
                     [_edge("a.example", "seed.example"),
                      _edge("b.example", "a.example")])
        s1 = watch.snapshot_from_result("Acme", ["seed.example"], r1)
        s2 = watch.snapshot_from_result("Acme", ["seed.example"], r2)
        s1.pop("created_at")
        s2.pop("created_at")
        self.assertEqual(s1, s2)

    def test_snapshot_format_fields(self):
        from llunascope import __version__
        snap = _snap("Acme", ["acme.com"], [])
        self.assertEqual(snap["tool"], "llunascope")
        self.assertEqual(snap["format_version"], 1)
        # Pinned to the live package version, not a literal: a version
        # bump used to break this test (1.2.0 -> 1.2.1, audit 28 Nov 2026).
        self.assertEqual(snap["version"], __version__)
        self.assertEqual(snap["company"], "Acme")
        self.assertIn("created_at", snap)
        self.assertIn("certs", snap)
        self.assertIn("asn_by_node", snap)

    def test_load_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(watch.load_snapshot(Path(d) / "nope.json"))

    def test_load_corrupt_raises_watch_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(watch.WatchError):
                watch.load_snapshot(p)

    def test_load_wrong_shape_raises_watch_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "wrong.json"
            p.write_text(json.dumps({"tool": "other", "nodes": [], "edges": []}),
                         encoding="utf-8")
            with self.assertRaises(watch.WatchError):
                watch.load_snapshot(p)

    def test_load_hostile_field_types_raise_watch_error(self):
        # Audit Sat 2026-11-21: shape-valid JSON with hostile field types
        # used to crash watch/radar with a bare traceback (exit 1).
        # The loader is the single gate: all of these must be WatchError.
        good = _snap("Acme", ["acme.com"], [])
        hostile = {
            "certs as list": {"certs": ["c1"]},
            "cert value int": {"certs": {"c1": 5}},
            "cert value string": {"certs": {"c1": "acme.com"}},
            "asn_by_node as list": {"asn_by_node": ["x"]},
            "asn value unhashable": {"asn_by_node": {"acme.com": [123]}},
            "asn value bool": {"asn_by_node": {"acme.com": True}},
            "node non-string": {"nodes": ["acme.com", 5]},
            "edge non-dict": {"edges": ["not-a-dict"]},
            "edge evidence string": {"edges": [{"target": "acme.com",
                                                "via": "x",
                                                "evidence": "SAN"}]},
            "edge evidence item non-dict": {"edges": [{"target": "acme.com",
                                                       "via": "x",
                                                       "evidence": ["SAN"]}]},
            "company non-string": {"company": 123},
            "org_identities value non-string": {"org_identities": {"acme.com": 7}},
            "seeds non-string": {"seeds": ["acme.com", 9]},
        }
        with tempfile.TemporaryDirectory() as d:
            for label, override in hostile.items():
                snap = dict(good)
                snap.update(override)
                p = Path(d) / "hostile.json"
                p.write_text(json.dumps(snap), encoding="utf-8")
                with self.subTest(label=label):
                    with self.assertRaises(watch.WatchError):
                        watch.load_snapshot(p)

    def test_load_rejects_non_string_created_at(self):
        # Audit Sun 2026-11-22: created_at was not shape-checked, so a
        # hostile string reached the watch report's "since ..." clause
        # unescaped; a non-string rendered as repr noise.
        good = _snap("Acme", ["acme.com"], [])
        with tempfile.TemporaryDirectory() as d:
            for label, bad in {"int": 123, "dict": {"x": 1},
                               "list": ["2026-01-01"]}.items():
                snap = dict(good)
                snap["created_at"] = bad
                p = Path(d) / "hostile.json"
                p.write_text(json.dumps(snap), encoding="utf-8")
                with self.subTest(label=label):
                    with self.assertRaises(watch.WatchError):
                        watch.load_snapshot(p)
            # Missing created_at stays tolerated (forward compat).
            snap = dict(good)
            del snap["created_at"]
            p = Path(d) / "nocreated.json"
            p.write_text(json.dumps(snap), encoding="utf-8")
            self.assertIsNotNone(watch.load_snapshot(p))

    def test_load_tolerates_unknown_extra_keys(self):
        # Forward compatibility: a newer snapshot with extra fields
        # must still load.
        snap = _snap("Acme", ["acme.com"], [])
        snap["future_field"] = {"nested": [1, 2, 3]}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "future.json"
            p.write_text(json.dumps(snap), encoding="utf-8")
            self.assertEqual(watch.load_snapshot(p)["future_field"],
                             {"nested": [1, 2, 3]})

    def test_save_unwritable_raises_watch_error(self):
        # Parent path is a regular file, not a directory: mkdir must fail.
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            with self.assertRaises(watch.WatchError):
                watch.save_snapshot(blocker / "snap.json", {"tool": "x"})

    def test_company_mismatch_raises(self):
        snap = _snap("Acme", ["acme.com"], [])
        with self.assertRaises(watch.WatchError):
            watch.check_snapshot_company(snap, "Globex", "/tmp/acme.json")

    def test_company_match_ignores_case_and_suffix(self):
        snap = _snap("PT Bank Mandiri (Persero) Tbk", ["bankmandiri.co.id"], [])
        # Same company under a different spelling must not raise.
        watch.check_snapshot_company(snap, "bank mandiri", "/tmp/x.json")

    def test_default_snapshot_path(self):
        p = watch.default_snapshot_path("PT Bank Mandiri (Persero) Tbk")
        self.assertEqual(p.name, "bank-mandiri.json")
        self.assertEqual(p.parent.name, "llunascope")
        p2 = watch.default_snapshot_path("Acme", state_dir="/tmp/state")
        self.assertEqual(str(p2), "/tmp/state/acme.json")


class TestDiff(unittest.TestCase):
    def test_added_removed_unchanged(self):
        old = _snap("Acme", ["acme.com", "gone.example"],
                    [_edge("gone.example", "acme.com")])
        new = _snap("Acme", ["acme.com", "fresh.example"],
                    [_edge("fresh.example", "acme.com")])
        d = watch.diff_snapshots(old, new)
        self.assertEqual(d.new_nodes, ["fresh.example"])
        self.assertEqual(d.removed_nodes, ["gone.example"])
        self.assertEqual(d.new_count, 1)  # edge follows the new node: no double count
        self.assertEqual(len(d.new_edges), 1)
        self.assertEqual(len(d.removed_edges), 1)

    def test_quiet_repeat(self):
        snap = _snap("Acme", ["acme.com", "child.example"],
                     [_edge("child.example", "acme.com")])
        d = watch.diff_snapshots(snap, snap)
        self.assertEqual(d.new_count, 0)
        self.assertEqual(d.new_nodes, [])
        self.assertEqual(d.new_edges, [])

    def test_new_signal_on_known_domain_counts(self):
        old = _snap("Acme", ["acme.com", "child.example"],
                    [_edge("child.example", "acme.com", ("san_overlap",))])
        new = _snap("Acme", ["acme.com", "child.example"],
                    [_edge("child.example", "acme.com",
                           ("san_overlap", "asn_match"))])
        d = watch.diff_snapshots(old, new)
        self.assertEqual(d.new_nodes, [])
        self.assertEqual(d.new_count, 1)
        self.assertEqual(len(d.new_edges), 1)

    def test_diff_is_order_insensitive(self):
        old = _snap("Acme", ["acme.com", "a.example", "b.example"],
                    [_edge("a.example", "acme.com"),
                     _edge("b.example", "acme.com")])
        new = _snap("Acme", ["b.example", "acme.com", "a.example"],
                    [_edge("b.example", "acme.com"),
                     _edge("a.example", "acme.com")])
        self.assertEqual(watch.diff_snapshots(old, new).new_count, 0)

    def test_empty_graph_diffs_cleanly(self):
        old = _snap("Acme", ["acme.com"], [])
        new = _snap("Acme", ["acme.com"], [])
        self.assertEqual(watch.diff_snapshots(old, new).new_count, 0)

    def test_report_strips_control_chars(self):
        # A hostile company string must not inject terminal escapes.
        d = watch.WatchDiff(new_nodes=["evil\x07.example"])
        report = watch.format_watch_report(d, "2026-11-20T00:00:00+00:00",
                                           "/tmp/acme.json")
        self.assertNotIn("\x07", report)
        self.assertIn("evil.example", report)

    def test_report_strips_control_chars_from_created_at_and_signals(self):
        # Audit Sun 2026-11-22: the "since <created_at>" clause and the
        # evidence signal names were printed raw — a hand-edited snapshot
        # could inject newlines/ANSI escapes into the watch report.
        # _term_safe strips the controls; the printable leftovers
        # ("[31mFAKE-ALERT") are inert text on the same line, they can
        # no longer forge a line of their own.
        hostile_ts = "2026-11-20T00:00:00+00:00\n\x1b[31mFAKE-ALERT\x1b[0m"
        d = watch.WatchDiff(new_nodes=["new.example"])
        report = watch.format_watch_report(d, hostile_ts, "/tmp/acme.json")
        self.assertNotIn("\x1b", report)
        self.assertNotIn("\nFAKE-ALERT", report)
        self.assertIn("new.example", report)
        evil_edge = {"target": "known.example", "via": "acme.com",
                     "evidence": [{"signal": "san_overlap\n\x1b[31mEVIL",
                                   "detail": "x", "weight": 30}]}
        d2 = watch.WatchDiff(new_edges=[evil_edge])
        report2 = watch.format_watch_report(
            d2, "2026-11-20T00:00:00+00:00", "/tmp/acme.json")
        self.assertNotIn("\x1b", report2)
        self.assertNotIn("\nEVIL", report2)
        self.assertIn("san_overlap", report2)

    def test_quiet_report(self):
        report = watch.format_watch_report(watch.WatchDiff(),
                                           "2026-11-20T00:00:00+00:00",
                                           "/tmp/acme.json")
        self.assertIn("no new shadow assets", report)


class TestRadar(unittest.TestCase):
    def _pair(self, **kw):
        a = _snap("Acme", ["acme.com", "acme.net"],
                  [_edge("acme.net", "acme.com")],
                  asn={"acme.com": 111, "acme.net": 111},
                  certs={"100": {"acme.com", "acme.net"}})
        b = _snap("Globex", ["globex.com", "globex.org"],
                  [_edge("globex.org", "globex.com")],
                  asn={"globex.com": 222, "globex.org": 222},
                  certs={"200": {"globex.com", "globex.org"}})
        a.update(kw.get("a", {}))
        b.update(kw.get("b", {}))
        return a, b

    def test_no_convergence_when_disjoint(self):
        a, b = self._pair()
        res = watch.detect_convergence(a, b)
        self.assertFalse(res.converged)
        self.assertEqual(res.total_shared_certs, 0)
        self.assertEqual(res.total_shared_asns, 0)

    def test_new_shared_cert(self):
        a, b = self._pair()
        # One cert now spans both orgs: classic acquisition signal.
        a["certs"]["999"] = ["acme.com", "globex.com"]
        b["certs"]["999"] = ["acme.com", "globex.com"]
        res = watch.detect_convergence(a, b)
        self.assertTrue(res.converged)
        self.assertEqual(len(res.new_shared_certs), 1)
        self.assertEqual(res.new_shared_certs[0].cert_id, "999")
        self.assertEqual(res.new_shared_certs[0].roots_a, ["acme.com"])
        self.assertEqual(res.new_shared_certs[0].roots_b, ["globex.com"])

    def test_new_shared_asn(self):
        a, b = self._pair()
        b["asn_by_node"]["globex.com"] = 111  # Globex moved onto Acme's ASN
        res = watch.detect_convergence(a, b)
        self.assertTrue(res.converged)
        self.assertEqual(len(res.new_shared_asns), 1)
        self.assertEqual(res.new_shared_asns[0].asn, 111)

    def test_cert_in_both_maps_but_one_sided_is_not_shared(self):
        a, b = self._pair()
        # Same cert id in both snapshots, but its SANs only touch Acme.
        a["certs"]["999"] = ["acme.com", "acme.net"]
        b["certs"]["999"] = ["acme.com", "acme.net"]
        res = watch.detect_convergence(a, b)
        self.assertFalse(res.converged)

    def test_baseline_suppresses_known_convergence(self):
        a, b = self._pair()
        a["certs"]["999"] = ["acme.com", "globex.com"]
        b["certs"]["999"] = ["acme.com", "globex.com"]
        base_a, base_b = self._pair()
        base_a["certs"]["999"] = ["acme.com", "globex.com"]
        base_b["certs"]["999"] = ["acme.com", "globex.com"]
        res = watch.detect_convergence(a, b, base_a, base_b)
        self.assertFalse(res.converged)
        self.assertEqual(res.total_shared_certs, 1)  # still shared, just old news
        self.assertTrue(res.had_baseline)

    def test_baseline_only_one_side_raises(self):
        a, b = self._pair()
        with self.assertRaises(watch.WatchError):
            watch.detect_convergence(a, b, base_a=a)

    def test_radar_report_strips_control_chars(self):
        a, b = self._pair()
        a["company"] = "Acme\x1b[31m"
        res = watch.detect_convergence(a, b)
        self.assertNotIn("\x1b", watch.format_radar_report(res))

    def test_radar_report_strips_control_chars_from_cert_id(self):
        # Audit Sun 2026-11-22: the shared cert id was printed raw — a
        # hand-edited snapshot could inject escapes into the radar report.
        a, b = self._pair()
        evil_id = "999\n\x1b[31mEVIL-CERT"
        a["certs"][evil_id] = ["acme.com", "globex.com"]
        b["certs"][evil_id] = ["acme.com", "globex.com"]
        res = watch.detect_convergence(a, b)
        report = watch.format_radar_report(res)
        self.assertNotIn("\x1b", report)
        self.assertNotIn("\nEVIL-CERT", report)
        self.assertIn("999", report)

    def test_same_company_radar_raises(self):
        # Audit Sun 2026-11-22: two snapshots of the same company share
        # every cert/ASN by construction, so radar reported "NEW
        # convergence" (exit 10) — a false M&A signal from an operator
        # typo or the same file passed twice.
        a, b = self._pair()
        b["company"] = "Acme Inc"  # normalizes to the same company
        b["certs"]["999"] = ["acme.com", "globex.com"]
        a["certs"]["999"] = ["acme.com", "globex.com"]
        with self.assertRaises(watch.WatchError):
            watch.detect_convergence(a, b)
        # Unknown companies (empty after normalization) still pass.
        c, d = self._pair()
        c.pop("company")
        d.pop("company")
        self.assertIsNotNone(watch.detect_convergence(c, d))


class TestWatchCli(unittest.TestCase):
    def _run(self, argv, fake_res):
        with mock.patch.object(net, "check_online", return_value=True), \
             mock.patch("llunascope.cli.pivot",
                        side_effect=_fake_pivot_factory(fake_res)):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_first_run_saves_baseline_rc0(self):
        res = _result(["example.com", "child.example"],
                      [_edge("child.example", "example.com")])
        with tempfile.TemporaryDirectory() as d:
            snap = str(Path(d) / "acme.json")
            rc, out, err = self._run(
                ["--seed", "example.com", "--watch", "--snapshot", snap,
                 "--roots-only", "Acme"], res)
            self.assertEqual(rc, 0)
            self.assertIn("baseline saved", err)
            self.assertTrue(Path(snap).exists())
            self.assertEqual(watch.load_snapshot(snap)["company"], "Acme")

    def test_repeat_run_quiet_rc0(self):
        res = _result(["example.com", "child.example"],
                      [_edge("child.example", "example.com")])
        with tempfile.TemporaryDirectory() as d:
            snap = str(Path(d) / "acme.json")
            self._run(["--seed", "example.com", "--watch", "--snapshot", snap,
                       "--roots-only", "Acme"], res)
            rc, _, err = self._run(
                ["--seed", "example.com", "--watch", "--snapshot", snap,
                 "--roots-only", "Acme"], res)
            self.assertEqual(rc, 0)
            self.assertIn("no new shadow assets", err)

    def test_new_asset_rc10(self):
        old_res = _result(["example.com", "child.example"],
                          [_edge("child.example", "example.com")])
        new_res = _result(["example.com", "child.example", "fresh.example"],
                          [_edge("child.example", "example.com"),
                           _edge("fresh.example", "example.com")])
        with tempfile.TemporaryDirectory() as d:
            snap = str(Path(d) / "acme.json")
            self._run(["--seed", "example.com", "--watch", "--snapshot", snap,
                       "--roots-only", "Acme"], old_res)
            rc, _, err = self._run(
                ["--seed", "example.com", "--watch", "--snapshot", snap,
                 "--roots-only", "Acme"], new_res)
            self.assertEqual(rc, 10)
            self.assertIn("1 new shadow asset", err)
            self.assertIn("fresh.example", err)

    def test_corrupt_snapshot_rc2(self):
        res = _result(["example.com"], [])
        with tempfile.TemporaryDirectory() as d:
            snap = Path(d) / "acme.json"
            snap.write_text("{broken", encoding="utf-8")
            rc, _, err = self._run(
                ["--seed", "example.com", "--watch", "--snapshot", str(snap),
                 "--roots-only", "Acme"], res)
            self.assertEqual(rc, 2)
            self.assertIn("not valid JSON", err)

    def test_company_mismatch_rc2(self):
        res = _result(["example.com"], [])
        with tempfile.TemporaryDirectory() as d:
            snap = Path(d) / "acme.json"
            _write(snap, _snap("Globex", ["example.com"], []))
            rc, _, err = self._run(
                ["--seed", "example.com", "--watch", "--snapshot", str(snap),
                 "--roots-only", "Acme"], res)
            self.assertEqual(rc, 2)
            self.assertIn("belongs to", err)

    def test_save_failure_rc2(self):
        res = _result(["example.com"], [])
        with tempfile.TemporaryDirectory() as d:
            snap = str(Path(d) / "acme.json")
            with mock.patch.object(watch, "save_snapshot",
                                  side_effect=watch.WatchError("disk full")):
                rc, _, err = self._run(
                    ["--seed", "example.com", "--watch", "--snapshot", snap,
                     "--roots-only", "Acme"], res)
            self.assertEqual(rc, 2)
            self.assertIn("disk full", err)

    def test_state_dir_flag(self):
        res = _result(["example.com"], [])
        with tempfile.TemporaryDirectory() as d:
            rc, _, err = self._run(
                ["--seed", "example.com", "--watch", "--state-dir", d,
                 "--roots-only", "Acme"], res)
            self.assertEqual(rc, 0)
            self.assertTrue((Path(d) / "acme.json").exists())

    def test_snapshot_without_watch_rc2(self):
        res = _result(["example.com"], [])
        rc, _, err = self._run(
            ["--seed", "example.com", "--snapshot", "/tmp/x.json",
             "--roots-only", "Acme"], res)
        self.assertEqual(rc, 2)
        self.assertIn("need --watch", err)


class TestRadarCli(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_radar_new_convergence_rc10(self):
        a = _snap("Acme", ["acme.com"], [],
                  certs={"999": {"acme.com", "globex.com"}})
        b = _snap("Globex", ["globex.com"], [],
                  certs={"999": {"acme.com", "globex.com"}})
        with tempfile.TemporaryDirectory() as d:
            pa, pb = Path(d) / "a.json", Path(d) / "b.json"
            _write(pa, a)
            _write(pb, b)
            rc, out, _ = self._run(["--radar", str(pa), str(pb)])
            self.assertEqual(rc, 10)
            self.assertIn("NEW convergence", out)
            self.assertIn("999", out)

    def test_radar_quiet_rc0(self):
        a = _snap("Acme", ["acme.com"], [])
        b = _snap("Globex", ["globex.com"], [])
        with tempfile.TemporaryDirectory() as d:
            pa, pb = Path(d) / "a.json", Path(d) / "b.json"
            _write(pa, a)
            _write(pb, b)
            rc, out, _ = self._run(["--radar", str(pa), str(pb)])
            self.assertEqual(rc, 0)
            self.assertIn("no new convergence", out)

    def test_radar_baseline_quiet_rc0(self):
        a = _snap("Acme", ["acme.com"], [],
                  certs={"999": {"acme.com", "globex.com"}})
        b = _snap("Globex", ["globex.com"], [],
                  certs={"999": {"acme.com", "globex.com"}})
        with tempfile.TemporaryDirectory() as d:
            pa, pb = Path(d) / "a.json", Path(d) / "b.json"
            ba, bb = Path(d) / "ba.json", Path(d) / "bb.json"
            _write(pa, a)
            _write(pb, b)
            _write(ba, a)
            _write(bb, b)
            rc, out, _ = self._run(
                ["--radar", str(pa), str(pb),
                 "--radar-baseline", str(ba), str(bb)])
            self.assertEqual(rc, 0)
            self.assertIn("no new convergence", out)

    def test_radar_missing_file_rc2(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _, err = self._run(
                ["--radar", str(Path(d) / "a.json"), str(Path(d) / "b.json")])
            self.assertEqual(rc, 2)
            self.assertIn("not found", err)

    def test_radar_corrupt_file_rc2(self):
        with tempfile.TemporaryDirectory() as d:
            pa, pb = Path(d) / "a.json", Path(d) / "b.json"
            pa.write_text("{broken", encoding="utf-8")
            _write(pb, _snap("Globex", ["globex.com"], []))
            rc, _, err = self._run(["--radar", str(pa), str(pb)])
            self.assertEqual(rc, 2)

    def test_radar_hostile_field_types_rc2_no_traceback(self):
        # Audit Sat 2026-11-21: hostile-but-shape-valid snapshots crashed
        # the radar with a traceback (rc 1). Now: clean rc 2, no traceback.
        good_a = _snap("Acme", ["acme.com"], [])
        good_b = _snap("Globex", ["globex.com"], [])
        hostile = {
            "certs as list": {"certs": ["c1"]},
            "asn value unhashable": {"asn_by_node": {"acme.com": [123]}},
            "cert value int": {"certs": {"c1": 5}},
        }
        with tempfile.TemporaryDirectory() as d:
            pb = Path(d) / "b.json"
            _write(pb, good_b)
            for label, override in hostile.items():
                snap = dict(good_a)
                snap.update(override)
                pa = Path(d) / "a.json"
                pa.write_text(json.dumps(snap), encoding="utf-8")
                with self.subTest(label=label):
                    rc, _, err = self._run(["--radar", str(pa), str(pb)])
                    self.assertEqual(rc, 2)
                    self.assertNotIn("Traceback", err)
                    self.assertIn("invalid shape", err)

    def test_radar_same_company_rc2(self):
        # Audit Sun 2026-11-22: radar on two snapshots of one company
        # (or the same file twice) used to report NEW convergence (rc 10).
        a = _snap("Acme", ["acme.com"], [],
                  certs={"999": {"acme.com", "acme.net"}})
        b = _snap("Acme", ["acme.com"], [],
                  certs={"999": {"acme.com", "acme.net"}})
        with tempfile.TemporaryDirectory() as d:
            pa, pb = Path(d) / "a.json", Path(d) / "b.json"
            _write(pa, a)
            _write(pb, b)
            rc, _, err = self._run(["--radar", str(pa), str(pb)])
            self.assertEqual(rc, 2)
            self.assertNotIn("Traceback", err)
            self.assertIn("different companies", err)
            rc, _, err = self._run(["--radar", str(pa), str(pa)])
            self.assertEqual(rc, 2)
            self.assertNotIn("Traceback", err)

    def test_radar_baseline_without_radar_rc2(self):
        rc, _, err = self._run(
            ["--radar-baseline", "/tmp/a.json", "/tmp/b.json", "Acme"])
        self.assertEqual(rc, 2)
        self.assertIn("needs --radar", err)

    def test_radar_and_watch_conflict_rc2(self):
        rc, _, err = self._run(
            ["--radar", "/tmp/a.json", "/tmp/b.json", "--watch", "Acme"])
        self.assertEqual(rc, 2)
        self.assertIn("separate modes", err)


if __name__ == "__main__":
    unittest.main()
