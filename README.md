<div align="center">

# 🌙 llunascope

**The moon never knocks. It just watches.**

*From a single company name to every forgotten subsidiary and shadow root domain.
Zero packets. Zero API keys. Full evidence chains.*

</div>

```bash
$ pip install llunascope
$ llunascope "Yahoo"
🌙 yahoo.com
├── aol.com  [75%]
│   · san_overlap: 12 shared cert(s) also cover aol.com
│   · asn_match: both resolve into AS36646
├── netscape.com  [65%]
│   · san_overlap: 8 shared cert(s) also cover netscape.com
├── oath.cloud  [40%]
│   · san_overlap: 3 shared cert(s) also cover oath.cloud
...
```

## What it does

Every EASM tool asks *"what subdomains does this domain have?"* — a small question.
llunascope asks the bigger one:

> **What does this company own that it doesn't know about?**

Subsidiaries. Acquired brands. Shadow IT. Forgotten dev environments.
You type a **company name** — not a domain — and get back a **lineage tree** of
root domains, where **every edge carries its evidence chain and a confidence
score**. No black-box claims: you can see exactly *why* each domain is linked.

## How it works

**Hop 0 — Seed Resolver.** The company name is resolved to root domains and
ASNs via public registries only: Wikidata (official website), SEC EDGAR (US
public companies), RIPE Stat, PeeringDB. Wikidata subsidiary names, EDGAR
matches, and PeeringDB orgs that don't yield domains are printed as follow-up
leads. Don't trust name resolution? Skip it: `--seed` lets you supply known
domains directly (normalized automatically — case, `www.`, trailing dots,
and URLs are cleaned).

**Hops 1–3 — Horizontal Pivot Engine.** Expands via certificate-transparency
SAN overlaps, ASN co-location, RDAP org handles, and Subject.O tuple matching
on OV/EV certificates. Bounded (3 hops, 500 nodes max) — it terminates.

**Evidence court.** Every edge shows which signals fired and a 0–100
confidence. The weights are published below — audit them, challenge them.

### Signal weights (v1, conservative)

| Signal | Weight | What it means |
|---|---|---|
| `san_overlap` | 40 | Two root domains share a TLS certificate in CT logs |
| `org_tuple` | 35 | Identical Subject.O on OV/EV certificates (very strong) |
| `asn_match` | 25 | Both resolve into the same non-cloud ASN |
| `rdap_org` | 15 | Same RDAP org handle (weak — post-GDPR redaction) |

Seed domains (Hop-0 resolved or `--seed`) are trusted roots, not graded edges —
they carry no signal weight. Registry leads (Wikidata subsidiary names, SEC
EDGAR listings, PeeringDB orgs) are printed as follow-up hints, not edges.

Anti–false-positive rules: cloud/CDN/large-ISP ASNs (AWS, Cloudflare, Google,
Telkom Indonesia, …) are **never** pivoted on; certificates covering >30
distinct root domains (shared CDN/WAF certs) get confidence **×0.2**; ASNs
announcing more than a /16 are skipped unless RDAP shows a customer
reassignment.

## Usage

```bash
llunascope "Bank Mandiri"                          # name in, tree out
llunascope --seed acme.com,acme.co.id "Acme"        # skip name resolution
llunascope "Acme" --roots-only --non-interactive | subfinder -silent
llunascope "Acme" --format mermaid                  # renders natively on GitHub
llunascope "Acme" --format html > report.html       # interactive graph (drag, zoom, click for evidence)
llunascope "Acme" --format json > graph.json        # machine-readable
llunascope --weights                                # show scoring weights
```

### Interactive graph — `--format html`

The HTML report is an interactive Cytoscape.js graph, not a static picture:
drag nodes, scroll to zoom, and click any node or edge to inspect its
evidence chain and confidence in the details panel. Toolbar: layout switcher
(force-directed / tree / concentric / grid), a minimum-confidence slider that
hides weak edges (and the nodes they orphan), a domain search box, and
Fit/Reset view. Edge colour tracks confidence (green ≥70%, amber 40–69%,
red <40%); seed roots get a purple star node. The evidence table from the
old report is still below the graph, so the file stays useful when the
Cytoscape.js CDN is unreachable. `--format mermaid` is unchanged for
GitHub-native rendering.

### Watch mode — temporal diffing

Run the same company on a schedule and get alerted only when something new
appears. Each run saves a snapshot (`~/.cache/llunascope/<company>.json` by
default); the next run diffs against it.

