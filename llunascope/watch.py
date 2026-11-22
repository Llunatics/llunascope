"""Watch mode (temporal diffing) and M&A radar.

Watch mode persists one JSON snapshot per company — the discovered root
domains, evidence edges, per-domain ASNs, and the cert->SAN registry from a
run — and diffs the next run against it. New shadow assets (new root domains
and new evidence on known domains) are reported; quiet repeat runs report
nothing. Exit codes follow puny-patrol's watchdog convention:
0 = no new assets, 10 = new shadow asset(s), 2 = usage/state error.

M&A radar compares two snapshots (org A vs org B) and flags convergence
that did not exist in the baseline: a TLS certificate whose SAN set spans
both orgs' domains, or an ASN hosting domains from both orgs. A shared cert
or ASN that was already shared in the baseline is not news. Without a
baseline every shared cert/ASN is reported (the baseline is empty).

Everything here is offline and deterministic: diffing is order-insensitive,
snapshots are plain JSON, and a failed load/save never corrupts the previous
snapshot (saves are atomic renames; a failed fetch never touches state).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .normalize import normalize_company
from .render import _term_safe

WATCH_EXIT_ALERT = 10
SNAPSHOT_FORMAT_VERSION = 1


class WatchError(Exception):
    """Usage/state error in watch or radar mode (exit code 2)."""


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def default_snapshot_path(company: str, state_dir: str | None = None) -> Path:
    """Per-company snapshot file: ~/.cache/llunascope/<slug>.json by default.

    The slug comes from the normalized company name, so "PT Bank Mandiri
    (Persero) Tbk" and "bank mandiri" share one snapshot file.
    """
    slug = re.sub(r"[^a-z0-9]+", "-",
                  normalize_company(company)).strip("-") or "seeded"
    base = Path(state_dir) if state_dir else Path.home() / ".cache" / "llunascope"
    return base / f"{slug}.json"


def _edge_dict(e) -> dict:
    return {
        "target": e.target,
        "via": e.via,
        "hop": e.hop,
        "confidence": e.confidence,
        "evidence": [{"signal": x.signal, "detail": x.detail,
                      "weight": x.weight}
                     for x in e.evidences
                     if not x.signal.startswith("__")],
    }


def _edge_sort_key(e: dict) -> tuple:
    return (e["target"], e["via"],
            tuple(sorted(x["signal"] for x in e["evidence"])))


def snapshot_from_result(company: str, seeds: list[str], result) -> dict:
    """Build a snapshot dict from a PivotResult.

    Snapshot format (v1):
      tool, format_version, version, company, seeds, created_at (UTC ISO),
      nodes (sorted root domains), edges (sorted, with evidence),
      org_identities (domain -> Subject.O), asn_by_node (domain -> ASN),
      certs (cert_id -> sorted SAN root domains; crt.sh certs only).
    """
    return {
        "tool": "llunascope",
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "version": __version__,
        "company": _term_safe(company),
        "seeds": sorted(seeds),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nodes": sorted(result.nodes),
        "edges": sorted((_edge_dict(e) for e in result.edges),
                        key=_edge_sort_key),
        "org_identities": {k: result.org_identities[k]
                           for k in sorted(result.org_identities)},
        "asn_by_node": {k: result.asn_by_node[k]
                        for k in sorted(result.asn_by_node)},
        "certs": {cid: sorted(roots)
                  for cid, roots in sorted(result.cert_map.items())},
    }


def save_snapshot(path, snap: dict) -> None:
    """Write a snapshot atomically (temp file + rename).

    Raises WatchError on any IO failure — the previous snapshot stays intact.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        raise WatchError(f"cannot save snapshot {path}: {exc}") from exc


