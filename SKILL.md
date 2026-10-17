# llunascope — agent skill

Passive EASM: from a single **company name** to every forgotten subsidiary and
shadow root domain. Zero packets sent. Zero API keys. Every finding carries a
visible evidence chain plus a confidence score.

## When to use

- Mapping an organization's attack surface starting from just its name
- Finding subsidiaries, acquired brands, and shadow IT during recon
- Answering "what does this company own that it doesn't know about?"

## Installation

```bash
pip install llunascope
# or from source:
git clone https://github.com/Llunatics/llunascope && cd llunascope && pip install -e .
```

## Usage

```bash
# Basic: company name in, lineage tree out
llunascope "Bank Mandiri"

# Known domains? Skip name resolution entirely
llunascope --seed bankmandiri.co.id,mandiri-sekuritas.co.id "Bank Mandiri"

# Machine-readable: root domains only, pipe straight into subfinder
llunascope "Acme Corp" --roots-only --non-interactive | subfinder -silent

# Other formats: mermaid (renders in GitHub), dot (graphviz), json, html
llunascope "Acme Corp" --format mermaid
llunascope "Acme Corp" --format html > report.html
llunascope "Acme Corp" --format json > graph.json

# Automation: never prompt
llunascope "Apple" --non-interactive --format json
```

## How it works (so you can interpret results)

1. **Hop-0 Seed Resolver** — resolves the company name to root domains + ASNs
   via Wikidata (official website P856), SEC EDGAR (US public companies),
   RIPE Stat, and PeeringDB. `--seed` bypasses this entirely.
2. **Horizontal Pivot Engine** (max 3 hops) — expands via:
   - `san_overlap` (40): two root domains share a TLS cert in CT logs
   - `org_tuple` (35): identical Subject.O on OV/EV certs (very strong)
   - `asn_match` (25): same non-cloud ASN (cloud/CDN ASNs never pivoted on)
   - `registry_hint` (30): Wikidata subsidiary/parent, EDGAR listing
   - `rdap_org` (15): same RDAP handle (weak post-GDPR)
   - Shared CDN/WAF certs (>30 roots in one SAN) are penalized ×0.2.
3. Every edge shows **why** it exists — check the evidence chain before
   trusting a link. Confidence is heuristic, not proof.

## Limits (be honest with the user)

- Registry coverage is Western-skewed; Indonesian/private companies may
  resolve poorly — use `--seed` with known domains in that case.
- It stops at **root domains** by design; run subfinder/amass on the
  `--roots-only` output for subdomain enumeration.
- Fully passive = no live host verification; a discovered domain may be
  parked, dead, or divested. The evidence chain tells you *why* it's linked.
- v1.1 roadmap: M&A radar, temporal watch mode, interactive HTML graph.

## Safety

Read-only against public APIs (crt.sh, RIPE Stat, Wikidata, SEC EDGAR,
RDAP). It never sends packets to target infrastructure. Do not use it to
guide unauthorized testing — map first, get authorization before touching.
