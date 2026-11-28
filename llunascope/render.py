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


_TERM_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


def _term_safe(s) -> str:
    """Strip control chars so data from public APIs (RDAP org names, cert
    subjects) or hand-edited snapshots can't inject terminal escapes into
    the tree output. All C0 controls are stripped, including newline and
    carriage return: a ``\\n`` inside a field value would forge fake log
    lines just as surely as an ESC sequence forges colours."""
    return _TERM_UNSAFE.sub("", s if isinstance(s, str) else str(s))


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
        return f"{_term_safe(e.target)}  [{e.confidence:g}%]"

    if RICH and out.isatty():
        console = Console(file=out)
        tree = Tree(f"[bold magenta]\U0001f319 {_term_safe(seed_label)}[/bold magenta]")
        seen: set[str] = set()

        def add(node: Tree, via: str, depth: int) -> None:
            if depth > 4:
                return
            for e in kids.get(via, []):
                if e.target in seen:
                    continue
                seen.add(e.target)
                branch = node.add(
                    f"[cyan]{rich_escape(_term_safe(e.target))}[/cyan]  [dim][{e.confidence:g}%][/dim]")
                if e.evidences:
                    ev = branch.add("[dim]evidence[/dim]")
                    for evd in e.evidences:
                        if evd.signal.startswith("__"):
                            continue
                        ev.add(f"[dim]{rich_escape(_term_safe(evd.signal))}: "
                               f"{rich_escape(_term_safe(evd.detail))}[/dim]")
                add(branch, e.target, depth + 1)

        for r in roots:
            add(tree, r, 0)
        console.print(tree)
        return

    # ASCII fallback.
    print(f"\U0001f319 {_term_safe(seed_label)}", file=out)
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
                print(f"{ev_prefix}\u00b7 {_term_safe(evd.signal)}: {_term_safe(evd.detail)}", file=out)
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


def _graph_payload(company: str, seed_label: str, edges: list[Edge],
                   nodes: set[str]) -> dict:
    """Cytoscape.js element set for the interactive HTML graph.

    The pivot records hop-1 parents as the seed domain itself (e.g.
    via='yahoo.com' — see test_pivot.py), NOT as 'SEED': the frontier
    carries 'SEED' only as a label, while Edge() is built with
    via=domain. Seed domains are not graph nodes (the SEED meta-node
    represents them), so a hop-1 edge's source must be rewritten to
    'SEED' here — otherwise Cytoscape.js refuses the whole graph
    ("Can not create edge ... with nonexistant source") and the report
    renders an empty canvas with no error message.
    """
    targets = {e.target for e in edges}
    seed_domains = sorted(n for n in nodes if n not in targets)
    max_conf: dict[str, float] = {}
    hop_of: dict[str, int] = {}
    for e in edges:
        max_conf[e.target] = max(max_conf.get(e.target, 0.0), e.confidence)
        hop_of[e.target] = min(hop_of.get(e.target, e.hop), e.hop)
    elements: list[dict] = [{
        "data": {"id": "SEED", "label": f"\U0001f319 {seed_label}",
                 "kind": "seed", "seed_domains": seed_domains},
    }]
    # Seed domains are NOT separate nodes: they have no graded edges of
    # their own (hop-1 edges hang off them, rewritten to the SEED
    # meta-node above), so standalone seed nodes would dangle
    # unconnected and vanish under the confidence filter. The SEED
    # meta-node represents them; its details panel lists them.
    for n in sorted(nodes):
        if n in seed_domains:
            continue
        elements.append({"data": {"id": n, "label": n, "kind": "domain",
                                  "hop": hop_of.get(n, 0),
                                  "confidence": max_conf.get(n, 0.0)}})
    for i, e in enumerate(sorted(edges, key=lambda x: -x.confidence)):
        elements.append({"data": {
            "id": f"e{i}", "source": "SEED" if e.via in seed_domains else e.via,
            "target": e.target,
            "confidence": e.confidence, "hop": e.hop,
            "penalized": e.penalized,
            "evidence": [{"signal": x.signal, "detail": x.detail,
                          "weight": x.weight}
                         for x in e.evidences
                         if not x.signal.startswith("__")],
        }})
    return {"company": company, "seed_label": seed_label,
            "seed_domains": seed_domains, "elements": elements}


