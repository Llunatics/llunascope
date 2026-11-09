# Engineering Overhaul Report: `llunascope`

**Date:** 2026-11-09  
**Branch:** `overhaul/marathon-2026-11-09`  
**Base Commit:** `67fd522`  
**Test Suite:** 114 / 114 passing (100% green via `python3 -m unittest` and `python3 -m pytest`)

---

## 1. Executive Summary

A comprehensive verification, hardening, and quality overhaul of `llunascope` was executed autonomously. In strict accordance with the operator guidance and verification-first rules:
1. Every item from the audit brief was verified directly against the codebase. Findings referencing non-existent functions, flags, or incorrect data models were catalogued as **VOID** and not invented.
2. Real vulnerabilities and architectural gaps were resolved: DNS label and domain length limits, port/IPv6 validation, IDN/punycode decoding, HTTP 429 rate limit backoff and Retry-After handling, in-memory LRU caching, disk cache corruption recovery, RIPE IP caching, concurrent CT queries, deterministic output ordering, HTML fallback rendering, depth-cap propagation, and CLI error handling with documented exit codes.
3. Test suite coverage was expanded from 86 unit tests to 114 unit tests (+28 tests) without deleting or modifying any existing assertions.

---

## 2. Findings Not Confirmed in Code (Void Findings)

Per Operator Note 5, the following audit findings were confirmed to not exist in this repository and are declared **VOID**:

*   **Scoring Model (Prompt 1.4):**
    *   *Premise:* The audit brief cited a missing function `score_domain`, claiming `san_overlap` was computed as `min(len(common_sans), 40)` and should be converted to a ratio, referencing a formula `trans 35 + dns 35 + cert 15 + ctx 15 = 100`.
    *   *Reality:* In `llunascope/scoring.py`, scoring is a binary signal model (`WEIGHTS = {san_overlap: 40.0, org_tuple: 35.0, asn_match: 25.0, rdap_org: 15.0}`). No `score_domain` function or raw count computation exists. Frozen weights and scoring semantics were strictly preserved.
*   **Seed Discovery (Prompt 1.5):**
    *   *Premise:* The prompt claimed `score_seed` and `seed_from_domain` hard-code scoring weights that drifted from `scoring.py`.
    *   *Reality:* Neither `score_seed` nor `seed_from_domain` exists in `llunascope/seed.py`. `seed.py` discovers seeds from Wikidata, SEC EDGAR, and PeeringDB using label similarity scores unrelated to `scoring.py`.
*   **Networking (Prompt 1.2):**
    *   *Premise:* The prompt cited `fetch()`, `_safe_request()`, and `fetch_all()`, claiming an unbounded manual redirect loop existed.
    *   *Reality:* The networking API in `llunascope/net.py` is `fetch_json()`, `fetch_text()`, and `check_online()`. There are no manual redirect loops; timeouts were already present.
*   **CLI Flags (Prompt 1.6 & 1.8.1):**
    *   *Premise:* The prompt alleged flag interaction bugs between `--json`, `--html`, and `--quiet`.
    *   *Reality:* Output format is controlled by a single `--format` flag with choices `[tree, mermaid, dot, json, html]`. `--json`, `--html`, and `--quiet` do not exist, while `--verbose` was already present.
*   **Pivot Architecture (Prompt 1.3 & 2.4):**
    *   *Premise:* The prompt cited functions `expand_company`, `expand_all`, `_load_cache`, `fetch_related`, and node pruning with `seen_roots`.
    *   *Reality:* None of these functions or structures exist in `llunascope/pivot.py`. The engine uses `pivot()` with BFS frontier expansion.
*   **Test Suite Dead Code (Prompt 4.7):**
    *   *Premise:* The prompt directed removal of `build_response` and `mock_get` from `tests/doh_shim.py` and duplicate `import requests` in `tests/test_seed.py`.
    *   *Reality:* Neither helper existed in `doh_shim.py`, and `test_seed.py` did not have duplicate imports. (An unused `import urllib.request` in `doh_shim.py` was removed.)

---

## 3. Key Findings & Implemented Fixes

