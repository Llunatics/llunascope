"""HTTP fetching with politeness: timeouts, per-host rate limiting, disk cache.

Every external call goes through here. A dead source returns None — it never
crashes the run. Fully passive: we only ever talk to public APIs, never to
target infrastructure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests

CACHE_DIR = Path(os.environ.get("LLUNASCOPE_CACHE", Path.home() / ".cache" / "llunascope"))
CACHE_TTL = 7 * 24 * 3600  # 7 days
MAX_CACHE_FILE_SIZE = 1_000_000  # 1 MB
MAX_FILES_PER_HOST = 100

# Minimum seconds between requests to the same host.
HOST_DELAYS = {
    "crt.sh": 1.0,
    "api.certspotter.com": 1.0,
    "query.wikidata.org": 1.0,
    "www.sec.gov": 0.25,          # SEC limit is 10 req/s; we stay well under
    "stat.ripe.net": 0.5,
    "www.peeringdb.com": 1.0,
    "data.iana.org": 2.0,
}
DEFAULT_DELAY = 0.5

_last_hit: dict[str, float] = {}
_lock = threading.Lock()

USER_AGENT = "Llunascope/1.0 (passive EASM research; admin@llunaticsys.web.id)"

# In-memory bounded LRU cache
MAX_MEM_CACHE_ENTRIES = 256
MEM_CACHE_TTL = 3600  # 1 hour
_mem_cache: OrderedDict[str, tuple[float, any]] = OrderedDict()
_mem_lock = threading.Lock()


def create_session() -> requests.Session:
    """Create a configured requests.Session with bounded redirects."""
    s = requests.Session()
    s.max_redirects = 3
    return s


@contextmanager
def managed_session():
    """Context manager for requests.Session with guaranteed cleanup."""
    s = create_session()
    try:
        yield s
    finally:
        s.close()


def _mem_cache_key(url: str, params: dict | None = None) -> str:
    param_str = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items())) if params else ""
    return f"{url}?{param_str}" if param_str else url


def _mem_cache_get(key: str) -> any:
    with _mem_lock:
        if key in _mem_cache:
            ts, val = _mem_cache[key]
            if time.time() - ts < MEM_CACHE_TTL:
                _mem_cache.move_to_end(key)
                return val
            del _mem_cache[key]
    return None


def _mem_cache_set(key: str, val: any) -> None:
    with _mem_lock:
        if key in _mem_cache:
            _mem_cache.move_to_end(key)
        _mem_cache[key] = (time.time(), val)
        if len(_mem_cache) > MAX_MEM_CACHE_ENTRIES:
            _mem_cache.popitem(last=False)


def clear_mem_cache() -> None:
    with _mem_lock:
        _mem_cache.clear()


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:32]
    host = urlparse(url).netloc.replace(":", "_")
    return CACHE_DIR / host / (digest + ".json")


def _throttle(url: str) -> None:
    host = urlparse(url).netloc
    delay = HOST_DELAYS.get(host, DEFAULT_DELAY)
    with _lock:
        last = _last_hit.get(host, 0.0)
        wait = delay - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.time()


def fetch_json(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 20,
    use_cache: bool = True,
) -> dict | list | None:
    """GET a JSON endpoint. Returns parsed JSON, or None on any failure."""
    cache_key = _mem_cache_key(url, params)
    if use_cache:
        mem_hit = _mem_cache_get(cache_key)
        if mem_hit is not None:
            return mem_hit

    path = _cache_path(cache_key)
    if use_cache and path.exists():
        try:
            if time.time() - path.stat().st_mtime < CACHE_TTL:
                data = json.loads(path.read_text(encoding="utf-8"))
                _mem_cache_set(cache_key, data)
                return data
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    try:
        _throttle(url)
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        resp = requests.get(url, params=params, headers=hdrs, timeout=timeout)
        attempt = 0
        while resp.status_code == 429 and attempt < 2:
            retry_after = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
            try:
                wait_time = min(float(retry_after), 5.0) if retry_after else min(2 ** attempt, 5.0)
            except (ValueError, TypeError):
                wait_time = min(2 ** attempt, 5.0)
            logging.warning("HTTP 429 rate limit for %s, backing off %ss (attempt %d)",
                            url, wait_time, attempt + 1)
            time.sleep(wait_time)
            attempt += 1
            _throttle(url)
            resp = requests.get(url, params=params, headers=hdrs, timeout=timeout)

        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    if use_cache and data is not None:
        _mem_cache_set(cache_key, data)
        try:
            serialized = json.dumps(data)
            if len(serialized.encode("utf-8")) <= MAX_CACHE_FILE_SIZE:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(serialized, encoding="utf-8")
                # Enforce host directory size limit (LRU eviction by mtime)
                host_dir = path.parent
                cache_files = list(host_dir.glob("*.json"))
                if len(cache_files) > MAX_FILES_PER_HOST:
                    cache_files.sort(key=lambda f: f.stat().st_mtime)
                    for f in cache_files[:-MAX_FILES_PER_HOST]:
                        f.unlink(missing_ok=True)
        except Exception:
            pass
    return data


def fetch_text(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 20,
) -> str | None:
    """GET a text endpoint. Returns text, or None on any failure. No cache."""
    try:
        _throttle(url)
        hdrs = {"User-Agent": USER_AGENT}
        if headers:
            hdrs.update(headers)
        resp = requests.get(url, params=params, headers=hdrs, timeout=timeout)
        attempt = 0
        while resp.status_code == 429 and attempt < 2:
            retry_after = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
            try:
                wait_time = min(float(retry_after), 5.0) if retry_after else min(2 ** attempt, 5.0)
            except (ValueError, TypeError):
                wait_time = min(2 ** attempt, 5.0)
            logging.warning("HTTP 429 rate limit for %s, backing off %ss (attempt %d)",
                            url, wait_time, attempt + 1)
            time.sleep(wait_time)
            attempt += 1
            _throttle(url)
            resp = requests.get(url, params=params, headers=hdrs, timeout=timeout)

        if resp.status_code != 200:
            return None
        return resp.text
    except Exception:
        return None


def check_online() -> bool:
    """Quick connectivity probe against a reliable public API."""
    try:
        _throttle("https://stat.ripe.net/")
        requests.get("https://stat.ripe.net/data/status/data.json",
                     headers={"User-Agent": USER_AGENT}, timeout=10)
        return True
    except Exception:
        return False
