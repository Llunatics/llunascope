"""Horizontal Pivot Engine: root domains -> related root domains.

Sources (all passive, all failure-isolated):
  - CT logs via crt.sh JSON API (primary), CertSpotter API (fallback)
  - ASN co-location via RIPE Stat, gated by a cloud/CDN ASN ignorelist and a
    /16 prefix-size threshold
  - RDAP org-handle grouping (weak signal post-GDPR)
  - Cert Org-Tuple pivot: Subject.O extracted (best-effort, via openssl) from
    certs issued by non-DV CAs; roots sharing an exact O= are strongly linked

Expansion is bounded: max 3 hops, visited-set, max 500 nodes. Loops impossible.
"""

from __future__ import annotations

import ipaddress
import re
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import net
from .normalize import root_domain
from .scoring import Edge, Evidence, SAN_CEILING

CRTSH_URL = "https://crt.sh/"
CERTSPOTTER_URL = "https://api.certspotter.com/v1/issuances"
RIPE_NETINFO = "https://stat.ripe.net/data/network-info/data.json"
RIPE_WHOIS = "https://stat.ripe.net/data/whois/data.json"
IANA_RDAP_BOOTSTRAP = "https://data.iana.org/rdap/dns.json"

MAX_HOPS = 3
MAX_NODES = 500
MAX_CRTSH_RESULTS = 500

# ASNs we NEVER pivot on: cloud, CDN, WAF, and very large ISP eyeball networks.
IGNORED_ASNS = {
    13335, 209242,                  # Cloudflare
    16509, 14618, 8987,             # AWS
    15169, 36040, 43515,            # Google / Google Cloud
    8075,                           # Microsoft / Azure
    20940, 16625, 21342,            # Akamai
    54113,                          # Fastly
    15133,                          # Edgecast
    22822,                          # Edgio (Limelight)
    19551,                          # Imperva
    17995, 4761, 17974, 7713,       # Telkom Indonesia / Indosat / Telkomsel
}

# CAs that only (or overwhelmingly) issue DV certs — no Subject.O to pivot on.
DV_ISSUERS = ("let's encrypt", "letsencrypt", "zerossl", "google trust services",
              "ssl.com", "buypass")

MAX_PIVOT_PREFIX_V4 = 2 ** 16  # /16


@dataclass
class CertInfo:
    cert_id: str
    issuer_o: str
    san_roots: set[str]
    subject_o: str = ""   # filled lazily for OV/EV candidates


def _parse_dn_o(dn: str) -> str:
    m = re.search(r"(?:^|,)\s*O=([^,]+)", dn or "")
    return m.group(1).strip() if m else ""


def _crtsh_certs(domain: str, limit: int = MAX_CRTSH_RESULTS) -> list[dict]:
    # Exact-domain query: cheap and reliable. SANs in the results still reveal
    # related domains, which is what the pivot needs.
    data = net.fetch_json(CRTSH_URL, params={"q": domain, "output": "json"}, timeout=45)
    if isinstance(data, list):
        return data[:limit]
    return []


def _certspotter_certs(domain: str) -> list[dict]:
    data = net.fetch_json(
        CERTSPOTTER_URL,
        params={"domain": domain, "include_subdomains": "true", "expand": "dns_names"},
        timeout=30)
    return data if isinstance(data, list) else []


