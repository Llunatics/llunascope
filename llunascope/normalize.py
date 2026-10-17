"""Name normalization and root-domain extraction."""

from __future__ import annotations

import re
import unicodedata

# Legal entity suffixes stripped during company-name normalization.
LEGAL_SUFFIXES = {
    "pt", "tbk", "persero", "ltd", "limited", "inc", "incorporated", "corp",
    "corporation", "llc", "gmbh", "plc", "sa", "srl", "bv", "nv", "ag",
    "pty", "sdn", "bhd", "co", "company", "holdings", "holding", "group",
    "the",
}

# Common multi-level public suffixes (minimal embedded list; default rule is
# "last two labels"). Enough for the TLDs we realistically encounter.
MULTI_SUFFIXES = {
    "co.id", "or.id", "ac.id", "go.id", "sch.id", "web.id", "my.id", "biz.id",
    "co.uk", "ac.uk", "gov.uk", "org.uk", "me.uk", "ltd.uk", "net.uk",
    "com.au", "net.au", "org.au", "edu.au", "id.au",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "ed.jp",
    "co.kr", "or.kr", "ne.kr", "go.kr",
    "com.br", "net.br", "org.br", "gov.br", "edu.br",
    "co.in", "net.in", "org.in", "gov.in", "ac.in",
    "co.za", "org.za", "net.za", "gov.za",
    "com.mx", "com.ar", "com.co", "com.pe", "com.sg", "com.my", "com.ph",
    "com.vn", "com.th", "co.th", "or.th", "go.th",
    "co.nz", "org.nz", "net.nz", "gov.nz",
    "com.tr", "com.eg", "com.sa", "com.ae", "com.qa", "com.kw",
    "com.es", "nom.es", "org.es", "gob.es", "edu.es",
    "com.tw", "net.tw", "org.tw", "idv.tw",
    "com.hk", "net.hk", "org.hk", "edu.hk", "gov.hk",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
    "com.ru", "net.ru", "org.ru", "pp.ru",
    "co.il", "org.il", "net.il", "ac.il", "gov.il",
    "com.ng", "com.gh", "co.ke",
    "com.ec", "com.ve", "com.uy", "com.py", "com.bo",
    "nom.br", "eti.br",
}


def normalize_company(name: str) -> str:
    """Fold a company name to a comparable token string.

    'PT Bank Mandiri (Persero) Tbk' -> 'bank mandiri'
    """
    text = unicodedata.normalize("NFKD", name)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[&]", " and ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = [t for t in text.split() if t not in LEGAL_SUFFIXES]
    return " ".join(tokens).strip()


def token_set(name: str) -> set[str]:
    return set(normalize_company(name).split())


def name_similarity(a: str, b: str) -> float:
    """Jaccard similarity over normalized token sets (0..1)."""
    ta, tb = token_set(a), token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def root_domain(host: str) -> str:
    """Extract the registrable root domain from a hostname.

    'www.chat.bankmandiri.co.id' -> 'bankmandiri.co.id'
    """
    host = host.strip().lower().rstrip(".")
    # Strip wildcard prefix that appears in CT logs.
    if host.startswith("*."):
        host = host[2:]
    # Skip non-DNS SAN entries (IPs, emails, URIs).
    if not host or " " in host or "@" in host or "/" in host:
        return ""
    if re.fullmatch(r"[0-9a-f:.]+", host) and (":" in host or host.replace(".", "").isdigit()):
        return ""  # IP literal
    labels = host.split(".")
    if len(labels) < 2:
        return ""
    # Multi-level suffix: "www.bankmandiri.co.id" -> "bankmandiri.co.id".
    # If the last two labels form a known multi-level suffix, the registrable
    # root includes one more label.
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    root = ".".join(labels[-2:])
    # Guard: never return a bare public suffix ("co.id" itself is not a domain).
    if root in MULTI_SUFFIXES:
        return ""
    return root


def is_subdomain_of(host: str, root: str) -> bool:
    host = host.strip().lower().rstrip(".")
    root = root.strip().lower().rstrip(".")
    return host == root or host.endswith("." + root)
