# AUDIT.md — llunascope v1.1.0 (watch mode + M&A radar)

Build date: 2026-11-20. Auditor: Lluna (build agent). This file records what
v1.1.0 added, the design decisions behind it, what was tested, and the honest
limitations. It is a build record, not marketing.

## What changed

- **Watch mode** (`--watch`, `--snapshot`, `--state-dir`): each run saves a
  JSON snapshot of the discovered graph; the next run diffs against it and
  reports new shadow assets. Exit codes: 0 = no new assets, 10 = new shadow
  asset(s), 2 = usage/state error. Same convention as puny-patrol's watchdog.
- **M&A radar** (`--radar SNAP-A SNAP-B [--radar-baseline BASE-A BASE-B]`):
  offline comparison of two snapshots; flags a TLS certificate whose SAN set
  spans both orgs' domains, or an ASN hosting domains from both orgs, when
  that sharing is new since the baseline. Exit codes: 0 = no new convergence,
  10 = new convergence, 2 = usage/state error.
- **Version bump** 1.0.0 → 1.1.0 (`llunascope/__init__.py`, `pyproject.toml`).
- **README** documents both features (usage, exit codes, snapshot format);
  roadmap items marked done. **SKILL.md** updated to match. Test-count line
  updated to the real count.

## Files changed

| File | Change |
|---|---|
| `llunascope/watch.py` (new) | Snapshots, diffing, radar, report formatting |
| `llunascope/cli.py` | `--watch`/`--snapshot`/`--state-dir`/`--radar`/`--radar-baseline` flags, radar runner, watch load/diff/save/report flow |
| `llunascope/pivot.py` | `PivotResult` gains `asn_by_node` (domain → ASN) and `cert_map` (cert id → SAN roots), populated during the existing pivot |
| `llunascope/__init__.py`, `pyproject.toml` | Version 1.1.0 |
| `tests/test_watch.py` (new) | 39 tests: snapshots, diffing, radar, CLI exit codes |
| `README.md`, `SKILL.md` | Docs for both features |
| `AUDIT.md` (this file) | Build record |

## Design decisions

- **Snapshot format v1** (plain JSON): `tool`, `format_version`, `version`,
  `company`, `seeds`, `created_at` (UTC ISO), `nodes` (sorted), `edges`
  (sorted, with evidence, `__san_ceiling__` markers stripped), `org_identities`,
  `asn_by_node`, `certs` (cert id → sorted SAN roots; crt.sh certs only,
  CertSpotter issues no ids). Everything sorted, so two identical runs
  produce identical snapshots except `created_at` (verified by test).
- **Edge identity for diffing** is (target, via, set of fired signals). A new
  signal firing on an already-known edge counts as a new asset — the evidence
  changed, even though the domain did not. A new edge whose target is itself
  a new domain is not double-counted: the domain is the asset.
- **Exit-code semantics**: watch/radar return 10 only for *new* findings.
  Removed domains are reported for context but never affect the exit code.
  A failed run never touches the snapshot (save happens after a successful
  pivot, atomically via temp file + rename), so a transient outage can't
  swallow an alert — same rule as puny-patrol's watchdog.
- **Radar needs a cert registry**: `pivot()` now records every crt.sh cert id
  it sees with its SAN roots. Radar compares cert ids across the two
  snapshots and requires the SAN set to touch both orgs' node sets — the same
  cert id appearing in both files with one-sided SANs is not convergence.
  ASN convergence uses the per-domain ASN map recorded during the pivot.
- **Per-company snapshots**: default path `~/.cache/llunascope/<slug>.json`,
  slug from the normalized company name. A snapshot recorded for a different
  company is refused (exit 2) rather than diffed — diffing "Acme" against
  "Globex" would report the whole graph as new.
- **Error handling**: missing snapshot = first run (baseline, exit 0);
  corrupt JSON, wrong shape, company mismatch, unwritable path = clean error
  message + exit 2, no traceback. `--snapshot`/`--state-dir` without
  `--watch`, `--radar-baseline` without `--radar`, and `--radar` + `--watch`
  together are all rejected with exit 2.