def fetch_certs(domain: str, limit: int = MAX_CRTSH_RESULTS) -> list[CertInfo]:
    """CT-log certificates covering a root domain.

    Merges crt.sh AND CertSpotter (deduplicated by SAN set) — neither source
    is complete alone, and crt.sh is flaky under load. A dead source just
    contributes zero certs; the run never crashes.
    """
    seen_sans: set[frozenset] = set()
    certs: list[CertInfo] = []

    def add(ci: CertInfo) -> None:
        key = frozenset(ci.san_roots)
        if key and key not in seen_sans:
            seen_sans.add(key)
            certs.append(ci)

    for c in _crtsh_certs(domain, limit):
        try:
            sans: set[str] = set()
            for line in str(c.get("name_value", "")).splitlines():
                rd = root_domain(line.strip())
                if rd:
                    sans.add(rd)
            if sans:
                add(CertInfo(cert_id=str(c.get("id", "")),
                             issuer_o=_parse_dn_o(c.get("issuer_name", "")),
                             san_roots=sans))
        except Exception:
            continue
    for c in _certspotter_certs(domain):
        try:
            sans = {root_domain(n) for n in (c.get("dns_names") or [])}
            sans = {s for s in sans if s}
            if sans:
                add(CertInfo(cert_id="", issuer_o="", san_roots=sans))
        except Exception:
            continue
    return certs


def _openssl_subject_o(pem_bytes: bytes) -> str:
    """Extract Subject.O via openssl CLI. '' on any failure (openssl missing, etc.)."""
    if not shutil.which("openssl"):
        return ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
            f.write(pem_bytes)
            path = f.name
        try:
            out = subprocess.run(
                ["openssl", "x509", "-in", path, "-noout", "-subject"],
                capture_output=True, timeout=15, text=True)
            if out.returncode == 0:
                return _parse_dn_o(out.stdout)
        finally:
            Path(path).unlink(missing_ok=True)
    except Exception:
        pass
    return ""


def enrich_subject_o(certs: list[CertInfo], limit: int = 3) -> None:
    """Best-effort: download PEMs for non-DV-issuer certs, extract Subject.O.

    Bounded and fully failure-isolated — a 429 or a missing openssl just means
    this signal doesn't fire. Keep the per-download timeout short: this runs
    once per domain and must never dominate runtime.
    """
    done = 0
    attempts = 0
    max_attempts = limit * 3  # don't hammer a rate-limiting crt.sh forever
    for cert in certs:
        if done >= limit or attempts >= max_attempts:
            break
        if not cert.cert_id:
            continue
        issuer = (cert.issuer_o or "").lower()
        if any(dv in issuer for dv in DV_ISSUERS):
            continue  # DV cert: no Subject.O, skip the download
        attempts += 1
        try:
            pem = net.fetch_text(CRTSH_URL, params={"d": cert.cert_id}, timeout=12)
            if pem and "BEGIN CERTIFICATE" in pem:
                o = _openssl_subject_o(pem.encode())
                if o:
                    cert.subject_o = o
                    done += 1
        except Exception:
            continue


def resolve_ips(domain: str) -> list[str]:
    """DNS A/AAAA for a domain. DNS only — never touches target infra."""
    ips: list[str] = []
    try:
        for fam in (socket.AF_INET, socket.AF_INET6):
            try:
                for info in socket.getaddrinfo(domain, None, fam, socket.SOCK_STREAM):
                    ip = info[4][0]
                    if ip not in ips:
                        ips.append(ip)
            except Exception:
                continue
    except Exception:
        pass
    return ips[:8]


@dataclass
class AsnInfo:
    asn: int
    prefix: str
    prefix_size_v4: int = 0


def ip_to_asn(ip: str) -> AsnInfo | None:
    data = net.fetch_json(RIPE_NETINFO, params={"resource": ip}, timeout=20)
    if not isinstance(data, dict):
        return None
    asns = (data.get("data") or {}).get("asns") or []
    if not asns:
        return None
    try:
        asn = int(asns[0])
    except Exception:
        return None
    prefix, size = "", 0
    try:
        w = net.fetch_json(RIPE_WHOIS, params={"resource": ip}, timeout=20)
        for rec in (w.get("data") or {}).get("records") or []:
            for obj in rec:
                if obj.get("key") in ("route", "inetnum", "inet6num"):
                    prefix = str(obj.get("value", "")).split()[0]
                    break
            if prefix:
                break
        if prefix and "/" in prefix:
            n = ipaddress.ip_network(prefix, strict=False)
            if n.version == 4:
                size = n.num_addresses
    except Exception:
        pass
    return AsnInfo(asn, prefix, size)


