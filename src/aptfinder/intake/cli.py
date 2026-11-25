"""aptf-intake: run a source over intake areas (config/areas.toml) and keep a done/left ledger.

  aptf-intake coverage                         # what's done and what's left, per source and area
  aptf-intake run zillow                       # every not-yet-covered area of the source's kind, one batched run
  aptf-intake run zillow --area 94301 --area 94025 [--reload]
  aptf-intake reparse <source> <raw file> [--covers complete|partial|auto] [--changed-only]   # re-ingest saved output (free)
  aptf-intake discover-sites [--limit 5] [--dry-run]   # find websites of buildings in a station walk zone
  aptf-intake promote-sites [--limit 100] [--retry-failed] [--dry-run]
  aptf-intake render [--site ID] [--retry-failed] [--dry-run]   # every site in the list by default
  aptf-intake render-restriction [--site ID] [--parking] [--dry-run]   # who may rent (or parking), re-read from rendered pages (no Apify)
  aptf-intake find-plans [--place KEY] [--name NAME] [--limit N]   # own-site list: search + click agent (pilot)
  aptf-intake prometheus [--site ID] [--dry-run]   # Prometheus sites: one availability-JSON request each, free
  aptf-intake precheck [--site ID] [--dry-run]   # free: DNS + one plain GET per site; records dead / challenge
  aptf-intake extract <text source>|all [--workers 8] [--dry-run]   # Haiku reads each listing's whole entry
  aptf-intake zillow-detail --max-usd 1.0 [--batch 150] [--limit N] [--dry-run]   # Zillow detail pages (raw only)
  aptf-intake zillow-buildings --max-usd 0.5 [--limit N] [--dry-run]   # Zillow building floor plans (raw only)
  (`run` of a text source then reads its rows automatically, `--no-extract` skips; `reparse` applies cached readings)
  (then, after any `run` or `reparse` that wrote rows: geocode new addresses (not on reparse), identity, free commute
   estimates for places without any; `--no-followup` skips)
"""

from __future__ import annotations

import argparse
import json

from .. import settings
from ..config import load_areas
from ..store import connect, init_db
from . import ADAPTERS
from .base import coverage, done, reparse, run_source, skipped
from .extract import TEXT_SOURCES

SHORT = {"direct": "direct", "zillow": "zillow", "apartments_com": "apts.com", "craigslist": "craigsl", "furnishedfinder": "ff",
         "airbnb": "airbnb", "google_places": "g.places", "zumper": "zumper", "padmapper": "padmapper",
         "portfolio": "portfolio", "rentcafe": "rentcafe"}