### 3.1 Input & Domain Normalization (`llunascope/normalize.py`)
*   **Domain & Label Length Validation:** Enforced RFC 1035 limits in `root_domain()`: domain length must not exceed 253 characters, each label must be 1–63 characters, empty labels (`..`) are rejected, and labels cannot start or end with a hyphen.
*   **Port Stripping & Range Validation:** Port on scheme-less domain input (`host:port`) is now parsed: valid numeric ports (1–65535) are stripped (`example.com:8080` -> `example.com`), while invalid/out-of-range ports (`:abc`, `:99999`, `:0`) are rejected.
*   **IPv6 & Bracket Protection:** IPv6 literals (`[::1]`, `[2001:db8::1]`) and malformed brackets (`[::1`, `::1]`) are detected and rejected.
*   **IDN / Punycode Decoding:** Validates Unicode domain input through IDNA encoding and decodes `xn--` punycode labels (`xn--mnchen-3ya.de` -> `münchen.de`). Malformed IDN/punycode (`xn--.de`, `xn--$$$.de`) is safely rejected.
*   **Type Safety:** `root_domain()`, `normalize_company()`, `name_similarity()`, and `is_subdomain_of()` now gracefully handle non-string / `None` inputs instead of raising `AttributeError`.
*   **TLD Documentation:** Documented the bare-label rejection rule in `root_domain()` docstring.

### 3.2 Polite & Resilient Networking (`llunascope/net.py`)
*   **HTTP 429 Rate Limiting with Backoff:** Implemented retry logic honoring the `Retry-After` header when received. On 429 responses, `fetch_json()` and `fetch_text()` apply exponential backoff (capped at 5s), log warnings, and retry up to 2 times before returning `None`.
*   **In-Memory LRU Cache:** Added thread-safe in-memory cache `_mem_cache` (max 256 entries, 1-hour TTL) keyed by `(url, params)` to eliminate redundant network calls within a single execution run.
*   **Corrupted Cache Recovery:** Reading cache files now catches `json.JSONDecodeError`, `UnicodeDecodeError`, and `ValueError`, automatically removing the corrupt cache file and treating it as a cache miss.
*   **Bounded Cache File Sizes:** Removed the slicing `[:5_000_000]` that previously produced corrupt, truncated JSON files. Implemented a 1 MB file size cap and per-host directory capacity bounding (100 files max with mtime LRU eviction).
*   **Session Management:** Provided `create_session()` (with bounded redirects `max_redirects = 3`) and `managed_session()` context manager for deterministic socket and connection pool closure.

### 3.3 Horizontal Pivot Engine (`llunascope/pivot.py`)
*   **RIPE IP Cache:** Added `_ip_asn_cache` keyed by IP address (`ip_to_asn()` and `lookup_ripe()`), eliminating redundant RIPE Stat lookups when multiple domains share the same IP address.
*   **Concurrent CT Log Queries:** `fetch_certs()` now queries crt.sh and CertSpotter in parallel using `concurrent.futures.ThreadPoolExecutor(max_workers=2)`.
*   **Uniform Seed Handling:** Normalized and validated seed domain inputs in `pivot()`; empty lists, empty strings, and whitespace strings now consistently return an empty `PivotResult` without exceptions.
*   **Deterministic Output Ordering:** Eliminated non-deterministic iteration across sets (`cert.san_roots`, `result.nodes`, `san_hits`). The final edges are deterministically sorted by `(-e.confidence, e.hop, e.via, e.target)` guaranteeing byte-identical output across repeated runs.
*   **Signal Failure Isolation:** Added exception barriers around each pivot signal evaluation (CT logs, SAN overlap, Org-Tuple, ASN lookup, RDAP handle) so unexpected transient errors preserve already-collected partial graph results.
*   **Cycle & Self-Edge Guard:** Verified graph bounds and added regression test for depth-0 domain self-references.

### 3.4 Confidence Scoring (`llunascope/scoring.py`)
*   **Adversarial Score Clamping:** Clamped `Edge.finalize()` confidence strictly to `float(round(max(0.0, min(MAX_CONFIDENCE, total)), 1))`, ensuring scores remain valid floats within `[0.0, 99.0]` under adversarial or negative signal weights.

### 3.5 Seed Resolver (`llunascope/seed.py`)
*   **Case Normalization & Deduplication:** `_domain_from_url()` and `resolve()` normalize case and collapse duplicate seed domains.

