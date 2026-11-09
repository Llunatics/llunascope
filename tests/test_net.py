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


def _resp(status=200, payload=None, text="", headers=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = text
    r.headers = headers or {}
    return r


class NetTestBase(unittest.TestCase):
    def setUp(self):
        net.clear_mem_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(net.clear_mem_cache)
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

    def test_429_retry_after_success(self):
        r429 = _resp(429, headers={"Retry-After": "2"})
        r200 = _resp(200, {"ok": 1})
        self.get.side_effect = [r429, r200]
        with mock.patch("time.sleep") as mock_sleep:
            res = net.fetch_json("https://x.test/rate-limited", use_cache=False)
        self.assertEqual(res, {"ok": 1})
        self.assertEqual(self.get.call_count, 2)
        mock_sleep.assert_called_with(2.0)

    def test_429_exhaust_retries_returns_none(self):
        r429 = _resp(429, headers={"Retry-After": "1"})
        self.get.return_value = r429
        with mock.patch("time.sleep"):
            res = net.fetch_json("https://x.test/rate-limited-forever", use_cache=False)
        self.assertIsNone(res)
        self.assertEqual(self.get.call_count, 3)

    def test_corrupted_cache_file_treated_as_miss(self):
        # Write corrupted JSON to cache file
        cache_path = net._cache_path("https://x.test/corrupt")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("{corrupt json !!!", encoding="utf-8")

        self.get.return_value = _resp(200, {"recovered": True})
        res = net.fetch_json("https://x.test/corrupt")
        self.assertEqual(res, {"recovered": True})
        self.assertEqual(self.get.call_count, 1)

    def test_in_memory_cache_deduplication(self):
        self.get.return_value = _resp(200, {"data": 42})
        first = net.fetch_json("https://x.test/mem-cached")
        second = net.fetch_json("https://x.test/mem-cached")
        self.assertEqual(first, {"data": 42})
        self.assertEqual(second, {"data": 42})
        self.assertEqual(self.get.call_count, 1)


class TestSessionManagement(unittest.TestCase):
    def test_managed_session_closes_cleanly(self):
        saved_session = None
        with net.managed_session() as s:
            saved_session = s
            self.assertIsInstance(s, net.requests.Session)
            self.assertEqual(s.max_redirects, 3)
            # Patch close on adapter to ensure adapter.close() is called
            with mock.patch.object(s, "close", wraps=s.close) as mock_close:
                pass
        # Assert close method was called on exit
        s_obj = net.create_session()
        with mock.patch.object(s_obj, "close") as mock_close:
            with mock.patch("llunascope.net.create_session", return_value=s_obj):
                with net.managed_session() as s:
                    pass
                mock_close.assert_called_once()


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