```bash
llunascope "Acme" --watch                  # first run: saves a baseline
llunascope "Acme" --watch                  # later run: reports new assets
llunascope "Acme" --watch --snapshot ~/acme-watch.json   # custom snapshot file
llunascope "Acme" --watch --state-dir ~/llunascope-state # per-company files
```

Exit codes: **0** = no new shadow assets, **10** = new shadow asset(s) found,
**2** = usage/state error (corrupt snapshot, wrong company, unwritable path).
A failed run never touches the snapshot, so a transient outage can't swallow
an alert. New root domains and new evidence on known domains are both
reported; removed domains are listed for context but don't affect the exit
code. Cron-friendly: `0 * * * * llunascope "Acme" --watch --non-interactive`.

Snapshot format (JSON, `format_version` 1): company, seed domains, UTC
timestamp, discovered root domains, evidence edges with confidence, Subject.O
identities, per-domain ASNs, and a cert registry (cert id → SAN root domains,
crt.sh certs only). Snapshots are per-company — a snapshot recorded for
another company is refused rather than diffed.

### M&A radar

Compare two snapshots and flag infrastructure convergence — an acquisition
signal. A TLS certificate whose SAN set spans both orgs' domains, or an ASN
hosting domains from both orgs, counts as convergence. Only convergence that
is new since the baseline is reported; without a baseline, everything shared
is reported.

```bash
llunascope --radar acme.json globex.json
llunascope --radar acme.json globex.json \
    --radar-baseline acme-old.json globex-old.json
```

Exit codes: **0** = no new convergence, **10** = new convergence,
**2** = usage/state error (missing or corrupt snapshot). Radar is fully
offline — it only reads snapshot files.

It **stops at root domains on purpose** — pipe `--roots-only` into
`subfinder`/`amass` for subdomain enumeration. Unix philosophy.

## Demo

Real run against **Yahoo** (the M&A-heavy classic) — from just the name
`"Yahoo"`, llunascope's Hop-0 resolver found `yahoo.com` via Wikidata, then
the pivot engine surfaced `aol.com`, `oath.cloud`, `yahooinc.com`, `cs.com`
(Compuserve), and `wmconnect.com` from shared certificates — each with its
evidence chain:

```
🌙 yahoo.com
├── aol.com  [40%]
│   · san_overlap: 3 shared cert(s) also cover aol.com
├── oath.cloud  [40%]
│   · san_overlap: 1 shared cert(s) also cover oath.cloud
├── cs.com  [40%]
│   · san_overlap: 2 shared cert(s) also cover cs.com
├── yahooinc.com  [40%]
│   · san_overlap: 1 shared cert(s) also cover yahooinc.com
...
```

140 root domains, 993 evidence-graded edges — from a single company name,
zero packets sent.

## Coverage honesty

- **Western / US public companies**: best coverage (SEC EDGAR, Wikidata).
- **Indonesian / private companies**: registry data is thin — use `--seed`
  with domains you already know and let the pivot engine expand from there.
- Fully passive = no live host verification. A linked domain may be parked,
  dead, or divested — the evidence chain tells you *why* it's linked, so you
  can judge.

## Install

```bash
pip install llunascope        # requires Python >= 3.10
```

Dependencies: `requests`, `rich` (graceful plain-text fallback if missing).
No API keys. No configuration. No packets sent to target infrastructure —
only public APIs (crt.sh, RIPE Stat, Wikidata, SEC EDGAR, RDAP) are queried.

## Development

| Command | What it does |
|---|---|
| `python3 -m unittest discover -s tests` | 140 offline unit tests: scoring math, name/domain normalization, seed resolution, pivot-engine signal logic (network mocked), CLI plumbing, all output formats incl. injection-escaping and the interactive HTML graph payload, watch-mode snapshots/diffing, M&A radar |
| `python3 -c "import sys; sys.path.insert(0,'tests'); import doh_shim; ..."` | run live tests on networks where port-53 DNS is intercepted |

`tests/doh_shim.py` is a test-only DNS-over-HTTPS shim for such networks —
not packaged, not needed on normal machines.

## Roadmap (v1.1 — shipped)

- **M&A radar** — flag when two orgs' certs/ASN suddenly converge (acquisition signal) ✅
- **Watch mode** — temporal diffing: alert on new shadow assets ✅
- **Interactive HTML graph** (Cytoscape.js) replacing the static Mermaid export ✅ (v1.2.0)

## License

MIT — built by [Llunatics](https://github.com/Llunatics). PRs welcome.