### 3.6 Output Rendering (`llunascope/render.py`)
*   **HTML Fallback:** When company name is empty or blank, `to_html()` / `render_html()` falls back to `seed_label` in `<title>` and `<h1>`.
*   **Mermaid Escaping:** Escaped angle brackets and double quotes in `_mermaid_label()` to prevent tag injection in Mermaid blocks.
*   **ASCII Tree Trailing Newline & Depth Cap:** `print_tree()` and `render_tree()` now accept `max_depth` to prune branches deeper than the cap, and `render_tree()` guarantees exactly one trailing newline.
*   **JSON Resilience:** `render_json()` handles raw dictionary inputs and safely provides well-formed defaults when domain or seed keys are missing.

### 3.7 CLI Polish & UX (`llunascope/cli.py`)
*   **Actionable Error Messages:** Replaced vague errors with clear next steps for network failures, empty `--seed` flags, and ambiguous disambiguation selections.
*   **Exit Codes:** Documented and verified standard CLI exit codes: `0` (success), `1` (general error), `2` (usage/arg error), `3` (offline), `4` (unresolvable company), `130` (interrupted via SIGINT).
*   **CLI UX Polish:** Added `-v` alias for `--verbose`. Added `--no-truncate` flag and displayed truncation notices for registry leads (`... (truncated, N more leads; use --no-truncate to show all)`). Wrapped format output generation to report rendering failures cleanly.

---

## 4. Files Changed

*   `llunascope/normalize.py`: Added RFC length checks, port stripping/validation, IPv6/bracket rejection, IDN/punycode decoding, non-string safety, and docstring documentation.
*   `llunascope/net.py`: Added 429 exponential backoff, in-memory LRU cache, corrupt cache handling, 1MB cache file bounding, per-host directory bounding, and session context manager.
*   `llunascope/pivot.py`: Added RIPE IP cache, parallel CT queries, deterministic iteration and edge sorting, seed input sanitization, and per-signal exception isolation.
*   `llunascope/scoring.py`: Clamped finalized confidence scores to `[0.0, 99.0]` as float.
*   `llunascope/seed.py`: Added non-string safety, case normalization, and duplicate seed collapsing.
*   `llunascope/render.py`: Added empty company fallback in HTML, depth-cap propagation, `render_tree()` trailing newline, `render_json()` missing key resilience, and angle bracket escaping.
*   `llunascope/cli.py`: Added `-v` shorthand, `--no-truncate` option, truncation notification, actionable error guidance, SIGINT exit 130 handler, and rendering error trap.
*   `README.md`: Documented exit codes table and updated offline test count to 114.
*   `tests/doh_shim.py`: Removed unused `import urllib.request`.
*   `tests/test_normalize.py`: Added regression tests for non-string input, port validation, IPv6 brackets, DNS length limits, and IDN/punycode.
*   `tests/test_net.py`: Added regression tests for 429 Retry-After success and exhaustion, corrupt cache recovery, memory cache deduplication, and session closure.
*   `tests/test_pivot.py`: Added regression tests for depth-0 self-cycle termination, empty/whitespace seed consistency, deterministic ordering, CT query error resilience, and RIPE IP caching.
*   `tests/test_scoring.py`: Added regression tests for adversarial evidence clamping, float typing, and bounds checking.
*   `tests/test_seed.py`: Added regression tests for uppercase URLs and duplicate seed flag collapsing.
*   `tests/test_render.py`: Added regression tests for trailing newlines in tree rendering, depth-cap propagation, HTML script tag escaping, empty name fallback, and `render_json` missing key fallback.
*   `tests/test_cli.py`: Added regression tests for `--help` exit code 0, SIGINT exit code 130, render error exit code 1, and hints truncation / `--no-truncate`.

---

## 5. Test Suite Results

*   **Before changes:** 86 tests passed (`python3 -m unittest discover -s tests -v` in 0.030s)
*   **After changes:** 114 tests passed (`python3 -m unittest discover -s tests -v` in 0.033s)
*   **Pytest verification:** 114 passed (`python3 -m pytest tests/ -q` in 0.19s)
*   **Existing tests deleted:** 0 (all 86 legacy tests preserved and passing)
*   **New regression tests added:** 28 tests

---

## 6. Git & Push Status

*   **Working branch:** `overhaul/marathon-2026-11-09`
*   **Commit Hash:** `48ecb26`
*   **Commit Message:** `Overhaul llunascope: fix parsing, scoring bounds, caching, and CLI polish`
*   **Push Status:** As anticipated in Operator Note 6, pushing from this machine failed with credentials error (`fatal: could not read Username for 'https://github.com': No such device or address`). The commit is cleanly preserved on the `overhaul/marathon-2026-11-09` branch for the operator to transfer and push.
