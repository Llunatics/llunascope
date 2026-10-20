"""Unit tests for llunascope.net — HTTP fetching, no real network.

requests.get is stubbed at the boundary; the disk cache is redirected to a
tmp dir so tests never touch the real ~/.cache/llunascope.

Added 2026-10-20 (deep-8 mutation testing): net.py had zero test coverage —
two mutants survived here (inverted status handling, disabled cache TTL).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llunascope import net


def _resp(status=200, payload=None, text=""):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = text
    return r


class NetTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p1 = mock.patch.object(net, "CACHE_DIR", Path(self._tmp.name))
        p2 = mock.patch.object(net, "_throttle", lambda url: None)
        p3 = mock.patch.object(net.requests, "get")
        p1.start()
        p2.start()
        self.get = p3.start()
        for p in (p1, p2, p3):
            self.addCleanup(p.stop)


class TestFetchJson(NetTestBase):
    def test_200_returns_parsed_json(self):
        self.get.return_value = _resp(200, {"a": 1})
        self.assertEqual(net.fetch_json("https://x.test/y", use_cache=False),
                         {"a": 1})

    def test_non_200_returns_none(self):
        self.get.return_value = _resp(404, {"err": 1})
        self.assertIsNone(net.fetch_json("https://x.test/y", use_cache=False))

    def test_transport_error_returns_none(self):
        self.get.side_effect = Exception("boom")
        self.assertIsNone(net.fetch_json("https://x.test/y", use_cache=False))

    def test_bad_json_returns_none(self):
        r = _resp(200)
        r.json.side_effect = ValueError("nope")
        self.get.return_value = r
        self.assertIsNone(net.fetch_json("https://x.test/y", use_cache=False))

    def test_cache_hit_serves_without_network(self):
        # Deep-8 M20: CACHE_TTL=0 must not silently disable the cache.
        self.get.return_value = _resp(200, {"a": 1})
        first = net.fetch_json("https://x.test/cached")
        self.get.return_value = _resp(200, {"a": 2})  # would differ if refetched
        second = net.fetch_json("https://x.test/cached")
        self.assertEqual(first, {"a": 1})
        self.assertEqual(second, {"a": 1})
        self.assertEqual(self.get.call_count, 1)


class TestFetchText(NetTestBase):
    def test_200_returns_text(self):
        self.get.return_value = _resp(200, text="hello")
        self.assertEqual(net.fetch_text("https://x.test/y"), "hello")

    def test_non_200_returns_none(self):
        self.get.return_value = _resp(500, text="err")
        self.assertIsNone(net.fetch_text("https://x.test/y"))

    def test_transport_error_returns_none(self):
        self.get.side_effect = Exception("boom")
        self.assertIsNone(net.fetch_text("https://x.test/y"))


if __name__ == "__main__":
    unittest.main()
