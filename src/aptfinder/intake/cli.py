"""aptf-intake: run a source over intake areas (config/areas.toml) and keep a done/left ledger.

  aptf-intake coverage                         # what's done and what's left, per chunk, area and source
  aptf-intake run zillow --chunk palo_alto     # every not-yet-covered area of that chunk, one batched run
  aptf-intake run zillow --area 94301 --area 94025 [--reload]
  aptf-intake reparse <source> <raw file>      # re-ingest saved output with the current parser (free)
"""

from __future__ import annotations

import argparse
import json

from .. import settings
from ..config import load_areas
from ..store import connect, init_db
from . import ADAPTERS
from .base import coverage, done, reparse, run_source

SHORT = {"direct": "direct", "zillow": "zillow", "apartments_com": "apts.com", "craigslist": "craigsl", "furnishedfinder": "ff",
         "airbnb": "airbnb"}


def print_coverage(conn) -> None:
    chunks, areas = load_areas()
    cov = coverage(conn)
    kinds = {"zip": [s for s, a in ADAPTERS.items() if a.area_kind == "zip"],
             "city": [s for s, a in ADAPTERS.items() if a.area_kind == "city"],
             "box": [s for s, a in ADAPTERS.items() if a.area_kind == "box"],
             "site": [s for s, a in ADAPTERS.items() if a.area_kind == "site"]}
    left = n_done = 0
    for chunk in chunks:
        print(f"\n=== chunk {chunk}")
        for kind, srcs in kinds.items():
            mine = [a for a in areas if a.kind == kind and chunk in a.chunks and a.chunks[0] == chunk]
            if not mine:
                continue
            print(f"  {kind:5s} {'':24s}" + "".join(f"{SHORT[s]:>13s}" for s in srcs))
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
                print(f"  {'':5s} {a.label[:24]:24s}" + "".join(f"{x:>13s}" for x in cells))
    print(f"\n{n_done} area-source pairs done, {left} left (TODO or PARTIAL).  * = earlier whole-city / box "
          "query; PARTIAL = cap hit or actor failed, rerun with --reload or a higher --max-items.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aptf-intake")
    ap.add_argument("--db", default=str(settings.DB_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("coverage", help="done / left per chunk, area and source")
    r = sub.add_parser("run")
    r.add_argument("source", choices=sorted(ADAPTERS))
    r.add_argument("--chunk", action="append", default=[])
    r.add_argument("--area", action="append", default=[], help="area id (ZIP, city name, or box id)")
    r.add_argument("--reload", action="store_true", help="include areas already covered")
    r.add_argument("--max-items", type=int)
    r.add_argument("--max-usd", type=float)
    r.add_argument("--dry-run", action="store_true", help="show which areas would run, spend nothing")
    rp = sub.add_parser("reparse", help="re-ingest a saved raw file with the current parser (no new scrape)")
    rp.add_argument("source", choices=sorted(ADAPTERS))
    rp.add_argument("raw_file")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    init_db(conn)
    if args.cmd == "coverage":
        print_coverage(conn)
        return
    if args.cmd == "reparse":
        print(json.dumps(reparse(conn, ADAPTERS[args.source], args.raw_file), indent=1))
        return

    adapter = ADAPTERS[args.source]
    _, areas = load_areas()
    pick = [a for a in areas if a.kind == adapter.area_kind
            and (a.id in args.area or any(c in a.chunks for c in args.chunk))]
    if not args.reload:
        cov = coverage(conn)
        pick = [a for a in pick if not done(cov, args.source, a.id)]
    if not pick:
        raise SystemExit("nothing to run: no matching areas, or all already covered (use --reload)")
    print(f"{args.source}: {len(pick)} areas: {', '.join(a.id for a in pick)}")
    if args.dry_run:
        return
    res = run_source(conn, adapter, pick, max_items=args.max_items, max_usd=args.max_usd)
    print(json.dumps({"run_id": res.run_id, "status": res.status, "cost_usd": res.cost_usd, **res.counts},
                     indent=1))
    if res.warnings:
        print("\nWARNINGS (check before relying on this run):\n  " + "\n  ".join(res.warnings))


if __name__ == "__main__":
    main()