def _check_shape(path, snap: dict) -> None:
    """Deep-validate the fields watch/radar actually consume.

    The top-level check in load_snapshot only proves nodes/edges are
    lists. A hand-edited snapshot with the right top-level shape but
    hostile field types (``certs`` as a list, an ASN that is a dict, an
    edge that is a string) used to crash the radar/diff with a bare
    traceback (exit 1). Every consumer below assumes these types, so the
    loader is the single gate: anything malformed is a clean WatchError
    (exit 2). Unknown extra keys are tolerated for forward compatibility.
    """
    def bad(why: str) -> WatchError:
        return WatchError(f"snapshot {path} has invalid shape: {why}")

    nodes = snap.get("nodes")
    if not isinstance(nodes, list) or not all(
            isinstance(n, str) for n in nodes):
        raise bad("'nodes' must be a list of strings")
    for e in snap.get("edges", []):
        if not isinstance(e, dict):
            raise bad("each 'edges' entry must be an object")
        if not isinstance(e.get("target", ""), str) or not isinstance(
                e.get("via", ""), str):
            raise bad("edge 'target'/'via' must be strings")
        ev = e.get("evidence", [])
        if not isinstance(ev, list) or not all(
                isinstance(x, dict) and isinstance(x.get("signal", ""), str)
                for x in ev):
            raise bad("edge 'evidence' must be a list of objects "
                      "with a string 'signal'")
    certs = snap.get("certs", {})
    if not isinstance(certs, dict) or not all(
            isinstance(k, str) and isinstance(v, list) and all(
                isinstance(r, str) for r in v)
            for k, v in certs.items()):
        raise bad("'certs' must map cert ids to lists of root domains")
    asns = snap.get("asn_by_node", {})
    if not isinstance(asns, dict) or not all(
            isinstance(k, str) and isinstance(v, int)
            and not isinstance(v, bool)
            for k, v in asns.items()):
        raise bad("'asn_by_node' must map domains to integer ASNs")
    orgs = snap.get("org_identities", {})
    if not isinstance(orgs, dict) or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in orgs.items()):
        raise bad("'org_identities' must map domains to strings")
    if not isinstance(snap.get("company", ""), str):
        raise bad("'company' must be a string")
    if "created_at" in snap and not isinstance(snap["created_at"], str):
        # format_watch_report prints it raw in the "since ..." clause;
        # a non-string here used to render as repr noise, a hostile
        # string as a terminal-injection vector (audit Sun 2026-11-22).
        raise bad("'created_at' must be a string")
    seeds = snap.get("seeds", [])
    if not isinstance(seeds, list) or not all(
            isinstance(s, str) for s in seeds):
        raise bad("'seeds' must be a list of strings")


