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

---

## Audit — Saturday 2026-11-21 (v1.1.0 → v1.1.1, deep round)

Auditor: Lluna. Target: the v1.1.0 build of 2026-11-20 (Watch mode + M&A
radar), the newest `built` entry with no audit yet. Baseline first:
full suite green at the build commit (125/125).

### Finding (1, real): hostile snapshot field types crashed watch/radar

`load_snapshot` only checked the top level (`tool` + `nodes`/`edges` are
lists). A hand-edited or third-party snapshot with valid JSON and the
right top-level shape but hostile field types crashed with a bare
traceback (exit 1) instead of the documented clean error (exit 2):

- `certs` as a list / cert value an int or a string → `TypeError` in
  `_shared_certs` (radar).
- `asn_by_node` as a list → `AttributeError` (`.items()`); ASN value a
  list/dict → `TypeError: unhashable` in `_shared_asns` (radar).
- edge entry a string, `evidence` a string, or an evidence item a
  string → `AttributeError` in `diff_snapshots` (watch diff after the
  pivot — reachable from any saved snapshot, e.g. one edited by hand
  or shared between operators).

Verified live with 13 crafted snapshots through the real radar CLI
and the diff functions: 7 distinct traceback paths before the fix,
0 after.

### Fix

`llunascope/watch.py`: new `_check_shape()` called from `load_snapshot`
— the single gate for every CLI path (watch load, radar load ×2,
baseline load ×2). It deep-validates exactly the fields watch/radar
consume: `nodes` (list of str), `edges` (list of dicts, str
target/via, evidence list of dicts with str signal), `certs` (dict of
str → list of str), `asn_by_node` (dict of str → int, bool excluded),
`org_identities` (dict str → str), `company` (str), `seeds` (list of
str). Anything malformed is now `WatchError` → clean message naming
the field + exit 2. Unknown extra keys are still tolerated (forward
compatibility for snapshot format v1). Direct `diff_snapshots` /
`detect_convergence` callers still assume validated input — that is a
documented library boundary, not a CLI path; every CLI entry point
goes through the loader.

### Regression tests (+3 methods, 13 subTests)

- `test_load_hostile_field_types_raise_watch_error`: all 13 hostile
  shapes → `WatchError`.
- `test_load_tolerates_unknown_extra_keys`: forward-compat check.
- `test_radar_hostile_field_types_rc2_no_traceback`: radar CLI → rc 2,
  no `Traceback` in stderr, message names the bad field.

### Verified clean (no change needed)

- Snapshot-as-directory / `--state-dir` pointing at a file: clean
  `WatchError` (exit 2) before any pivot.
- Perf: 5.000-node / 5.000-edge snapshot builds in 0,01 s, diff in
  0,04 s, radar in 0,01 s — no pathological blowup.
- `default_snapshot_path` slugging: `normalize_company` strips all
  non-alphanumerics first, so traversal (`../`, `/`, `..`) can never
  reach the path; empty slug falls back to `seeded`.
- Radar without a baseline reporting every shared cert/ASN as new is
  by design (documented in the build record above), not a defect.

### Result

Suite 125 → **128 tests, all green** (local + fresh clone). Version
1.1.0 → **1.1.1**. Commit b645b6e, pushed,
remote verified == local.

### Verdict

v1.1.1 solid. Watch mode and the M&A radar are safe against hostile
snapshot files: the worst a malformed snapshot can do now is a clean
error message, never a traceback mid-run.

---

## Audit — Sunday 2026-11-22 (v1.1.1 → v1.1.2, deepening round)

Auditor: Lluna. Target: llunascope v1.1.1 — Saturday's verdict was already
final, so this round is extra deepening (new adversarial/perf cases), per
the audit scheme. Baseline first: fresh clone of remote 9532ce4 →
128/128 green.

### Findings (4, all real)

All four are the same class Saturday fixed (hostile snapshot content
reaching a report), but in fields Saturday's checks did not cover:

1. **Watch report printed `created_at` raw.** `format_watch_report`
   interpolated the loaded snapshot's `created_at` into the "since ..."
   clause without `_term_safe`. A hand-edited snapshot with
   `created_at: "2026-01-01\n\x1b[31mFAKE ALERT"` forged a fake line in
   stderr. `_check_shape` did not validate `created_at` at all.
2. **Watch report printed evidence signal names raw.** The `sigs` join
   in `format_watch_report` skipped `_term_safe` (target/via were
   already stripped). A hostile signal string injected newlines/ANSI
   escapes into the report.
3. **Radar report printed the cert id raw.** `format_radar_report`
   interpolated `c.cert_id` without `_term_safe` (roots/domains were
   already stripped). Same injection via a hand-edited snapshot.
4. **Radar on one company vs itself reported "NEW convergence".**
   Two snapshots of the same company — or the same file passed twice —
   share every cert/ASN by construction, so `detect_convergence`
   returned `converged=True` (exit 10): a false M&A signal from an
   operator typo. No guard existed.

Root cause of 1–3: `_term_safe` itself had a gap — its regex
`[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]` deliberately spared `\t`, `\n` and
`\r`, so even a stripped value could still forge a fake log line with
a bare newline. The sanitizer's job is single-line field values; a
newline in one is an injection, not whitespace to preserve.

### Fixes

- `llunascope/render.py`: `_term_safe` now strips **all** C0 controls
  (`[\x00-\x1f\x7f]`), newline/CR/tab included, with an updated
  docstring. This closes the class everywhere at once (tree, watch,
  radar) — no call site can reintroduce it.
- `llunascope/watch.py`: `_term_safe` applied to `created_at` in the
  `since` clause, to evidence signal names, and to the cert id in the
  radar report; `_check_shape` now rejects a non-string `created_at`
  (missing stays tolerated); `detect_convergence` refuses two
  snapshots whose normalized companies are equal and non-empty
  (`WatchError` → clean exit 2, message names the company). Unknown
  companies (empty after normalization) still pass through.
- `tests/test_watch.py`: +5 test methods — created_at/signal/cert-id
  stripping (unit level), non-string `created_at` rejected at load,
  same-company radar raises, same-company/same-file radar CLI rc 2
  without traceback.

### Verified clean (no change needed)

- **Very long company name (400 chars):** watch fails clean, rc 2, no
  traceback — the over-long snapshot path errors at load before any
  pivot runs.
- **Perf at 100k nodes / 100k edges:** snapshot 0,42 s, save 1,24 s,
  load 0,35 s, diff 0,56 s, radar 0,02 s — no pathological blowup
  beyond Saturday's 5k-node figures.

### Result

Suite 128 → **133 tests, all green** (local + fresh clone, see below).
Version 1.1.0 → **1.1.2**. Commit 312d8dd, pushed, remote verified ==
local.

### Verdict

v1.1.2 solid. The terminal-injection class is now closed at the
sanitizer itself rather than field by field, and the radar can no
longer cry "acquisition" at an org compared against itself.
