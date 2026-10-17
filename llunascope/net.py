"""HTTP fetching with politeness: timeouts, per-host rate limiting, disk cache.

Every external call goes through here. A dead source returns None — it never
crashes the run. Fully passive: we only ever talk to public APIs, never to
target infrastructure.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

CACHE_DIR = Path(os.environ.get("LLUNASCOPE_CACHE", Path.home() / ".cache" / "llunascope"))
CACHE_TTL = 7 * 24 * 3600  # 7 days

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
    cache_key = url + ("?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items())) if params else "")
    path = _cache_path(cache_key)
    if use_cache and path.exists():
        try:
            if time.time() - path.stat().st_mtime < CACHE_TTL:
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass

    try:
        _throttle(url)
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        resp = requests.get(url, params=params, headers=hdrs, timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    if use_cache:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data)[:5_000_000], encoding="utf-8")
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
