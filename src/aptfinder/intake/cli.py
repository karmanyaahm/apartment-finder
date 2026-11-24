"""aptf-intake: run a source for some cities (staged rollout), then link identities and estimate trips.

  aptf-intake run airbnb --city "Mountain View"
  aptf-intake run airbnb --all-cities
"""

from __future__ import annotations

import argparse
import json

from .. import settings
from ..config import load_area
from ..store import connect, init_db
from . import ADAPTERS
from .base import run_source


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aptf-intake")
    ap.add_argument("--db", default=str(settings.DB_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("source", choices=sorted(ADAPTERS))
    r.add_argument("--city", action="append", default=[])
    r.add_argument("--all-cities", action="store_true")
    r.add_argument("--max-items", type=int)
    r.add_argument("--max-usd", type=float)
    r.add_argument("--no-trips", action="store_true", help="skip free straight-line trip estimates")
    rp = sub.add_parser("reparse", help="re-ingest a saved raw file with the current parser (no new scrape)")
    rp.add_argument("source", choices=sorted(ADAPTERS))
    rp.add_argument("raw_file")
    args = ap.parse_args(argv)

    if args.cmd == "reparse":
        from .base import reparse

        conn = connect(args.db)
        init_db(conn)
        print(json.dumps(reparse(conn, ADAPTERS[args.source], args.raw_file), indent=1))
        return

    cities = load_area().cities if args.all_cities else args.city
    if not cities:
        raise SystemExit("give --city (repeatable) or --all-cities")
    conn = connect(args.db)
    init_db(conn)
    res = run_source(conn, ADAPTERS[args.source], cities, max_items=args.max_items, max_usd=args.max_usd)
    print(json.dumps({"run_id": res.run_id, "status": res.status, "cost_usd": res.cost_usd, **res.counts},
                     indent=1))
    if res.warnings:
        print("\nWARNINGS (check before relying on this source):\n  " + "\n  ".join(res.warnings))
    if not args.no_trips:
        from ..trips import build_commute, compute_all, make_source

        t = compute_all(conn, build_commute(args.db, make_source("estimate")))
        print(f"estimate trips: {t['new_trips']} new rows for {t['places']} places")


if __name__ == "__main__":
    main()
