"""llunascope CLI: from a company name to its shadow infrastructure."""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from . import net, seed as seedmod, watch as watchmod
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
    p.add_argument("--watch", action="store_true",
                   help="Watch mode: diff this run against the saved snapshot "
                        "and alert on new shadow assets (exit 10 if any)")
    p.add_argument("--snapshot", default="",
                   help="Snapshot file for --watch (default: "
                        "~/.cache/llunascope/<company>.json)")
    p.add_argument("--state-dir", default="",
                   help="Directory holding per-company snapshots for --watch")
    p.add_argument("--radar", nargs=2, metavar=("SNAP-A", "SNAP-B"), default=None,
                   help="M&A radar: compare two snapshots and flag cert/ASN "
                        "convergence (exit 10 on new convergence)")
    p.add_argument("--radar-baseline", nargs=2, metavar=("BASE-A", "BASE-B"),
                   default=None,
                   help="Baseline snapshots for --radar (only convergence "
                        "new since the baseline is reported)")
    return p


def _run_radar(args) -> int:
    """Standalone radar run: two snapshots in, convergence report out."""
    snaps = []
    for path in args.radar:
        try:
            snap = watchmod.load_snapshot(path)
        except watchmod.WatchError as exc:
            print(f"[llunascope] {exc}", file=sys.stderr)
            return 2
        if snap is None:
            print(f"[llunascope] radar: snapshot file not found: {path}",
                  file=sys.stderr)
            return 2
        snaps.append(snap)
    bases: list = [None, None]
    if args.radar_baseline:
        for i, path in enumerate(args.radar_baseline):
            try:
                base = watchmod.load_snapshot(path)
            except watchmod.WatchError as exc:
                print(f"[llunascope] {exc}", file=sys.stderr)
                return 2
            if base is None:
                print(f"[llunascope] radar: baseline file not found: {path}",
                      file=sys.stderr)
                return 2
            bases[i] = base
    try:
        result = watchmod.detect_convergence(snaps[0], snaps[1],
                                             bases[0], bases[1])
    except watchmod.WatchError as exc:
        print(f"[llunascope] {exc}", file=sys.stderr)
        return 2
    print(watchmod.format_radar_report(result))
    return watchmod.WATCH_EXIT_ALERT if result.converged else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        from . import __version__
        print(f"llunascope {__version__}")
        return 0
    if args.weights:
        print(describe_weights())
        return 0
    if args.radar_baseline and not args.radar:
        print("[llunascope] --radar-baseline needs --radar.", file=sys.stderr)
        return 2
    if args.radar and args.watch:
        print("[llunascope] --radar and --watch are separate modes; "
              "use one at a time.", file=sys.stderr)
        return 2
    if (args.snapshot or args.state_dir) and not args.watch:
        print("[llunascope] --snapshot/--state-dir need --watch.",
              file=sys.stderr)
        return 2
    if args.radar:
        return _run_radar(args)
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
        # Normalize: --seed takes human input ("WWW.Acme.COM.",
        # "https://acme.com:8080/x?y=1"), never raw strings.
        # Drop anything that isn't a domain.
        seeds = []
        for s in seed_domains:
            host = s.split("//")[-1].split("/")[0].split("?")[0].split("#")[0]
            host = host.rsplit("@", 1)[-1]  # strip userinfo
            host = host.split(":")[0]       # strip port (fuzz round 2:
                                            # "…co.id:8080/x" jadi "co.id:8080")
            rd = root_domain(host)
            if rd and rd not in seeds:
                seeds.append(rd)
        if not seeds:
            print("[llunascope] --seed contained no valid domains.",
                  file=sys.stderr)
            return 2
        if verbose:
            verbose(f"using --seed domains: {', '.join(seeds)}")
    else:
        domains, hints, entities = seedmod.resolve(company)
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
        if hints and (verbose or _is_tty()):
            # Registry hints (Wikidata subsidiaries, EDGAR listings, PeeringDB
            # orgs) are leads, not seeds: print them so the user can follow up
            # with --seed instead of silently discarding them.
            print("[llunascope] registry hints (leads — try --seed):",
                  file=sys.stderr)
            for h in hints[:10]:
                print(f"  · {h.value} — {h.evidence}", file=sys.stderr)
        if not seeds:
            print(f"[llunascope] could not resolve {company!r} to any root domain.\n"
                  f"  Try: llunascope --seed example.com,example.co.id \"{company}\"\n"
                  f"  (registry coverage varies by region — see README)",
                  file=sys.stderr)
            return 4

    seed_label = seeds[0] if len(seeds) == 1 else f"{len(seeds)} seeds"

    # ---- Watch mode: load the previous snapshot before pivoting --------
    # Fail fast on a corrupt snapshot or a company mismatch, before the
    # (slow, networked) pivot runs. A missing file is a first run: the
    # snapshot is created from this run and nothing is diffed.
    watch_path = watch_old = watch_diff = None
    if args.watch:
        watch_path = (Path(args.snapshot) if args.snapshot
                      else watchmod.default_snapshot_path(company,
                                                          args.state_dir or None))
        try:
            watch_old = watchmod.load_snapshot(watch_path)
            if watch_old is not None:
                watchmod.check_snapshot_company(watch_old, company, watch_path)
        except watchmod.WatchError as exc:
            print(f"[llunascope] {exc}", file=sys.stderr)
            return 2

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

    # ---- Watch mode: snapshot + diff (before output) -------------------
    # Built right after the pivot so the diff is ready whatever the output
    # format is; the file itself is saved after rendering (below), so a
    # failed run never touches state.
    new_snap = None
    if args.watch:
        new_snap = watchmod.snapshot_from_result(company, seeds, result)
        if watch_old is not None:
            watch_diff = watchmod.diff_snapshots(watch_old, new_snap)

    # ---- Output --------------------------------------------------------
    if args.roots_only:
        for n in sorted(result.nodes):
            print(n)
    else:
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

    # ---- Watch mode: save, report, exit code ---------------------------
    # The snapshot is saved after the run, so a failed fetch never touches
    # state (same rule as puny-patrol's watchdog). A save failure is still
    # an error (exit 2): the next run would diff against a stale baseline.
    if args.watch:
        try:
            watchmod.save_snapshot(watch_path, new_snap)
        except watchmod.WatchError as exc:
            print(f"[llunascope] {exc}", file=sys.stderr)
            return 2
        if watch_old is None:
            print(f"[llunascope] watch: no previous snapshot — baseline "
                  f"saved to {watch_path} ({len(new_snap['nodes'])} root "
                  f"domains, {len(new_snap['edges'])} edges). Run again "
                  f"to diff.", file=sys.stderr)
            return 0
        print(watchmod.format_watch_report(
            watch_diff, watch_old.get("created_at"), watch_path),
            file=sys.stderr)
        return watchmod.WATCH_EXIT_ALERT if watch_diff.new_count else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
