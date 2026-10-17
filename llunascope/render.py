"""Output rendering: terminal tree, mermaid/dot, HTML report, JSON."""

from __future__ import annotations

import html
import json
import sys

from .scoring import Edge

try:
    from rich.console import Console
    from rich.tree import Tree
    from rich.status import Status
    RICH = True
except Exception:
    RICH = False


def _children_map(edges: list[Edge]) -> dict[str, list[Edge]]:
    m: dict[str, list[Edge]] = {}
    for e in edges:
        m.setdefault(e.via, []).append(e)
    for v in m.values():
        v.sort(key=lambda e: -e.confidence)
    return m


def print_tree(seed_label: str, edges: list[Edge], file=None) -> None:
    """Pretty lineage tree. rich if available, else plain ASCII."""
    out = file or sys.stdout
    kids = _children_map(edges)

    def label(e: Edge) -> str:
        return f"{e.target}  [{e.confidence:g}%]"

    if RICH and out.isatty():
        console = Console(file=out)
        tree = Tree(f"[bold magenta]\U0001f319 {seed_label}[/bold magenta]")
        seen: set[str] = set()

        def add(node: Tree, via: str, depth: int) -> None:
            if depth > 4:
                return
            for e in kids.get(via, []):
                if e.target in seen:
                    continue
                seen.add(e.target)
                branch = node.add(
                    f"[cyan]{e.target}[/cyan]  [dim][{e.confidence:g}%][/dim]")
                if e.evidences:
                    ev = branch.add("[dim]evidence[/dim]")
                    for evd in e.evidences:
                        if evd.signal.startswith("__"):
                            continue
                        ev.add(f"[dim]{evd.signal}: {evd.detail}[/dim]")
                add(branch, e.target, depth + 1)

        add(tree, "SEED", 0)
        # Also attach edges whose via is a seed domain name.
        console.print(tree)
        return

    # ASCII fallback.
    print(f"\U0001f319 {seed_label}", file=out)
    seen = set()

    def walk(via: str, prefix: str, depth: int) -> None:
        if depth > 4:
            return
        items = kids.get(via, [])
        for i, e in enumerate(items):
            if e.target in seen:
                continue
            seen.add(e.target)
            last = i == len(items) - 1
            elbow, cont = ("\u2514\u2500\u2500 ", "    ") if last else ("\u251c\u2500\u2500 ", "\u2502   ")
            print(f"{prefix}{elbow}{label(e)}", file=out)
            ev_prefix = prefix + cont
            for evd in e.evidences:
                if evd.signal.startswith("__"):
                    continue
                print(f"{ev_prefix}\u00b7 {evd.signal}: {evd.detail}", file=out)
            walk(e.target, ev_prefix, depth + 1)

    walk("SEED", "", 0)


def to_mermaid(seed_label: str, edges: list[Edge]) -> str:
    """Mermaid flowchart. Renders natively in GitHub markdown."""
    lines = ["flowchart TD"]
    safe = lambda s: re_sub(s)
    lines.append(f'    SEED["\U0001f319 {seed_label}"]')
    for e in edges:
        a, b = safe(e.via), safe(e.target)
        sig = "+".join(s for s in (x.signal for x in e.evidences)
                       if not s.startswith("__")) or "linked"
        lines.append(f'    {a} -->|"{e.confidence:g}% · {sig}"| {b}')
    return "\n".join(lines) + "\n"


def re_sub(s: str) -> str:
    import re as _re
    s = _re.sub(r"[^A-Za-z0-9_]", "_", s)
    if s and s[0].isdigit():
        s = "n_" + s
    return s or "node"


def to_dot(seed_label: str, edges: list[Edge]) -> str:
    lines = ['digraph llunascope {', '  rankdir=LR;',
             f'  "SEED" [label="\U0001f319 {seed_label}", shape=doublecircle];']
    for e in edges:
        sig = "+".join(s for s in (x.signal for x in e.evidences)
                       if not s.startswith("__")) or "linked"
        lines.append(f'  "{e.via}" -> "{e.target}" [label="{e.confidence:g}% {sig}"];')
    lines.append("}")
    return "\n".join(lines) + "\n"


def to_json(seed_label: str, company: str, edges: list[Edge],
            nodes: set[str], org_identities: dict) -> str:
    return json.dumps({
        "tool": "llunascope",
        "company": company,
        "seed": seed_label,
        "nodes": sorted(nodes),
        "org_identities": org_identities,
        "edges": [{
            "target": e.target, "via": e.via, "hop": e.hop,
            "confidence": e.confidence,
            "evidence": [{"signal": x.signal, "detail": x.detail,
                          "weight": x.weight} for x in e.evidences],
        } for e in sorted(edges, key=lambda x: -x.confidence)],
    }, indent=2, ensure_ascii=False)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>llunascope — {company}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  body {{ background:#0d1117; color:#c9d1d9; font-family:system-ui,sans-serif; margin:0; padding:2rem; }}
  h1 {{ color:#e6edf3; }} h1 .moon {{ color:#a371f7; }}
  .tag {{ color:#8b949e; font-style:italic; margin-top:-0.8rem; }}
  .mermaid {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:1rem; }}
  table {{ border-collapse:collapse; width:100%; margin-top:1.5rem; }}
  th,td {{ border:1px solid #30363d; padding:0.5rem 0.7rem; text-align:left; font-size:0.9rem; }}
  th {{ background:#161b22; color:#e6edf3; }}
  .conf {{ font-weight:bold; color:#7ee787; }}
  .foot {{ color:#8b949e; font-size:0.8rem; margin-top:2rem; }}
  code {{ background:#161b22; padding:0.15rem 0.4rem; border-radius:4px; }}
</style></head><body>
<h1><span class="moon">\U0001f319</span> llunascope — {company_html}</h1>
<p class="tag">The moon never knocks. It just watches. — zero packets sent, zero API keys.</p>
<div class="mermaid">
{mermaid}
</div>
<h2>Evidence chains</h2>
<table><tr><th>Target</th><th>Via</th><th>Confidence</th><th>Evidence</th></tr>
{rows}
</table>
<p class="foot">Generated by <code>llunascope</code> · passive EASM · {n_nodes} root domains, {n_edges} edges.
Signals: san_overlap, org_tuple, asn_match, registry_hint, rdap_org. Weights published in README.</p>
<script>mermaid.initialize({{startOnLoad:true, theme:'dark'}});</script>
</body></html>
"""


def to_html(company: str, seed_label: str, edges: list[Edge], nodes: set[str],
            org_identities: dict) -> str:
    rows = []
    for e in sorted(edges, key=lambda x: -x.confidence):
        evs = "<br>".join(
            f"<code>{html.escape(x.signal)}</code> {html.escape(x.detail)}"
            for x in e.evidences if not x.signal.startswith("__"))
        rows.append(
            f"<tr><td><code>{html.escape(e.target)}</code></td>"
            f"<td><code>{html.escape(e.via)}</code></td>"
            f"<td class='conf'>{e.confidence:g}%</td><td>{evs}</td></tr>")
    return HTML_TEMPLATE.format(
        company=company, company_html=html.escape(company),
        mermaid=to_mermaid(seed_label, edges),
        rows="\n".join(rows), n_nodes=len(nodes), n_edges=len(edges))