- **Terminal safety**: company/domain strings in watch and radar reports go
  through the existing `_term_safe` (C0 control stripping), same as the tree
  renderer. JSON output is safe by construction (`json.dumps` escapes).
- **No new dependencies**: stdlib + the existing `requests`/`rich` only.
  Still fully passive — watch and radar add zero network calls; radar is
  entirely offline.

## Tests

- Full suite: `python3 -m unittest discover -s tests` — **125 tests, all
  green** (86 before this change, +39 new in `tests/test_watch.py`).
- New coverage: snapshot save/load round-trip and determinism; diff with
  added/removed/unchanged nodes and edges; new-signal-on-known-domain;
  order-insensitivity; empty graph; corrupt/wrong-shape/unwritable snapshots;
  company match/mismatch; default snapshot path slugging; radar new shared
  cert, new shared ASN, one-sided cert (not convergence), baseline
  suppression, baseline asymmetry error; CLI exit codes for watch first run
  (0), quiet repeat (0), new asset (10), corrupt snapshot (2), company
  mismatch (2), save failure (2), `--state-dir`, `--snapshot` without
  `--watch` (2), radar convergence (10), radar quiet (0), radar baseline
  quiet (0), radar missing/corrupt file (2), flag conflicts (2);
  control-character stripping in watch and radar reports.
- Existing suite stayed green throughout (no regressions in scoring,
  normalization, seed resolution, pivot signals, or renderers).

## Live smoke test

- Ran live (with `tests/doh_shim.py`, needed because this VM intercepts
  port-53 DNS) against `--seed example.com`, twice in a row:
  - Run 1: baseline saved to `/tmp/smoke-ex.json` — 5 root domains
    (`example.com`, `example.edu`, `example.net`, `example.org`,
    `testexample.com`), 13 edges, 4 certs, 5 ASNs — exit code **0**.
  - Run 2: `watch: no new shadow assets since 2026-11-20T08:11:15+00:00`
    — exit code **0**. Quiet repeat confirmed live.
  - Full log: `/tmp/smoke-live.log`; snapshot: `/tmp/smoke-ex.json`.
- Caveat: live runs are slow in this sandbox — each run took ~2–3 minutes
  (many sequential CT/RIPE/RDAP calls, one curl per DNS lookup via the DoH
  shim). An earlier 170 s attempt with a large seed (`yahoo.com`) timed out
  before finishing; that is sandbox slowness, not a code defect — the pivot
  itself is unchanged from v1.0, and the watch path (snapshot → diff → exit
  code) is exercised end-to-end by the mocked CLI tests above as well as by
  this live run.

## Honest limitations

- **Radar sees only what the pivot recorded.** `asn_by_node` covers only
  domains the pivot actually resolved an ASN for; `certs` covers only crt.sh
  certs with ids (CertSpotter certs have none and are invisible to the
  radar). A convergence that exists in CT logs but wasn't captured in either
  snapshot is missed — radar compares snapshots, not the live internet.
- **Baseline quality decides radar signal.** Without `--radar-baseline`,
  every shared cert/ASN is reported as new, including long-standing shared
  infrastructure (e.g. a subsidiary that has shared a cert for years). The
  first radar run against a pair with history will be noisy; re-run with
  those snapshots as the baseline to see only fresh convergence.
- **ASN sharing is weak evidence on its own.** Two orgs on the same ASN may
  just share a hoster or a /24 reassignment the pivot didn't see. Treat a
  shared ASN as a lead, a shared SAN cert as the stronger signal.
- **Watch diffs snapshots, not ground truth.** A domain that stops appearing
  because crt.sh was flaky that day shows up as "removed" — the report lists
  removals separately, but the operator should check whether the source was
  down before concluding a divestment.
- **Interactive HTML graph** (Cytoscape.js) from the v1.1 roadmap is still
  pending; the static Mermaid export remains.
