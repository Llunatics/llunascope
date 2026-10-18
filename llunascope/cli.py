"""llunascope CLI: from a company name to its shadow infrastructure."""

from __future__ import annotations

import argparse
import signal
import sys

from . import net, seed as seedmod
from .normalize import root_domain
from .pivot import pivot
from . import render
from .scoring import describe_weights

# Clean exit on broken pipes (e.g. `llunascope X --roots-only | head`).
try:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except Exception:
    pass


def _is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _disambiguate(entities: list[dict], name: str, non_interactive: bool):
    """Pick the right org when the name matches several. Returns chosen or None."""
    if not entities:
        return None
    if len(entities) == 1:
        return entities[0]
    if non_interactive or not _is_tty():
        best = max(entities, key=lambda e: e.get("sim", 0))
        print(f"[llunascope] ambiguous name {name!r}: auto-selected "
              f"'{best['label']}' ({best['qid']}) — highest label similarity. "
              f"Use --seed to override.", file=sys.stderr)
        return best
    print(f"\n[llunascope] {name!r} matches several organizations:", file=sys.stderr)
    for i, e in enumerate(entities, 1):
        print(f"  [{i}] {e['label']} ({e['qid']}, similarity {e['sim']})",
              file=sys.stderr)
    print(f"  [0] none of these — I'll rely on --seed only", file=sys.stderr)
    try:
        choice = input("  pick one [1]: ").strip() or "1"
        idx = int(choice)
        if idx == 0:
            return None
        return entities[idx - 1]
    except (ValueError, IndexError, EOFError, KeyboardInterrupt):
        print("\n[llunascope] aborting.", file=sys.stderr)
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="llunascope",
        description=("\U0001f319 llunascope — the moon never knocks, it just watches. "
                     "From a single company name to every forgotten subsidiary and "
                     "shadow root domain. Zero packets sent. Zero API keys."),
    )
    p.add_argument("company", nargs="?", help="Company name, e.g. \"Bank Mandiri\"")
    p.add_argument("--seed", default="",
                   help="Comma-separated known root domains (escape hatch, skips name resolution)")
    p.add_argument("--non-interactive", action="store_true",
                   help="Never prompt; auto-resolve ambiguity (for pipes/scripts)")
    p.add_argument("--roots-only", action="store_true",
                   help="Print only discovered root domains, one per line (pipe to subfinder)")
    p.add_argument("--format", choices=["tree", "mermaid", "dot", "json", "html"],
                   default="tree", help="Output format (default: tree)")
    p.add_argument("--weights", action="store_true", help="Show scoring weights and exit")
    p.add_argument("--verbose", action="store_true", help="Log pivot progress to stderr")
    p.add_argument("--version", action="store_true", help="Show version and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        from . import __version__
        print(f"llunascope {__version__}")
        return 0
    if args.weights:
        print(describe_weights())
        return 0
    if not args.company and not args.seed:
        build_parser().print_help(sys.stderr)
        return 2

    company = args.company or "(seeded)"
    seed_domains = [s.strip() for s in args.seed.split(",") if s.strip()]

    if not net.check_online():
        print("[llunascope] no internet connection detected — llunascope queries "
              "public APIs (RIPE Stat, Wikidata, crt.sh) and needs connectivity. "
              "It never contacts target infrastructure.", file=sys.stderr)
        return 3

    verbose = (lambda m: print(f"[llunascope] {m}", file=sys.stderr)) if args.verbose else None

    # ---- Hop 0: seed resolution -------------------------------------
    if seed_domains:
        seeds = seed_domains
        if verbose:
            verbose(f"using --seed domains: {', '.join(seeds)}")
    else:
        domains, _others, entities = seedmod.resolve(company)
        chosen = _disambiguate(entities, company, args.non_interactive)
        if chosen is None and entities and len(entities) > 1:
            # User explicitly rejected all candidates (picked "0").
            seeds = []
        elif chosen:
            # Re-run wikidata restricted to the chosen entity for precision.
            qid = chosen["qid"]
            claims = seedmod._wikidata_claims(qid)
            picked = []
            for site in claims.get("websites", []):
                rd = root_domain(site.split("//")[-1].split("/")[0])
                if rd:
                    picked.append(rd)
            if picked:
                seeds = picked
                if verbose:
                    verbose(f"disambiguated to {chosen['label']} -> {', '.join(picked)}")
            else:
                seeds = [c.value for c in domains]
        else:
            seeds = [c.value for c in domains]
        if not seeds:
            print(f"[llunascope] could not resolve {company!r} to any root domain.\n"
                  f"  Try: llunascope --seed example.com,example.co.id \"{company}\"\n"
                  f"  (registry coverage varies by region — see README)",
                  file=sys.stderr)
            return 4

    seed_label = seeds[0] if len(seeds) == 1 else f"{len(seeds)} seeds"

    # ---- Pivot --------------------------------------------------------
    status = None
    if render.RICH and not args.roots_only and args.format == "tree" and _is_tty():
        from rich.console import Console
        status = render.Status("\U0001f319 sweeping the night sky…",
                               console=Console(file=sys.stderr))
        status.start()
    try:
        result = pivot(seeds, verbose_cb=verbose)
    finally:
        if status:
            status.stop()

    # ---- Output --------------------------------------------------------
    if args.roots_only:
        for n in sorted(result.nodes):
            print(n)
        return 0

    fmt = args.format
    if fmt == "tree":
        render.print_tree(seed_label, result.edges, seed_roots=seeds)
    elif fmt == "mermaid":
        print(render.to_mermaid(seed_label, result.edges))
    elif fmt == "dot":
        print(render.to_dot(seed_label, result.edges))
    elif fmt == "json":
        print(render.to_json(seed_label, company, result.edges,
                             result.nodes, result.org_identities))
    elif fmt == "html":
        print(render.to_html(company, seed_label, result.edges,
                             result.nodes, result.org_identities))

    if _is_tty() or args.verbose:
        print(f"\n[llunascope] {len(result.nodes)} root domains, "
              f"{len(result.edges)} edges. Zero packets sent to targets.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
