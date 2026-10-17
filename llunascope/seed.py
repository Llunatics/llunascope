"""Hop-0 Seed Resolver: company name -> initial root domains + ASNs.

JSON APIs only. Every source is optional and failure-isolated: a dead source
yields zero candidates, never an exception. Never touches bgp.he.net or
OpenCorporates (bot-blocked).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import net
from .normalize import normalize_company, name_similarity, root_domain

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
EDGAR_TICKERS = "https://www.sec.gov/files/company_tickers_exchange.json"
PEERINGDB_ORG = "https://www.peeringdb.com/api/org/"

EDGAR_UA = "Llunascope admin@llunaticsys.web.id"


@dataclass
class SeedCandidate:
    kind: str            # 'domain' | 'asn'
    value: str           # root domain or 'AS1234'
    source: str          # 'wikidata' | 'edgar' | 'peeringdb' | 'seed-flag'
    confidence: float    # 0..100
    evidence: str        # human-readable provenance
    extra: dict = field(default_factory=dict)


def _wikidata_label_search(name: str) -> list[dict]:
    """Find Wikidata entities whose English label matches the company name."""
    norm = normalize_company(name)
    # wbsearchentities is fast and purpose-built for label lookup.
    data = net.fetch_json(
        "https://www.wikidata.org/w/api.php",
        params={"action": "wbsearchentities", "search": name,
                "language": "en", "format": "json", "limit": "10"},
        timeout=20,
    )
    if not data:
        return []
    out = []
    for item in data.get("search", []):
        label = item.get("label", "")
        sim = name_similarity(label, name)
        # Also compare against normalized form for robustness.
        sim = max(sim, name_similarity(label, norm))
        if sim >= 0.4 or norm in label.lower() or label.lower() in norm:
            out.append({"id": item.get("id"), "label": label, "sim": sim})
    return sorted(out, key=lambda x: -x["sim"])[:5]


def _wikidata_claims(qid: str) -> dict:
    """Pull P856 (website), P355 (subsidiary), P749 (parent) for an entity."""
    query = f"""SELECT ?website ?sub ?subLabel ?parent ?parentLabel WHERE {{
      OPTIONAL {{ wd:{qid} wdt:P856 ?website. }}
      OPTIONAL {{ wd:{qid} wdt:P355 ?sub. }}
      OPTIONAL {{ wd:{qid} wdt:P749 ?parent. }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }} LIMIT 50"""
    data = net.fetch_json(
        WIKIDATA_SPARQL,
        params={"format": "json", "query": query},
        headers={"Accept": "application/sparql-results+json"},
        timeout=25,
    )
    if not data:
        return {}
    rows = data.get("results", {}).get("bindings", [])
    websites, subs, parents = [], [], []
    for r in rows:
        if "website" in r and r["website"]["value"] not in websites:
            websites.append(r["website"]["value"])
        if "sub" in r:
            key = r["sub"]["value"]
            if key not in [s[0] for s in subs]:
                subs.append((key, r.get("subLabel", {}).get("value", "")))
        if "parent" in r:
            key = r["parent"]["value"]
            if key not in [p[0] for p in parents]:
                parents.append((key, r.get("parentLabel", {}).get("value", "")))
    return {"websites": websites, "subsidiaries": subs, "parents": parents}


def _domain_from_url(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return root_domain(urlparse(url).netloc or url)
    except Exception:
        return ""


def wikidata_seeds(name: str) -> tuple[list[SeedCandidate], list[dict]]:
    """Returns (candidates, matched_entities_for_disambiguation)."""
    entities = _wikidata_label_search(name)
    candidates: list[SeedCandidate] = []
    for ent in entities:
        claims = _wikidata_claims(ent["id"])
        if not claims:
            continue
        conf = 55 + ent["sim"] * 40  # 55..95 scaled by label similarity
        for site in claims.get("websites", []):
            dom = _domain_from_url(site)
            if dom:
                candidates.append(SeedCandidate(
                    "domain", dom, "wikidata", round(min(conf, 95), 1),
                    f"Wikidata {ent['id']} ({ent['label']}) official website (P856)",
                    {"qid": ent["id"]}))
        for _qid, sublabel in claims.get("subsidiaries", []):
            # Subsidiary names become secondary search hints, not domains yet.
            if sublabel and name_similarity(sublabel, name) < 0.9:
                candidates.append(SeedCandidate(
                    "hint", sublabel, "wikidata", round(conf * 0.6, 1),
                    f"Wikidata {ent['id']} subsidiary (P355): {sublabel}",
                    {"qid": ent["id"]}))
    # Deduplicate domains, keep highest confidence.
    seen: dict[str, SeedCandidate] = {}
    for c in candidates:
        if c.kind == "domain" and (c.value not in seen or c.confidence > seen[c.value].confidence):
            seen[c.value] = c
    domains = list(seen.values())
    hints = [c for c in candidates if c.kind == "hint"]
    return domains, [{"label": e["label"], "qid": e["id"], "sim": round(e["sim"], 2)} for e in entities]


def edgar_seeds(name: str) -> list[SeedCandidate]:
    """Match against SEC EDGAR company tickers (US public companies)."""
    data = net.fetch_json(EDGAR_TICKERS, headers={"User-Agent": EDGAR_UA}, timeout=25)
    if not isinstance(data, dict):
        return []
    norm = normalize_company(name)
    out: list[SeedCandidate] = []
    for entry in data.values():
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        cname = str(entry[1])
        sim = name_similarity(cname, name)
        if sim >= 0.6 or (norm and norm in cname.lower()):
            out.append(SeedCandidate(
                "hint", cname, "edgar", round(50 + sim * 40, 1),
                f"SEC EDGAR listed company (CIK {entry[0]}, ticker {entry[2] if len(entry) > 2 else '?'})",
                {"cik": str(entry[0])}))
            if len(out) >= 5:
                break
    return out


def peeringdb_seeds(name: str) -> list[SeedCandidate]:
    """PeeringDB org search -> ASNs. Supplement only; often empty for enterprises."""
    data = net.fetch_json(PEERINGDB_ORG, params={"name__icontains": name[:40]}, timeout=20)
    if not isinstance(data, dict):
        return []
    out: list[SeedCandidate] = []
    for org in (data.get("data") or [])[:5]:
        oname = org.get("name", "")
        if name_similarity(oname, name) < 0.4:
            continue
        for asn in (org.get("asn") or []) if isinstance(org.get("asn"), list) else []:
            out.append(SeedCandidate(
                "asn", f"AS{asn}", "peeringdb", 60.0,
                f"PeeringDB org '{oname}' announces AS{asn}"))
    return out


def resolve(name: str, seed_domains: list[str] | None = None
            ) -> tuple[list[SeedCandidate], list[SeedCandidate], list[dict]]:
    """Run all Hop-0 sources.

    Returns (domain_candidates, other_candidates, disambiguation_entities).
    seed_domains bypasses resolution entirely (escape hatch).
    """
    domains: list[SeedCandidate] = []
    others: list[SeedCandidate] = []
    entities: list[dict] = []

    if seed_domains:
        for d in seed_domains:
            rd = root_domain(d.strip())
            if rd:
                domains.append(SeedCandidate("domain", rd, "seed-flag", 95.0,
                                             "user-supplied --seed domain"))
        return domains, others, entities

    wd_domains, entities = wikidata_seeds(name)
    domains.extend(wd_domains)
    others.extend([c for c in wd_domains if c.kind == "hint"])
    others.extend(edgar_seeds(name))
    others.extend(peeringdb_seeds(name))
    return domains, others, entities