def load_snapshot(path):
    """Load a snapshot, or None if the file does not exist (first run).

    Raises WatchError on unreadable files, invalid JSON, or wrong shape
    (including hostile field types inside an otherwise valid-looking
    snapshot — see _check_shape).
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WatchError(f"cannot read snapshot {path}: {exc}") from exc
    try:
        snap = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WatchError(f"snapshot {path} is not valid JSON: {exc}") from exc
    if (not isinstance(snap, dict) or snap.get("tool") != "llunascope"
            or not isinstance(snap.get("nodes"), list)
            or not isinstance(snap.get("edges"), list)):
        raise WatchError(f"snapshot {path} is not a llunascope snapshot")
    _check_shape(path, snap)
    return snap


def check_snapshot_company(snap: dict, company: str, path) -> None:
    """Refuse a snapshot recorded for a different company.

    Raises WatchError on mismatch — silently diffing "Acme" against
    "Globex" would report the whole graph as new assets.
    """
    snap_company = str(snap.get("company", ""))
    if normalize_company(snap_company) != normalize_company(company):
        raise WatchError(
            f"snapshot {path} belongs to {_term_safe(snap_company)!r}, "
            f"not {_term_safe(company)!r} — use --snapshot to point at the "
            f"right file")


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

def _edge_key(e: dict) -> tuple:
    """Edge identity: target + via + the set of fired signals.

    A new signal firing on an already-known edge counts as a new edge —
    the evidence changed, even though the domain did not.
    """
    return (e.get("target", ""), e.get("via", ""),
            tuple(sorted(x.get("signal", "") for x in e.get("evidence", []))))


@dataclass
class WatchDiff:
    new_nodes: list[str] = field(default_factory=list)
    removed_nodes: list[str] = field(default_factory=list)
    new_edges: list[dict] = field(default_factory=list)
    removed_edges: list[dict] = field(default_factory=list)

    @property
    def new_count(self) -> int:
        """New shadow assets: new domains + new evidence on known domains.

        An edge whose target is itself a new domain is not double-counted —
        the domain is the asset, the edge is how it was found.
        """
        new_targets = set(self.new_nodes)
        return len(self.new_nodes) + len(
            [e for e in self.new_edges if e.get("target") not in new_targets])


def diff_snapshots(old: dict, new: dict) -> WatchDiff:
    """Order-insensitive diff of two snapshots."""
    old_nodes, new_nodes = set(old.get("nodes", [])), set(new.get("nodes", []))
    old_edges = {_edge_key(e): e for e in old.get("edges", [])}
    new_edges = {_edge_key(e): e for e in new.get("edges", [])}
    return WatchDiff(
        new_nodes=sorted(new_nodes - old_nodes),
        removed_nodes=sorted(old_nodes - new_nodes),
        new_edges=[new_edges[k] for k in sorted(new_edges)
                   if k not in old_edges],
        removed_edges=[old_edges[k] for k in sorted(old_edges)
                       if k not in new_edges],
    )


def format_watch_report(diff: WatchDiff, old_created_at: str | None,
                        snap_path) -> str:
    """Human-readable watch diff for stderr."""
    # created_at comes from the loaded snapshot, not from our own clock:
    # strip controls exactly like every other snapshot-sourced string.
    since = (f" since {_term_safe(old_created_at)}"
             if old_created_at else "")
    if not diff.new_count and not diff.removed_nodes and not diff.removed_edges:
        return (f"[llunascope] watch: no new shadow assets{since} "
                f"(snapshot {snap_path}).")
    lines = [f"[llunascope] watch: {diff.new_count} new shadow asset(s)"
             f"{since} (snapshot {snap_path}):"]
    for n in diff.new_nodes:
        lines.append(f"  + new root domain: {_term_safe(n)}")
    for e in diff.new_edges:
        if e.get("target") in diff.new_nodes:
            continue  # already listed as a new domain
        sigs = ", ".join(_term_safe(x.get("signal", "?"))
                         for x in e.get("evidence", []))
        lines.append(f"  + new evidence: {_term_safe(e.get('target', ''))} "
                     f"via {_term_safe(e.get('via', ''))} [{sigs}]")
    for n in diff.removed_nodes:
        lines.append(f"  - removed: {_term_safe(n)}")
    for e in diff.removed_edges:
        lines.append(f"  - evidence gone: {_term_safe(e.get('target', ''))} "
                     f"via {_term_safe(e.get('via', ''))}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# M&A radar
# ---------------------------------------------------------------------------

@dataclass
class SharedCert:
    cert_id: str
    roots_a: list[str]
    roots_b: list[str]


@dataclass
class SharedAsn:
    asn: int
    domains_a: list[str]
    domains_b: list[str]


@dataclass
class RadarResult:
    company_a: str
    company_b: str
    new_shared_certs: list[SharedCert] = field(default_factory=list)
    new_shared_asns: list[SharedAsn] = field(default_factory=list)
    total_shared_certs: int = 0
    total_shared_asns: int = 0
    had_baseline: bool = False

    @property
    def converged(self) -> bool:
        return bool(self.new_shared_certs or self.new_shared_asns)


def _shared_certs(snap_a: dict, snap_b: dict) -> dict[str, SharedCert]:
    """Certs whose SAN set spans both orgs' node sets, keyed by cert id."""
    nodes_a, nodes_b = set(snap_a.get("nodes", [])), set(snap_b.get("nodes", []))
    certs_a, certs_b = snap_a.get("certs", {}), snap_b.get("certs", {})
    out: dict[str, SharedCert] = {}
    for cid in sorted(set(certs_a) & set(certs_b)):
        roots_a = sorted(set(certs_a[cid]) & nodes_a)
        roots_b = sorted(set(certs_b[cid]) & nodes_b)
        if roots_a and roots_b:
            out[cid] = SharedCert(cid, roots_a, roots_b)
    return out