_rdap_bootstrap: dict | None = None


def _rdap_server_for(tld: str) -> str | None:
    global _rdap_bootstrap
    if _rdap_bootstrap is None:
        data = net.fetch_json(IANA_RDAP_BOOTSTRAP, timeout=20, use_cache=True)
        _rdap_bootstrap = data if isinstance(data, dict) else {}
    for svc in (_rdap_bootstrap.get("services") or []):
        tlds, urls = svc[0], svc[1]
        if tld in tlds and urls:
            return urls[0].rstrip("/")
    return None


def rdap_org_handle(domain: str) -> str:
    """Best-effort RDAP registrant/org handle. '' if unavailable/redacted."""
    try:
        tld = domain.rsplit(".", 1)[-1]
        server = _rdap_server_for(tld)
        if not server:
            return ""
        data = net.fetch_json(f"{server}/domain/{domain}", timeout=15, use_cache=True)
        if not isinstance(data, dict):
            return ""
        for ent in data.get("entities") or []:
            roles = ent.get("roles") or []
            if any(r in ("registrant", "administrative", "technical") for r in roles):
                tag = ent.get("handle") or ""
                try:
                    for item in (ent.get("vcardArray") or [[], []])[1]:
                        if item[0] == "org":
                            tag = str(item[3]) or tag
                except Exception:
                    pass
                if tag and not re.search(
                        r"privacy|redact|whoisguard|withheld|not disclosed|redacted",
                        tag, re.I):
                    return tag.strip()[:80]
        return ""
    except Exception:
        return ""


@dataclass
class PivotResult:
    edges: list[Edge] = field(default_factory=list)
    nodes: set[str] = field(default_factory=set)
    org_identities: dict[str, str] = field(default_factory=dict)  # domain -> Subject.O


def _edge_for(edges: list[Edge], target: str, via: str) -> Edge | None:
    for e in edges:
        if e.target == target and e.via == via:
            return e
    return None