def _safe_json_for_script(payload: dict) -> str:
    """Serialize a payload for a <script type=\"application/json\"> block.

    The only breakout from such a block is a literal `</script` (or an
    HTML comment opener in exotic parsers), so every `<`, `>` and `&` is
    Unicode-escaped — JSON.parse decodes them back exactly. A hostile
    company/seed string can never close the block or inject markup.
    """
    raw = json.dumps(payload, ensure_ascii=False)
    return (raw.replace("&", "\\u0026").replace("<", "\\u003c")
               .replace(">", "\\u003e"))


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>llunascope — %%COMPANY_HTML%%</title>
<script src="https://cdn.jsdelivr.net/npm/cytoscape@3.30.4/dist/cytoscape.min.js"></script>
<style>
  body { background:#0d1117; color:#c9d1d9; font-family:system-ui,sans-serif; margin:0; padding:2rem; }
  h1 { color:#e6edf3; } h1 .moon { color:#a371f7; }
  .tag { color:#8b949e; font-style:italic; margin-top:-0.8rem; }
  #cy-wrap { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:1rem; }
  #cy { height:560px; width:100%; background:#0d1117; border:1px solid #30363d; border-radius:6px; }
  #toolbar { display:flex; flex-wrap:wrap; gap:0.8rem 1.2rem; align-items:center; margin-bottom:0.8rem; font-size:0.9rem; }
  #toolbar label { color:#8b949e; }
  #toolbar select, #toolbar input[type="text"] { background:#0d1117; color:#e6edf3; border:1px solid #30363d; border-radius:6px; padding:0.3rem 0.5rem; }
  #toolbar button { background:#21262d; color:#e6edf3; border:1px solid #30363d; border-radius:6px; padding:0.3rem 0.8rem; cursor:pointer; }
  #toolbar button:hover { background:#30363d; }
  #counts { color:#8b949e; }
  #details { background:#0d1117; border:1px solid #30363d; border-radius:6px; padding:0.8rem 1rem; margin-top:0.8rem; font-size:0.9rem; min-height:2.6rem; }
  #details h3 { margin:0 0 0.4rem; color:#e6edf3; font-size:1rem; }
  #details .muted { color:#8b949e; }
  #details ul { margin:0.3rem 0 0; padding-left:1.2rem; }
  #cy-fallback { display:none; color:#f85149; margin-top:0.8rem; }
  .legend { color:#8b949e; font-size:0.85rem; margin-top:0.6rem; }
  .legend b { font-weight:600; }
  .legend .hi { color:#3fb950; } .legend .mid { color:#d29922; } .legend .lo { color:#f85149; } .legend .seed { color:#a371f7; }
  table { border-collapse:collapse; width:100%; margin-top:1.5rem; }
  th,td { border:1px solid #30363d; padding:0.5rem 0.7rem; text-align:left; font-size:0.9rem; }
  th { background:#161b22; color:#e6edf3; }
  .conf { font-weight:bold; color:#7ee787; }
  .foot { color:#8b949e; font-size:0.8rem; margin-top:2rem; }
  code { background:#161b22; padding:0.15rem 0.4rem; border-radius:4px; }
</style></head><body>
<h1><span class="moon">\U0001f319</span> llunascope — %%COMPANY_HTML%%</h1>
<p class="tag">The moon never knocks. It just watches. — zero packets sent, zero API keys.</p>
<div id="cy-wrap">
  <div id="toolbar">
    <label>Layout
      <select id="layout-select">
        <option value="cose" selected>Force-directed (cose)</option>
        <option value="breadthfirst">Tree (breadthfirst)</option>
        <option value="concentric">Concentric</option>
        <option value="grid">Grid</option>
      </select>
    </label>
    <label>Min confidence
      <input id="conf-slider" type="range" min="0" max="99" value="0" step="1">
      <span id="conf-value">0%</span>
    </label>
    <label>Find domain
      <input id="search" type="text" placeholder="aol.com" size="18">
    </label>
    <button id="fit-btn" type="button">Fit</button>
    <button id="reset-btn" type="button">Reset view</button>
    <span id="counts"></span>
  </div>
  <div id="cy"></div>
  <p id="cy-fallback">Cytoscape.js failed to load (CDN unreachable?) — the graph is
  unavailable, but the evidence table below still lists every edge.</p>
  <div id="details"><span class="muted">Click a node or an edge to inspect its evidence chain.</span></div>
  <p class="legend"><b class="seed">◆ seed</b> &nbsp;·&nbsp; edge colour by confidence:
  <b class="hi">≥70% strong</b>, <b class="mid">40–69% moderate</b>,
  <b class="lo">&lt;40% weak</b> &nbsp;·&nbsp; drag to move, scroll to zoom, click to inspect.</p>
</div>
<script type="application/json" id="llunascope-graph">%%GRAPH_JSON%%</script>
<script>
(function () {
  var fallback = document.getElementById('cy-fallback');
  if (typeof cytoscape === 'undefined') { fallback.style.display = 'block'; return; }
  var payload = JSON.parse(document.getElementById('llunascope-graph').textContent);
  var details = document.getElementById('details');
  var counts = document.getElementById('counts');
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function band(conf) { return conf >= 70 ? 'hi' : (conf >= 40 ? 'mid' : 'lo'); }
  function evList(evs) {
    if (!evs || !evs.length) return '<p class="muted">No evidence recorded.</p>';
    var items = evs.map(function (e) {
      return '<li><code>' + esc(e.signal) + '</code> ' + esc(e.detail) + '</li>';
    }).join('');
    return '<ul>' + items + '</ul>';
  }
  var cy;
  try {
    cy = cytoscape({
    container: document.getElementById('cy'),
    elements: payload.elements,
    style: [
      { selector: 'node', style: {
          'label': 'data(label)', 'font-size': 11, 'color': '#e6edf3',
          'text-valign': 'center', 'text-halign': 'center',
          'background-color': '#58a6ff', 'width': 26, 'height': 26,
          'text-outline-color': '#0d1117', 'text-outline-width': 2 } },
      { selector: 'node[kind="seed"]', style: {
          'background-color': '#a371f7', 'shape': 'star',
          'width': 40, 'height': 40, 'font-size': 13, 'font-weight': 'bold' } },
      { selector: 'node.conf-hi', style: { 'background-color': '#3fb950' } },
      { selector: 'node.conf-mid', style: { 'background-color': '#d29922' } },
      { selector: 'node.conf-lo', style: { 'background-color': '#f85149' } },
      { selector: 'node:selected', style: { 'border-width': 3, 'border-color': '#e6edf3' } },
      { selector: 'node.highlight', style: { 'border-width': 4, 'border-color': '#f0f6fc' } },
      { selector: 'edge', style: {
          'label': 'data(confidence)', 'font-size': 10, 'color': '#8b949e',
          'text-background-color': '#0d1117', 'text-background-opacity': 1,
          'text-background-padding': 2, 'curve-style': 'bezier',
          'target-arrow-shape': 'triangle', 'width': 2, 'line-color': '#8b949e',
          'target-arrow-color': '#8b949e' } },
      { selector: 'edge.conf-hi', style: { 'line-color': '#3fb950', 'target-arrow-color': '#3fb950', 'width': 4 } },
      { selector: 'edge.conf-mid', style: { 'line-color': '#d29922', 'target-arrow-color': '#d29922', 'width': 3 } },
      { selector: 'edge.conf-lo', style: { 'line-color': '#f85149', 'target-arrow-color': '#f85149', 'width': 2 } },
      { selector: 'edge:selected', style: { 'line-color': '#e6edf3', 'target-arrow-color': '#e6edf3', 'width': 5 } }
    ],
    layout: { name: 'cose', animate: true, randomize: false },
    wheelSensitivity: 0.3
    });
  } catch (err) {
    // A bad payload (e.g. an edge pointing at a missing node) used to
    // kill the whole graph silently, leaving an empty canvas. Surface it.
    fallback.textContent = 'Graph failed to render (' + err.message
      + ') — the evidence table below still lists every edge.';
    fallback.style.display = 'block';
    return;
  }
  cy.edges().forEach(function (e) { e.addClass('conf-' + band(e.data('confidence'))); });
  cy.nodes('[kind="domain"]').forEach(function (n) { n.addClass('conf-' + band(n.data('confidence'))); });
  function updateCounts() {
    var visEdges = cy.edges(':visible').length;
    // Domain nodes only — the SEED meta-node is not a root domain, so it
    // is excluded to keep this count consistent with the report footer.
    var visNodes = cy.nodes('[kind="domain"]:visible').length;
    counts.textContent = visNodes + ' nodes · ' + visEdges + ' edges shown';
  }
  function showNode(node) {
    var d = node.data();
    var html = '<h3>' + esc(d.label) + '</h3>';
    if (d.kind === 'seed') {
      html += '<p>Seed root' + (d.seed_domains && d.seed_domains.length > 1 ? 's' : '') + ': '
        + d.seed_domains.map(esc).join(', ') + '</p>';
    } else {
      html += '<p>Hop ' + d.hop + ' · strongest edge confidence '
        + d.confidence + '%</p>';
    }
    var outs = node.outgoers('edge');
    if (outs.length) {
      html += '<p>Outgoing edges:</p><ul>' + outs.map(function (e) {
        return '<li>' + esc(e.data('target')) + ' — ' + e.data('confidence')
          + '% (' + e.data('evidence').map(function (x) { return esc(x.signal); }).join(' + ') + ')</li>';
      }).join('') + '</ul>';
    }
    var ins = node.incomers('edge');
    if (ins.length) {
      html += '<p>Incoming edge' + (ins.length > 1 ? 's' : '') + ':</p>' + ins.map(function (e) {
        return '<p><b>' + esc(e.data('source')) + ' → ' + esc(e.data('target'))
          + '</b> — ' + e.data('confidence') + '%'
          + (e.data('penalized') ? ' (shared-cert penalty ×0.2 applied)' : '')
          + evList(e.data('evidence')) + '</p>';
      }).join('');
    }
    details.innerHTML = html;
  }
  function showEdge(edge) {
    var d = edge.data();
    details.innerHTML = '<h3>' + esc(d.source) + ' → ' + esc(d.target) + '</h3>'
      + '<p>Confidence <b>' + d.confidence + '%</b> · hop ' + d.hop
      + (d.penalized ? ' · shared-cert penalty ×0.2 applied' : '') + '</p>'
      + evList(d.evidence);
  }
  cy.on('tap', 'node', function (evt) { showNode(evt.target); });
  cy.on('tap', 'edge', function (evt) { showEdge(evt.target); });
  cy.on('tap', function (evt) { if (evt.target === cy) {
    details.innerHTML = '<span class="muted">Click a node or an edge to inspect its evidence chain.</span>';
  } });
  document.getElementById('layout-select').addEventListener('change', function (evt) {
    var name = evt.target.value;
    var opts = { name: name, animate: true };
    if (name === 'breadthfirst') opts.directed = true;
    if (name === 'concentric') opts.minNodeSpacing = 40;
    cy.layout(opts).run();
  });
  var slider = document.getElementById('conf-slider');
  slider.addEventListener('input', function () {
    var min = Number(slider.value);
    document.getElementById('conf-value').textContent = min + '%';
    cy.edges().forEach(function (e) {
      if (e.data('confidence') < min) { e.hide(); } else { e.show(); }
    });
    cy.nodes().forEach(function (n) {
      if (n.data('kind') === 'seed') { n.show(); return; }
      var connected = n.connectedEdges(':visible');
      if (connected.length) { n.show(); } else { n.hide(); }
    });
    updateCounts();
  });
  document.getElementById('search').addEventListener('change', function (evt) {
    var q = evt.target.value.trim().toLowerCase();
    cy.nodes().removeClass('highlight');
    if (!q) return;
    var match = cy.nodes().filter(function (n) {
      return n.data('label').toLowerCase().indexOf(q) !== -1;
    });
    if (match.length) {
      match.addClass('highlight');
      cy.animate({ center: { eles: match.first() } }, { duration: 300 });
      showNode(match.first());
    }
  });
  document.getElementById('fit-btn').addEventListener('click', function () { cy.fit(); });
  document.getElementById('reset-btn').addEventListener('click', function () {
    slider.value = 0;
    document.getElementById('conf-value').textContent = '0%';
    document.getElementById('search').value = '';
    cy.elements().show();
    cy.nodes().removeClass('highlight');
    cy.layout({ name: 'cose', animate: true, randomize: false }).run();
    updateCounts();
  });
  updateCounts();
})();
</script>
<h2>Evidence chains</h2>
<table><tr><th>Target</th><th>Via</th><th>Confidence</th><th>Evidence</th></tr>
%%ROWS%%
</table>
<p class="foot">Generated by <code>llunascope</code> · passive EASM · %%N_NODES%% root domains, %%N_EDGES%% edges.
Signals: san_overlap, org_tuple, asn_match, rdap_org. Weights published in README.
Graph: <a href="https://js.cytoscape.org/" style="color:#8b949e">Cytoscape.js</a> (CDN); graph data is embedded in this file.</p>
</body></html>
"""


def to_html(company: str, seed_label: str, edges: list[Edge], nodes: set[str],
            org_identities: dict) -> str:
    """Interactive HTML report: Cytoscape.js graph + evidence table.

    The graph data rides in a <script type="application/json"> block,
    Unicode-escaped so hostile strings cannot break out of it. The
    evidence table below the graph is unchanged (printable, works even
    when the CDN is unreachable).
    """
    rows = []
    for e in sorted(edges, key=lambda x: -x.confidence):
        evs = "<br>".join(
            f"<code>{html.escape(x.signal)}</code> {html.escape(x.detail)}"
            for x in e.evidences if not x.signal.startswith("__"))
        rows.append(
            f"<tr><td><code>{html.escape(e.target)}</code></td>"
            f"<td><code>{html.escape(e.via)}</code></td>"
            f"<td class='conf'>{e.confidence:g}%</td><td>{evs}</td></tr>")
    graph_json = _safe_json_for_script(
        _graph_payload(company, seed_label, edges, nodes))
    # Single-pass substitution: sequential str.replace() rescans text it
    # already inserted, so a company/evidence string containing a token
    # (e.g. '%%ROWS%%') was rewritten by a later pass — the title filled
    # with table rows + graph JSON and the embedded JSON payload no longer
    # round-tripped the real strings. One regex pass never rescans.
    subs = {
        "%%COMPANY_HTML%%": html.escape(company),
        "%%GRAPH_JSON%%": graph_json,
        "%%ROWS%%": "\n".join(rows),
        "%%N_NODES%%": str(len(nodes)),
        "%%N_EDGES%%": str(len(edges)),
    }
    return re.sub(r"%%(COMPANY_HTML|GRAPH_JSON|ROWS|N_NODES|N_EDGES)%%",
                  lambda m: subs[m.group(0)], HTML_TEMPLATE)
