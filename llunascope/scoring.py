"""Confidence scoring for lineage edges.

Every edge carries its evidence chain (which signals fired) plus a 0-100
confidence score. Weights are conservative by default and published in the
README so anyone can audit or challenge them.

Signal weights (v1):
  SAN_OVERLAP      40  — two root domains share a TLS certificate (CT logs)
  ORG_TUPLE        35  — two root domains share an exact Subject.O (OV/EV cert)
  ASN_MATCH        25  — two root domains resolve into the same non-ignored ASN
  RDAP_ORG         15  — same RDAP org handle (weak: post-GDPR redaction)

Penalties:
  SAN_CEILING       x0.2 — cert with >30 distinct root domains in SAN
                           (shared CDN/WAF cert; likely unrelated tenants)
  IGNORED_ASN       skip — cloud/CDN/large-ISP ASN, never pivoted on

Confidence = min(99, sum of fired signal weights) * penalties.
"""

from __future__ import annotations

from dataclasses import dataclass, field

WEIGHTS = {
    "san_overlap": 40.0,
    "org_tuple": 35.0,
    "asn_match": 25.0,
    "rdap_org": 15.0,
}
# NOTE: seed domains (Hop-0 resolved or --seed) are trusted ROOTS, not graded
# edges — they carry no signal weight. Registry leads (Wikidata subsidiaries,
# EDGAR listings, PeeringDB orgs) are surfaced as follow-up hints, not edges.

SAN_CEILING = 30          # distinct root domains in one cert -> shared-cert penalty
SAN_CEILING_FACTOR = 0.2
MAX_CONFIDENCE = 99.0


@dataclass
class Evidence:
    signal: str
    detail: str
    weight: float = 0.0


@dataclass
class Edge:
    """A lineage edge: seed/hop domain -> discovered root domain."""
    target: str
    via: str                      # parent domain (or 'SEED')
    hop: int
    evidences: list[Evidence] = field(default_factory=list)
    confidence: float = 0.0
    penalized: bool = False

    def add(self, signal: str, detail: str) -> None:
        self.evidences.append(Evidence(signal, detail, WEIGHTS.get(signal, 0.0)))

    def finalize(self) -> "Edge":
        total = sum(e.weight for e in self.evidences)
        if any(e.signal == "__san_ceiling__" for e in self.evidences):
            total *= SAN_CEILING_FACTOR
            self.penalized = True
        self.confidence = round(min(MAX_CONFIDENCE, total), 1)
        return self

    def summary(self) -> str:
        bits = [f"{e.signal}({e.weight:g})" for e in self.evidences
                if not e.signal.startswith("__")]
        s = " + ".join(bits)
        if self.penalized:
            s += " [shared-cert penalty x0.2]"
        return s


def describe_weights() -> str:
    lines = ["Signal weights (v1, conservative):"]
    for sig, w in WEIGHTS.items():
        lines.append(f"  {sig:15s} {w:5.1f}")
    lines.append(f"  san ceiling: >{SAN_CEILING} roots in one cert -> confidence x{SAN_CEILING_FACTOR}")
    lines.append("  confidence = min(99, sum of fired weights) x penalties")
    return "\n".join(lines)