def pivot(seed_domains: list[str], verbose_cb=None,
          org_tuple_hops: int = 1) -> PivotResult:
    """Bounded BFS expansion over the pivot signals.

    org_tuple_hops: run the (slow, best-effort) Subject.O enrichment only for
    hops below this depth. Default 1 = seed domains only.
    """
    result = PivotResult()
    visited: set[str] = set()
    frontier: list[tuple[str, int, str]] = [(d, 0, "SEED") for d in seed_domains]
    for d in seed_domains:
        result.nodes.add(d)

    asn_cache: dict[str, AsnInfo | None] = {}
    rdap_cache: dict[str, str] = {}

    def note(msg: str) -> None:
        if verbose_cb:
            verbose_cb(msg)

    def asn_of(domain: str) -> AsnInfo | None:
        if domain not in asn_cache:
            info = None
            for ip in resolve_ips(domain):
                info = ip_to_asn(ip)
                if info:
                    break
            asn_cache[domain] = info
        return asn_cache[domain]

    while frontier and len(result.nodes) < MAX_NODES:
        domain, hop, via = frontier.pop(0)
        if domain in visited or hop > MAX_HOPS:
            continue
        visited.add(domain)
        note(f"[hop {hop}] pivoting {domain}")

        note(f"  -> CT logs...")
        certs = fetch_certs(domain, limit=500 if hop == 0 else 150)
        note(f"  -> {len(certs)} certs")
        if hop < org_tuple_hops:
            note(f"  -> Subject.O enrichment...")
            enrich_subject_o(certs)
            note(f"  -> O= done")

        # Record Subject.O identities for the org-tuple signal.
        for cert in certs:
            if cert.subject_o:
                for rd in cert.san_roots:
                    if rd not in result.org_identities:
                        result.org_identities[rd] = cert.subject_o

        # --- Signal 1: SAN overlap --------------------------------------
        san_hits: dict[str, list[CertInfo]] = {}
        for cert in certs:
            for rd in cert.san_roots:
                if rd != domain:
                    san_hits.setdefault(rd, []).append(cert)
        for rd, clist in san_hits.items():
            shared = [c for c in clist if len(c.san_roots) <= SAN_CEILING]
            polluted = [c for c in clist if len(c.san_roots) > SAN_CEILING]
            if not shared:
                continue  # only via shared CDN/WAF certs: too weak, skip
            edge = _edge_for(result.edges, rd, domain) or Edge(rd, domain, hop + 1)
            is_new = edge not in result.edges
            if not any(e.signal == "san_overlap" for e in edge.evidences):
                detail = f"{len(shared)} shared cert(s) also cover {rd}"
                if shared[0].cert_id:
                    detail += f" (e.g. serial {shared[0].cert_id[:12]}…)"
                edge.add("san_overlap", detail)
            if polluted and not any(e.signal == "__san_ceiling__"
                                    for e in edge.evidences):
                edge.evidences.append(Evidence(
                    "__san_ceiling__",
                    f"also shares a cert covering "
                    f"{max(len(c.san_roots) for c in polluted)} roots (CDN/WAF)"))
            edge.finalize()
            if is_new:
                result.edges.append(edge)
            if rd not in visited and len(result.nodes) < MAX_NODES:
                result.nodes.add(rd)
                frontier.append((rd, hop + 1, domain))

        # --- Signal 2: Org-Tuple (exact Subject.O across roots) ----------
        o_here = result.org_identities.get(domain, "")
        if o_here:
            for other, o_other in result.org_identities.items():
                if other == domain or o_other != o_here:
                    continue
                edge = _edge_for(result.edges, other, domain)
                if edge is None:
                    edge = Edge(other, domain, hop + 1)
                    result.edges.append(edge)
                if not any(e.signal == "org_tuple" for e in edge.evidences):
                    edge.add("org_tuple", f"identical Subject.O='{o_here[:60]}' (OV/EV cert)")
                    edge.finalize()

        # --- Signal 3: ASN co-location (gated) ---------------------------
        note(f"  -> ASN lookup...")
        info = asn_of(domain)
        note(f"  -> ASN: {info.asn if info else None}")
        if info and info.asn not in IGNORED_ASNS:
            pivotable = (info.prefix_size_v4 == 0
                         or info.prefix_size_v4 <= MAX_PIVOT_PREFIX_V4)
            if pivotable:
                for other in list(result.nodes):
                    if other == domain:
                        continue
                    oinfo = asn_of(other)
                    if oinfo and oinfo.asn == info.asn:
                        edge = _edge_for(result.edges, other, domain)
                        if edge is None:
                            edge = Edge(other, domain, hop + 1)
                            result.edges.append(edge)
                        if not any(e.signal == "asn_match" for e in edge.evidences):
                            edge.add("asn_match",
                                     f"both resolve into AS{info.asn}"
                                     + (f" ({info.prefix})" if info.prefix else ""))
                            edge.finalize()

        # --- Signal 4: RDAP org handle (weak) ----------------------------
        note(f"  -> RDAP...")
        if domain not in rdap_cache:
            rdap_cache[domain] = rdap_org_handle(domain)
        note(f"  -> RDAP done")
        handle = rdap_cache[domain]
        if handle:
            for other in list(result.nodes):
                if other == domain:
                    continue
                if other not in rdap_cache:
                    rdap_cache[other] = rdap_org_handle(other)
                if rdap_cache[other] == handle:
                    edge = _edge_for(result.edges, other, domain)
                    if edge is None:
                        edge = Edge(other, domain, hop + 1)
                        result.edges.append(edge)
                    if not any(e.signal == "rdap_org" for e in edge.evidences):
                        edge.add("rdap_org", f"shared RDAP handle '{handle[:40]}'")
                        edge.finalize()

    # Finalize any edges that never got finalized.
    for e in result.edges:
        if e.confidence == 0 and e.evidences:
            e.finalize()
    return result
