"""Output rendering: terminal tree, mermaid/dot, HTML report, JSON."""

from __future__ import annotations

import html
import json
import re
import sys

from .scoring import Edge

try:
    from rich.console import Console
    from rich.markup import escape as rich_escape
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


def print_tree(seed_label: str, edges: list[Edge], file=None,
               seed_roots: list[str] | None = None) -> None:
    """Pretty lineage tree. rich if available, else plain ASCII.

    seed_roots: the actual seed domain names — edge `via` fields carry these,
    never the display label, so they must be supplied for children to render.
    """
    out = file or sys.stdout
    kids = _children_map(edges)
    roots = seed_roots or []

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
                    f"[cyan]{rich_escape(e.target)}[/cyan]  [dim][{e.confidence:g}%][/dim]")
                if e.evidences:
                    ev = branch.add("[dim]evidence[/dim]")
                    for evd in e.evidences:
                        if evd.signal.startswith("__"):
                            continue
                        ev.add(f"[dim]{rich_escape(evd.signal)}: "
                               f"{rich_escape(evd.detail)}[/dim]")
                add(branch, e.target, depth + 1)

        for r in roots:
            add(tree, r, 0)
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

    for r in roots:
        walk(r, "", 0)


def _mermaid_label(s: str) -> str:
    """Make a string safe for a mermaid quoted node label.

    Double quotes are mermaid-escaped as #quot; — newlines are flattened.
    """
    return s.replace('"', "#quot;").replace("\n", " ").replace("\r", "")


def _unique_ids(names: list[str]) -> dict[str, str]:
    """Map display names to unique mermaid node ids.

    re_sub() alone can collide (e.g. 'a-b.com' and 'a_b.com' both become
    'a_b_com'), silently merging two domains into one graph node. Suffix
    collisions instead.
    """
    used: set[str] = set()
    out: dict[str, str] = {}
    for name in names:
        if name in out:
            continue  # same domain referenced by several edges: one node
        base = re_sub(name)
        cand, n = base, 2
        while cand in used:
            cand = f"{base}_{n}"
            n += 1
        used.add(cand)
        out[name] = cand
    return out


def to_mermaid(seed_label: str, edges: list[Edge]) -> str:
    """Mermaid flowchart. Renders natively in GitHub markdown."""
    names = ["SEED"]
    for e in edges:
        names.extend([e.via, e.target])
    ids = _unique_ids(names)
    lines = ["flowchart TD"]
    lines.append(f'    {ids["SEED"]}["\U0001f319 {_mermaid_label(seed_label)}"]')
    for e in edges:
        a, b = ids[e.via], ids[e.target]
        sig = "+".join(s for s in (x.signal for x in e.evidences)
                       if not s.startswith("__")) or "linked"
        lines.append(f'    {a} -->|"{e.confidence:g}% · {sig}"| {b}')
        if e.hop == 1:
            # Hop-1 edges hang off the seed: connect the SEED node so the
            # graph is one connected tree instead of a floating root label.
            lines.append(f'    {ids["SEED"]} --> {b}')
    return "\n".join(lines) + "\n"


def re_sub(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    if s and s[0].isdigit():
        s = "n_" + s
    return s or "node"


def _dot_label(s: str) -> str:
    """Escape a string for a DOT double-quoted label."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def to_dot(seed_label: str, edges: list[Edge]) -> str:
    lines = ['digraph llunascope {', '  rankdir=LR;',
             f'  "SEED" [label="\U0001f319 {_dot_label(seed_label)}", shape=doublecircle];']
    for e in edges:
        sig = "+".join(s for s in (x.signal for x in e.evidences)
                       if not s.startswith("__")) or "linked"
        lines.append(f'  "{_dot_label(e.via)}" -> "{_dot_label(e.target)}" '
                     f'[label="{e.confidence:g}% {sig}"];')
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
<title>llunascope — {company_html}</title>
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
Signals: san_overlap, org_tuple, asn_match, rdap_org. Weights published in README.</p>
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
        # The mermaid source is embedded raw in the HTML body: escape it so a
        # hostile company/seed string cannot break out of the <div> (mermaid
        # reads the div's text content, so entities decode back harmlessly).
        mermaid=html.escape(to_mermaid(seed_label, edges)),
        rows="\n".join(rows), n_nodes=len(nodes), n_edges=len(edges))