def print_coverage(conn) -> None:
    areas = load_areas()
    cov = coverage(conn)
    left = n_done = 0
    for kind in ("zip", "city", "box", "site"):
        srcs = [s for s, a in ADAPTERS.items() if a.area_kind == kind and not skipped(s)]
        mine = [a for a in areas if a.kind == kind]
        if not srcs or not mine:
            continue
        print(f"\n=== {kind}")
        print(f"  {'':24s}" + "".join(f"{SHORT[s]:>13s}" for s in srcs))
        for a in mine:
            cells = []
            for s in srcs:
                c = cov.get((s, a.id))
                if c and c["state"] in ("complete", "empty"):
                    n_done += 1
                    tag = "empty" if c["state"] == "empty" else c["date"][5:]
                    cells.append(f"{tag}{'*' if c['legacy'] else ''} r{c['run_id']}")
                elif c:
                    left += 1
                    cells.append(f"PARTIAL r{c['run_id']}")
                else:
                    left += 1
                    cells.append("TODO")
            print(f"  {a.label[:24]:24s}" + "".join(f"{x:>13s}" for x in cells))
    print(f"\n{n_done} area-source pairs done, {left} left (TODO or PARTIAL).  * = earlier whole-city / box "
          "query; PARTIAL = cap hit or actor failed; the next `run` picks TODO and PARTIAL areas up again.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aptf-intake")
    ap.add_argument("--db", default=str(settings.DB_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("coverage", help="done / left per source and area")
    sub.add_parser("credit", help="the Apify credit guard's numbers now (live, no actor run): credit left, reserve, "
                                  "room for the next run")
    r = sub.add_parser("run")
    r.add_argument("source", choices=sorted(ADAPTERS))
    r.add_argument("--area", action="append", default=[],
                   help="area id (ZIP, city name, box id or site id); default every area of the source's kind")
    r.add_argument("--reload", action="store_true", help="include areas already covered")
    r.add_argument("--max-items", type=int)
    r.add_argument("--max-usd", type=float)
    r.add_argument("--dry-run", action="store_true", help="show which areas would run, spend nothing")
    r.add_argument("--no-extract", action="store_true", help="do not read the new rows' text with Haiku afterwards")
    r.add_argument("--no-followup", action="store_true",
                   help="do not geocode new addresses, rebuild identity or estimate commutes afterwards")
    r.add_argument("--workers", type=int, default=8, help="parallel Claude calls for the text reading")
    r.add_argument("--extract-max-usd", type=float, default=1.0, help="cap on Claude spend for the text reading")
    rp = sub.add_parser("reparse", help="re-ingest a saved raw file with the current parser (no new scrape)")
    rp.add_argument("source", choices=sorted(ADAPTERS))
    rp.add_argument("raw_file")
    rp.add_argument("--covers", choices=["complete", "partial", "auto"],
                    help="credit the file's areas in the coverage ledger (a trial scraped on another copy); auto: "
                         "each area's state from the file (portfolio: only a city with a failed page is partial)")
    rp.add_argument("--changed-only", action="store_true",
                    help="append only active listings whose latest row came from this file and whose parse changed "
                         "(a parser that keeps a new field); retires, supersedes and resurrects nothing")
    rp.add_argument("--no-followup", action="store_true", help="do not rebuild identity or estimate commutes afterwards")
    ds = sub.add_parser("discover-sites", help="find community websites with a local headless Claude (web search "
                                               "only), then fetch and address-check them through Apify")
    ds.add_argument("--limit", type=int, default=10)
    ds.add_argument("--batch", type=int, default=5, help="buildings per Claude call")
    ds.add_argument("--model", default="sonnet")
    ds.add_argument("--max-usd", type=float, default=0.5, help="cap on Claude spend for this command")
    ds.add_argument("--no-fetch", action="store_true", help="record proposals only; no Apify fetch")
    ds.add_argument("--dry-run", action="store_true", help="list the buildings that would be searched")
    ps = sub.add_parser("promote-sites", help="turn websites that listings already name into community sites: "
                                              "fetch through Apify, keep the ones showing the address, scrape them")
    ps.add_argument("--limit", type=int, default=100)
    ps.add_argument("--no-fetch", action="store_true")
    ps.add_argument("--retry-failed", action="store_true", help="only fetch again found sites whose page was not "
                                                               "fetched last time (blocked, timed out, empty)")
    ps.add_argument("--browser", action="store_true", help="check pages in a real browser (sites that refuse the "
                                                          "cheap fetch); one page per site")
    ps.add_argument("--residential", action="store_true", help="--browser through Apify's residential proxy")
    ps.add_argument("--dry-run", action="store_true")
    ex = sub.add_parser("extract", help="read each listing's whole entry with Claude Haiku (address, name, website, "
                                        "amenities, laundry, parking, pets, fees, furnished, private bath, ...)")
    ex.add_argument("source", choices=[*TEXT_SOURCES, "all"])
    ex.add_argument("--limit", type=int, default=100000)
    ex.add_argument("--workers", type=int, default=8, help="parallel Claude calls")
    ex.add_argument("--batch", type=int, default=10, help="listings per Claude call")
    ex.add_argument("--model", default="haiku")
    ex.add_argument("--max-usd", type=float, default=1.0, help="cap on Claude spend for this command")
    ex.add_argument("--dry-run", action="store_true")
    ex.add_argument("--reread", action="store_true", help="apply cached answers again to listings already read "
                                                          "(free; after a change to the checks)")
    ex.add_argument("--recheck-restriction", metavar="VALUE", help="only listings read as this restriction (e.g. "
                    "income): apply cached answers again (free), after a stricter check")
    ex.add_argument("--recheck-none", action="store_true", help="only listings read as restriction 'none' from a "
                                                                "name or title alone: apply cached answers again "
                                                                "(free), so they become unknown")
    ex.add_argument("--units-missing", action="store_true", help="only rows whose source left beds or baths empty "
                                                               "(not rooms, building info or Zillow building rows): "
                                                               "read again; a Zillow single unit gets its page's "
                                                               "beds, baths, sqft and price")
    ex.add_argument("--id-like", help="only listings whose source_id matches this SQL LIKE pattern "
                                      "(e.g. '%%:%%': Zillow buildings' bedroom rows)")
    zd = sub.add_parser("zillow-detail", help="fetch the detail page of every active Zillow listing without one "
                                              "(Apify; raw only, read by `extract zillow`)")
    zd.add_argument("--max-usd", type=float, required=True, help="cap on Apify spend for this command")
    zd.add_argument("--batch", type=int, default=150, help="zpids per actor run")
    zd.add_argument("--limit", type=int, help="at most this many zpids")
    zd.add_argument("--refetch", action="store_true", help="also zpids that already have a page")
    zd.add_argument("--dry-run", action="store_true")
    zb = sub.add_parser("zillow-buildings", help="fetch the building page (every floor plan: beds, baths, sqft, "
                                                 "price, units) of every building with active Zillow bedroom rows "
                                                 "(Apify; raw only, applied by `extract zillow`)")
    zb.add_argument("--max-usd", type=float, required=True, help="cap on Apify spend for this command")
    zb.add_argument("--batch", type=int, default=150, help="buildings per actor run")
    zb.add_argument("--limit", type=int, help="at most this many buildings")
    zb.add_argument("--refetch", action="store_true", help="also buildings that already have a page")
    zb.add_argument("--dry-run", action="store_true")
    rd = sub.add_parser("render", help="render buildings' own sites in a browser (Apify) and read offered plans "
                                       "and prices with Claude Haiku")
    rd.add_argument("--site", action="append", default=[], help="site id (repeatable); default every site")
    rd.add_argument("--model", default="haiku")
    rd.add_argument("--max-usd", type=float, default=1.0, help="cap for each of Apify and Claude")
    rd.add_argument("--retry-failed", action="store_true", help="only sites whose last render failed (blocked, "
                                                               "timed out, empty, not returned)")
    rd.add_argument("--residential", action="store_true", help="through Apify's residential proxy (sites that block "
                                                              "the datacenter one; billed by traffic, images skipped)")
    rd.add_argument("--dry-run", action="store_true")
    rr = sub.add_parser("render-restriction", help="read who may rent (income, senior, ...) again from the raw page "
                                                   "text of sites already rendered (Claude Haiku, no Apify)")
    rr.add_argument("--site", action="append", default=[], help="site id (repeatable); default every rendered site")
    rr.add_argument("--model", default="haiku")
    rr.add_argument("--max-usd", type=float, default=1.0, help="cap on Claude spend")
    rr.add_argument("--workers", type=int, default=4, help="parallel Claude calls")
    rr.add_argument("--dry-run", action="store_true")
    rr.add_argument("--parking", action="store_true", help="read the community's parking instead (extra.page_parking)")
    fp = sub.add_parser("find-plans", help="buildings on Verify's own-site list: web search + a click agent find "
                                           "the floor-plans page, Haiku reads it (pilot: report only, stores nothing)")
    fp.add_argument("--place", action="append", default=[], help="place key (repeatable)")
    fp.add_argument("--name", action="append", default=[], help="building name as listed (repeatable)")
    fp.add_argument("--limit", type=int, help="the first N items of the own-site list")
    fp.add_argument("--max-apify-usd", type=float, default=0.6)
    fp.add_argument("--max-claude-usd", type=float, default=2.0)
    fp.add_argument("--no-search", action="store_true", help="no web search: the click agent from the known site")
    fp.add_argument("--google-only", action="store_true", help="search with Google only (no Claude web search)")
    fp.add_argument("--reason", action="append", default=[], help="only own-site items with this reason "
                                                                  "(no_website, no_plans, ...; repeatable)")
    fp.add_argument("--store", action="store_true", help="store priced readings like render (plan rows, sites)")
    fp.add_argument("--shots", action="store_true", help="screenshots + Haiku vision when the text has no price")
    fp.add_argument("--dry-run", action="store_true", help="print the targets and start URLs only")
    pm = sub.add_parser("prometheus", help="Prometheus own sites: units and prices from Prometheus's availability "
                                           "JSON, one direct request per site (no Apify, no page scraping)")
    pm.add_argument("--site", action="append", default=[], help="site id (repeatable); default every Prometheus site")
    pm.add_argument("--dry-run", action="store_true")
    pc = sub.add_parser("precheck", help="free reachability check of every own site from this machine (DNS, one "
                                         "plain GET): record dead domains and Cloudflare challenges, no Apify")
    pc.add_argument("--site", action="append", default=[], help="site id (repeatable); default every site")
    pc.add_argument("--dry-run", action="store_true", help="check and print, record nothing")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    init_db(conn)
    if args.cmd == "coverage":
        print_coverage(conn)
        return
    if args.cmd == "credit":
        from .apify import credit

        c = credit()
        room = None if c["left"] is None else round(c["left"] - c["reserve"], 4)
        print(json.dumps({**c, "room": room, "guard": "refuses" if room is None or room < 0.02 else "allows"}))
        return
    if args.cmd == "reparse":
        res = reparse(conn, ADAPTERS[args.source], args.raw_file, args.covers, rebuild_identity=False,
                      changed_only=args.changed_only)
        print(json.dumps(res, indent=1))
        from .extract import after_intake  # the new rows hide earlier readings: apply cached ones (free)

        ex = after_intake(conn, args.source, res["observations"], cached_only=True, followup=not args.no_followup,
                          geocode=False, db_path=args.db)
        if ex:
            print(json.dumps({"extract (cached)": ex}, indent=1, default=str))
        return

    if args.cmd == "render":
        from .discover import Blocker
        from .render import render

        try:
            res = render(conn, sites=args.site or None, model=args.model, max_claude_usd=args.max_usd,
                         max_apify_usd=args.max_usd, dry_run=args.dry_run, retry_failed=args.retry_failed,
                         residential=args.residential)
        except Blocker as e:
            raise SystemExit(f"BLOCKED: {e}")
        print(json.dumps(res, indent=1, default=str))
        return

    if args.cmd == "find-plans":
        from .discover import Blocker
        from .findplans import pilot, targets

        ts = targets(conn, places=args.place, names=args.name, limit=args.limit, reasons=args.reason)
        if args.dry_run:
            print(json.dumps([{**t, "own_hosts": sorted(t["own_hosts"])} for t in ts], indent=1))
            return
        try:
            res = pilot(ts, max_apify_usd=args.max_apify_usd, max_claude_usd=args.max_claude_usd,
                    search="none" if args.no_search else "google" if args.google_only else "both",
                    shots=args.shots, conn=conn if args.store else None)
        except Blocker as e:
            raise SystemExit(f"BLOCKED: {e}")
        print(json.dumps({k: res[k] for k in ("raw", "apify_usd", "claude_usd", "pages", "found", "stored")},
                         indent=1, default=str))
        return

    if args.cmd == "render-restriction":
        from .render import reread_restrictions

        res = reread_restrictions(conn, sites=args.site or None, model=args.model, max_usd=args.max_usd,
                                  workers=args.workers, dry_run=args.dry_run, parking=args.parking)
        print(json.dumps(res, indent=1, default=str))
        if res.get("error"):
            raise SystemExit(f"BLOCKED: render-restriction {res['error']} (sites read before it are stored)")
        return

    if args.cmd == "precheck":
        from .precheck import run as precheck_run

        print(json.dumps(precheck_run(conn, sites=args.site or None, dry_run=args.dry_run), indent=1, default=str))
        return

    if args.cmd == "prometheus":
        from .prometheus import read

        print(json.dumps(read(conn, sites=args.site or None, dry_run=args.dry_run), indent=1, default=str))
        return

    if args.cmd == "zillow-buildings":
        from .zillow_building import fetch as fetch_buildings

        res = fetch_buildings(conn, max_usd=args.max_usd, batch=args.batch, limit=args.limit, refetch=args.refetch,
                              dry_run=args.dry_run)
        print(json.dumps(res, indent=1, default=str))
        if res.get("warnings"):
            print("\nWARNINGS:\n  " + "\n  ".join(res["warnings"]))
        if res.get("fetched"):
            print("\napply to the bedroom rows (free, cached answers): aptf-intake extract zillow --reread "
                  "--id-like '%:%'")
        return

    if args.cmd == "zillow-detail":
        from .zillow_detail import fetch

        res = fetch(conn, max_usd=args.max_usd, batch=args.batch, limit=args.limit, refetch=args.refetch,
                    dry_run=args.dry_run)
        print(json.dumps(res, indent=1, default=str))
        if res.get("warnings"):
            print("\nWARNINGS:\n  " + "\n  ".join(res["warnings"]))
        return

    if args.cmd == "extract":
        from .discover import Blocker
        from .extract import extract

        left = args.max_usd
        for src in (TEXT_SOURCES if args.source == "all" else [args.source]):
            try:
                res = extract(conn, src, limit=args.limit, batch=args.batch, model=args.model, max_usd=left,
                              workers=args.workers, dry_run=args.dry_run, reread=args.reread,
                              id_like=args.id_like, recheck_none=args.recheck_none, recheck=args.recheck_restriction,
                              units_missing=args.units_missing)
            except Blocker as e:
                raise SystemExit(f"BLOCKED: extract {e}")
            print(json.dumps(res, indent=1, default=str), flush=True)
            left -= res.get("claude_usd", 0)
            if left <= 0:
                raise SystemExit(f"stopped: --max-usd {args.max_usd} spent; run again to continue")
        return

    if args.cmd == "promote-sites":
        from .discover import Blocker, promote

        try:
            res = promote(conn, limit=args.limit, dry_run=args.dry_run, fetch=not args.no_fetch,
                          retry_failed=args.retry_failed, browser=args.browser, residential=args.residential)
        except Blocker as e:
            raise SystemExit(f"BLOCKED: {e}")
        print(json.dumps(res, indent=1, default=str))
        return

    if args.cmd == "discover-sites":
        from .discover import Blocker, discover

        try:
            res = discover(conn, limit=args.limit, batch=args.batch, model=args.model,
                           max_usd=args.max_usd, dry_run=args.dry_run, fetch=not args.no_fetch)
        except Blocker as e:
            raise SystemExit(f"BLOCKED: {e}")
        print(json.dumps(res, indent=1))
        return

    if skipped(args.source):
        raise SystemExit(f"{args.source} is skipped (skip = true in config/sources.toml); set it to false to run it")
    adapter = ADAPTERS[args.source]
    areas = load_areas()
    pick = [a for a in areas if a.kind == adapter.area_kind and (not args.area or a.id in args.area)]
    if not args.reload:
        cov = coverage(conn)
        pick = [a for a in pick if not done(cov, args.source, a.id)]
    if not pick:
        raise SystemExit("nothing to run: no matching areas, or all already covered (use --reload)")
    print(f"{args.source}: {len(pick)} areas: {', '.join(a.id for a in pick)}")
    if args.dry_run:
        return
    # identity: rebuilt once, by the after-intake hook below (after_steps)
    res = run_source(conn, adapter, pick, max_items=args.max_items, max_usd=args.max_usd, rebuild_identity=False)
    print(json.dumps({"run_id": res.run_id, "status": res.status, "cost_usd": res.cost_usd, **res.counts},
                     indent=1))
    if res.warnings:
        print("\nWARNINGS (check before relying on this run):\n  " + "\n  ".join(res.warnings))
    # core pipeline: the run's rows supersede earlier readings, so read them (unchanged text is cached, free)
    from .discover import Blocker
    from .extract import after_intake

    try:
        ex = after_intake(conn, args.source, res.counts.get("observations", 0), skip=args.no_extract,
                          followup=not args.no_followup, db_path=args.db, workers=args.workers,
                          max_usd=args.extract_max_usd)
    except Blocker as e:
        raise SystemExit(f"BLOCKED: extract {e} (the intake run above is stored)")
    if ex:
        print(json.dumps({"extract": ex}, indent=1, default=str))


if __name__ == "__main__":
    main()
