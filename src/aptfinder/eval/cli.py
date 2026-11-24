"""aptf-eval: discovery benchmark.

  geocode        geocode benchmark addresses once (Nominatim) into eval/benchmark_geocoded.csv
  commute-check  compare computed walk-to-Caltrain and drive-to-Meta with the sheet's Google estimates
  recall         which benchmark rows the poller has found, per tier
"""

from __future__ import annotations

import argparse
import csv
import re
import time

import httpx

from .. import settings
from ..commute.geo import haversine_m
from ..config import load_points
from ..store import connect, init_db
from ..store.keys import parse_address, place_key
from .benchmark import GEOCODED, load_benchmark, load_geocodes

NOTES = settings.ROOT / "eval" / "recall_notes.csv"  # user-maintained: name,reason (e.g. taken_down)


def cmd_geocode() -> None:
    done = load_geocodes()
    out = []
    client = httpx.Client(timeout=30, headers={"User-Agent": "apartment-finder/0.1 (personal research)"})
    for r in load_benchmark():
        if r.tier == "ruled_out":
            continue
        if r.name in done:
            out.append({"name": r.name, "lat": done[r.name][0], "lon": done[r.name][1], "query": ""})
            continue
        p = parse_address(r.name, r.city)
        q = f"{p['street']}, {r.city}, CA"
        res = client.get("https://nominatim.openstreetmap.org/search",
                         params={"format": "json", "q": q, "limit": 1}).json()
        time.sleep(1.1)  # Nominatim usage policy: max 1 request per second
        lat, lon = (res[0]["lat"], res[0]["lon"]) if res else ("", "")
        print(f"{'ok ' if res else 'MISS'} {q} -> {lat},{lon}")
        out.append({"name": r.name, "lat": lat, "lon": lon, "query": q})
    with open(GEOCODED, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "lat", "lon", "query"])
        w.writeheader()
        w.writerows(out)


def cmd_commute_check(provider: str, max_calls: int, db: str) -> None:
    from ..trips import build_commute, make_source

    commute = build_commute(db, make_source(provider, max_calls))
    geos = load_geocodes()
    targets = {p.name: p for p in load_points()["commute_targets"]}
    stations = load_points()["caltrain_stations"]
    meta = targets["Meta HQ"]
    print(f"provider={provider}   (sheet values are Google Maps estimates)")
    print(f"{'listing':48s} {'station':>14s} {'walk sheet':>10s} {'calc':>6s} {'diff':>6s}"
          f" {'drive sheet':>11s} {'calc':>6s} {'diff':>6s}")
    walk_d, drive_d = [], []
    for r in load_benchmark():
        if r.name not in geos:
            continue
        o = geos[r.name]
        (st, wt), = commute.nearest(o, stations, "walk", k=1) or [(None, None)]
        dt = commute.trip(o, (meta.lat, meta.lon), "drive")

        def fmt(v):
            return f"{v:6.1f}" if v is not None else "     -"

        wd = (wt.minutes - r.walk_caltrain) if (wt and wt.minutes is not None and r.walk_caltrain) else None
        dd = (dt.minutes - r.drive_meta) if (dt.minutes is not None and r.drive_meta) else None
        if wd is not None:
            walk_d.append(abs(wd))
        if dd is not None:
            drive_d.append(abs(dd))
        flag = "  <- station differs" if st and r.station and st.name != r.station else ""
        print(f"{r.name[:48]:48s} {st.name if st else '-':>14s} {fmt(r.walk_caltrain):>10s} "
              f"{fmt(wt.minutes if wt else None)} {fmt(wd)} {fmt(r.drive_meta):>11s} {fmt(dt.minutes)} "
              f"{fmt(dd)}{flag}")
    for label, ds in (("walk", walk_d), ("drive", drive_d)):
        if ds:
            within = sum(d <= 2 for d in ds)
            print(f"{label}: mean |diff| {sum(ds) / len(ds):.1f} min, within 2 min: {within}/{len(ds)}")


def cmd_recall(db: str) -> None:
    conn = connect(db)
    init_db(conn)
    places = conn.execute("SELECT place_key, lat, lon, property_name, address FROM places").fetchall()
    keys = {p["place_key"] for p in places}
    notes = {}
    if NOTES.exists():
        with open(NOTES, newline="") as f:
            notes = {r["name"]: r["reason"] for r in csv.DictReader(f)}
    geos = load_geocodes()
    from ..intake.base import coverage

    from ..config import load_areas

    covered_zips = {aid for (src, aid) in coverage(conn) if src in ("zillow", "apartments_com")}
    covered_cities = {a.city for a in load_areas()[1] if a.kind == "zip" and a.id in covered_zips}
    tiers: dict[str, list[tuple[str, str]]] = {}
    for r in load_benchmark():
        found = False
        if r.tier == "ruled_out":
            found = any(p["property_name"] and r.name.lower() in p["property_name"].lower() for p in places)
        else:
            if place_key(r.name, r.city, None, None) in keys:
                found = True
            elif r.name in geos:  # fallback: any place within 60 m
                found = any(p["lat"] and haversine_m(geos[r.name], (p["lat"], p["lon"])) < 60 for p in places)
        zm = re.search(r"\b(9\d{4})\b", r.name)
        if found:
            status = "found"
        elif r.name in notes:
            status = "missed: " + notes[r.name]
        elif zm and zm.group(1) not in covered_zips:
            status = f"not scraped yet: ZIP {zm.group(1)} is TODO"
        elif not zm and r.city and r.city not in covered_cities:
            status = f"not scraped yet: no ZIP of {r.city} covered"
        else:
            status = "missed: not found by poller"
        tiers.setdefault(r.tier, []).append((r.name, status))
    for tier, items in tiers.items():
        counted = [s for _, s in items if "taken_down" not in s and not s.startswith("not scraped")]
        n_found = sum(s == "found" for s in counted)
        n_todo = sum(s.startswith("not scraped") for _, s in items)
        print(f"\n== {tier}: {n_found}/{len(counted)} found in scraped areas"
              f" ({n_todo} in areas not scraped yet, {len(items) - len(counted) - n_todo} excused as taken down)")
        for name, s in items:
            print(f"  [{'x' if s == 'found' else ' '}] {name[:70]:70s} {'' if s == 'found' else s}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aptf-eval")
    ap.add_argument("--db", default=str(settings.DB_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("geocode")
    cc = sub.add_parser("commute-check")
    cc.add_argument("--provider", choices=["estimate", "google"], default="estimate")
    cc.add_argument("--max-calls", type=int, default=60)
    sub.add_parser("recall")
    args = ap.parse_args(argv)
    if args.cmd == "geocode":
        cmd_geocode()
    elif args.cmd == "commute-check":
        cmd_commute_check(args.provider, args.max_calls, args.db)
    else:
        cmd_recall(args.db)


if __name__ == "__main__":
    main()