def _shared_asns(snap_a: dict, snap_b: dict) -> dict[int, SharedAsn]:
    """ASNs hosting domains from both orgs, keyed by ASN."""
    by_asn_a: dict[int, list[str]] = {}
    for dom, asn in (snap_a.get("asn_by_node") or {}).items():
        by_asn_a.setdefault(asn, []).append(dom)
    by_asn_b: dict[int, list[str]] = {}
    for dom, asn in (snap_b.get("asn_by_node") or {}).items():
        by_asn_b.setdefault(asn, []).append(dom)
    out: dict[int, SharedAsn] = {}
    for asn in sorted(set(by_asn_a) & set(by_asn_b)):
        out[asn] = SharedAsn(asn, sorted(by_asn_a[asn]),
                             sorted(by_asn_b[asn]))
    return out


def detect_convergence(snap_a: dict, snap_b: dict,
                       base_a: dict | None = None,
                       base_b: dict | None = None) -> RadarResult:
    """Flag cert/ASN convergence between two orgs that is new vs the baseline.

    base_a/base_b must be given together or not at all. Without a baseline
    every shared cert/ASN counts as new (empty baseline).
    """
    if (base_a is None) != (base_b is None):
        raise WatchError("radar baseline needs both snapshots "
                         "(--radar-baseline A-old B-old)")
    # Radar compares two *different* orgs. Two snapshots of the same
    # company (or the same file twice) share every cert/ASN by
    # construction, so without this guard they report "NEW convergence"
    # (exit 10) — a false M&A signal from an operator typo. Unknown
    # companies (empty after normalization) are still allowed through.
    comp_a = normalize_company(str(snap_a.get("company", "")))
    comp_b = normalize_company(str(snap_b.get("company", "")))
    if comp_a and comp_b and comp_a == comp_b:
        raise WatchError(
            "radar compares two different companies — both snapshots "
            f"belong to {_term_safe(str(snap_a.get('company', '?')))!r}")
    shared_certs = _shared_certs(snap_a, snap_b)
    shared_asns = _shared_asns(snap_a, snap_b)
    if base_a is not None and base_b is not None:
        base_certs = _shared_certs(base_a, base_b)
        base_asns = _shared_asns(base_a, base_b)
        new_certs = [c for cid, c in shared_certs.items()
                     if cid not in base_certs]
        new_asns = [a for asn, a in shared_asns.items()
                    if asn not in base_asns]
    else:
        new_certs = list(shared_certs.values())
        new_asns = list(shared_asns.values())
    return RadarResult(
        company_a=str(snap_a.get("company", "?")),
        company_b=str(snap_b.get("company", "?")),
        new_shared_certs=new_certs,
        new_shared_asns=new_asns,
        total_shared_certs=len(shared_certs),
        total_shared_asns=len(shared_asns),
        had_baseline=base_a is not None,
    )


def format_radar_report(res: RadarResult) -> str:
    """Human-readable radar report for stdout."""
    a, b = _term_safe(res.company_a), _term_safe(res.company_b)
    lines = [f"\U0001f319 M&A radar: {a} vs {b}"]
    lines.append(f"  shared certificates: {res.total_shared_certs} "
                 f"({len(res.new_shared_certs)} new"
                 f"{' since baseline' if res.had_baseline else ''})")
    lines.append(f"  shared ASNs: {res.total_shared_asns} "
                 f"({len(res.new_shared_asns)} new"
                 f"{' since baseline' if res.had_baseline else ''})")
    if not res.converged:
        lines.append("  no new convergence"
                     + (" since baseline." if res.had_baseline
                        else " — the two orgs share no certs or ASNs."))
        return "\n".join(lines)
    lines.append("  NEW convergence:")
    for c in res.new_shared_certs:
        lines.append(f"    cert {_term_safe(c.cert_id)}: "
                     f"{', '.join(_term_safe(r) for r in c.roots_a)} "
                     f"({a}) <-> "
                     f"{', '.join(_term_safe(r) for r in c.roots_b)} ({b})")
    for s in res.new_shared_asns:
        lines.append(f"    AS{s.asn}: "
                     f"{', '.join(_term_safe(d) for d in s.domains_a)} "
                     f"({a}) <-> "
                     f"{', '.join(_term_safe(d) for d in s.domains_b)} ({b})")
    return "\n".join(lines)
