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
public companies), RIPE Stat, PeeringDB. Don't trust name resolution? Skip it:
`--seed` lets you supply known domains directly.

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
| `registry_hint` | 30 | Wikidata subsidiary/parent link, or SEC EDGAR listing |
| `rdap_org` | 15 | Same RDAP org handle (weak — post-GDPR redaction) |
| `seed_direct` | 95 | You supplied it via `--seed` (trusted input) |

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
llunascope "Acme" --format html > report.html       # standalone report
llunascope "Acme" --format json > graph.json        # machine-readable
llunascope --weights                                # show scoring weights
```

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

## Roadmap (v1.1)

- **M&A radar** — flag when two orgs' certs/ASN suddenly converge (acquisition signal)
- **Watch mode** — temporal diffing: alert on new shadow assets
- **Interactive HTML graph** (Cytoscape.js) replacing the static Mermaid export

## License

MIT — built by [Llunatics](https://github.com/Llunatics). PRs welcome.
