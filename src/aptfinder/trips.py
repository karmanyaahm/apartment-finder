"""Glue between the store and the independent commute module: computes `trips` rows for places.

Also the `aptf-commute` CLI (`route` for an ad-hoc A to B, `compute` for places).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from typing import Any

from . import settings
from .commute import Commute, GoogleRoutesLegSource, Station, StraightLineLegSource
from .commute.geo import haversine_m
from .config import Point, load_caltrain, load_point_sets, load_points
from .store import append_trip, connect, init_db


class SpendCapReached(RuntimeError):
    pass


class CappedSource:
    """Wraps a paid LegSource and refuses calls beyond a cap (cache hits are free and uncounted)."""

    def __init__(self, inner, max_calls: int):
        self.inner, self.max_calls, self.calls = inner, max_calls, 0
        self.name = inner.name

    def leg(self, o, d, mode, depart):
        if self.calls >= self.max_calls:
            raise SpendCapReached(f"{self.name}: reached cap of {self.max_calls} calls")
        self.calls += 1
        return self.inner.leg(o, d, mode, depart)


def make_source(provider: str, max_calls: int = 200):
    if provider == "google":
        key = settings.secret("GOOGLE_MAPS_API_KEY")
        if not key:
            raise SystemExit("GOOGLE_MAPS_API_KEY is not set in .env")
        return CappedSource(GoogleRoutesLegSource(key), max_calls)
    return StraightLineLegSource()


def build_commute(cache_db: str, source) -> Commute:
    stations = [Station(p.name, p.lat, p.lon) for p in load_points().get("caltrain_stations", [])]
    return Commute(cache_db, source, stations, load_caltrain())


def latest_trip(conn: sqlite3.Connection, place_key: str, point_id: str, mode: str, provider: str):
    return conn.execute(
        "SELECT * FROM trips WHERE place_key=? AND point_id=? AND mode=? AND provider=? "
        "ORDER BY id DESC LIMIT 1", (place_key, point_id, mode, provider)).fetchone()


def compute_for_place(conn: sqlite3.Connection, commute: Commute, place: sqlite3.Row | dict,
                      on_demand: bool = False, sets: list[str] | None = None) -> int:
    """Append trips for one place. Returns the number of new rows.

    Paid providers respect each set's prefilter_km and per-mode max_km unless on_demand.
    A row is appended only when no trip exists for that place, point, mode and provider, or its
    minutes changed (the leg cache makes recomputation free; this keeps the log from growing)."""
    origin = (place["lat"], place["lon"])
    if origin[0] is None or origin[1] is None:
        return 0
    paid = commute.source.name != "estimate"
    points = load_points()
    n = 0
    for set_name, cfg in load_point_sets().items():
        if sets and set_name not in sets:
            continue
        pts: list[Point] = points.get(set_name, [])
        if not pts:
            continue
        shortlist = sorted(pts, key=lambda p: haversine_m(origin, (p.lat, p.lon)))
        if cfg.k > 0:
            shortlist = shortlist[: cfg.k]
        for p in shortlist:
            km = haversine_m(origin, (p.lat, p.lon)) / 1000
            if paid and not on_demand and cfg.prefilter_km is not None and km > cfg.prefilter_km:
                continue
            for mode in cfg.modes:
                limit = cfg.max_km.get(mode)
                if paid and not on_demand and limit is not None and km > limit:
                    continue
                t = commute.trip(origin, (p.lat, p.lon), mode)
                prev = latest_trip(conn, place["place_key"], p.id, mode, t.provider)
                if prev is not None and prev["minutes"] == t.minutes:
                    continue
                append_trip(conn, place["place_key"], p.id, mode, t.minutes, t.distance_m, t.legs,
                            t.depart, t.provider)  # commit now: the commute cache writes via its own connection
                n += 1
    conn.commit()
    return n


def compute_all(conn: sqlite3.Connection, commute: Commute, place_keys: list[str] | None = None,
                on_demand: bool = False) -> dict[str, Any]:
    q = "SELECT place_key, lat, lon FROM places WHERE lat IS NOT NULL"
    places = conn.execute(q).fetchall()
    if place_keys:
        places = [p for p in places if p["place_key"] in place_keys]
    total, done, error = 0, 0, None
    try:
        for p in places:
            total += compute_for_place(conn, commute, p, on_demand=on_demand)
            done += 1
    except SpendCapReached as e:
        error = str(e)
    return {"places": len(places), "places_done": done, "new_trips": total, "error": error}


def _ll(text: str) -> tuple[float, float]:
    a, b = text.split(",")
    return (float(a), float(b))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aptf-commute")
    ap.add_argument("--db", default=str(settings.DB_PATH))
    ap.add_argument("--provider", choices=["estimate", "google"], default="estimate")
    ap.add_argument("--max-calls", type=int, default=200, help="cap on paid API calls this run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("route", help="ad-hoc trip between two coordinates")
    r.add_argument("origin", help="lat,lon")
    r.add_argument("dest", help="lat,lon")
    r.add_argument("--mode", default="bike")
    c = sub.add_parser("compute", help="append trips for places")
    c.add_argument("--place", action="append", help="place_key (repeatable); default all")
    c.add_argument("--on-demand", action="store_true", help="ignore straight-line prefilters")
    args = ap.parse_args(argv)

    source = make_source(args.provider, args.max_calls)
    commute = build_commute(args.db, source)
    if args.cmd == "route":
        print(json.dumps(commute.trip(_ll(args.origin), _ll(args.dest), args.mode).to_dict(), indent=2))
        return
    conn = connect(args.db)
    init_db(conn)
    from .store import finish_run, start_run

    run = start_run(conn, "commute", args.provider, {"places": args.place, "on_demand": args.on_demand})
    res = compute_all(conn, commute, args.place, args.on_demand)
    calls = getattr(source, "calls", 0)
    finish_run(conn, run, "error" if res["error"] else "ok", {**res, "api_calls": calls},
               est_cost=round(calls * 0.005, 3) if args.provider == "google" else 0.0,
               error=res["error"])
    print(json.dumps({**res, "api_calls": calls}, indent=2))


if __name__ == "__main__":
    main()
