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


def load_google_caps() -> dict[str, Any]:
    import tomllib

    with open(settings.CONFIG_DIR / "google.toml", "rb") as f:
        return tomllib.load(f)


def google_usage(conn: sqlite3.Connection, month: str | None = None) -> dict[str, Any]:
    """Calls logged this calendar month (UTC): total, by mode, and estimated cost beyond the free tier."""
    from datetime import datetime, timezone

    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    rows = conn.execute("SELECT mode, COUNT(*) FROM api_calls WHERE provider = 'google' AND ts LIKE ? "
                        "GROUP BY mode", (f"{month}%",)).fetchall()
    by_mode = {r[0]: r[1] for r in rows}
    total = sum(by_mode.values())
    caps = load_google_caps()
    billable = max(0, total - caps.get("free_calls_per_month", 0))
    return {"month": month, "calls": total, "by_mode": by_mode, "cap": caps["monthly_call_cap"],
            "cap_by_mode": caps.get("monthly_cap_by_mode", {}),
            "est_cost_usd": round(billable * caps.get("price_per_1000_usd", 5.0) / 1000, 2)}


class MeteredSource:
    """Wraps a paid LegSource. Before each real call (cache hits never reach it) checks the per-run cap and
    the monthly caps in config/google.toml against the `api_calls` log, then logs the call. One ledger in
    the main database for every caller (CLI, dashboard, eval), whatever cache file the caller uses."""

    def __init__(self, inner, max_calls: int, meter_db: str | None = None):
        self.inner, self.max_calls, self.calls = inner, max_calls, 0
        self.name = inner.name
        self.meter = connect(meter_db or settings.DB_PATH)
        init_db(self.meter)
        self.caps = load_google_caps()

    def leg(self, o, d, mode, depart):
        from .store import now_iso

        if self.calls >= self.max_calls:
            raise SpendCapReached(f"{self.name}: reached this run's cap of {self.max_calls} calls")
        use = google_usage(self.meter)
        if use["calls"] >= self.caps["monthly_call_cap"]:
            raise SpendCapReached(f"{self.name}: monthly cap of {self.caps['monthly_call_cap']} calls reached")
        mode_cap = self.caps.get("monthly_cap_by_mode", {}).get(mode)
        if mode_cap is not None and use["by_mode"].get(mode, 0) >= mode_cap:
            raise SpendCapReached(f"{self.name}: monthly {mode} cap of {mode_cap} calls reached")
        self.calls += 1
        ok = 0
        try:
            res = self.inner.leg(o, d, mode, depart)
            ok = 1
            return res
        finally:
            self.meter.execute("INSERT INTO api_calls (ts, provider, mode, ok) VALUES (?, ?, ?, ?)",
                               (now_iso(), self.name, mode, ok))
            self.meter.commit()


def make_source(provider: str, max_calls: int = 200, meter_db: str | None = None):
    if provider == "google":
        key = settings.secret("GOOGLE_MAPS_API_KEY")
        if not key:
            raise SystemExit("GOOGLE_MAPS_API_KEY is not set in .env")
        return MeteredSource(GoogleRoutesLegSource(key), max_calls, meter_db)
    return StraightLineLegSource()


def build_commute(cache_db: str, source) -> Commute:
    stations = [Station(p.name, p.lat, p.lon) for p in load_points().get("caltrain_stations", [])]
    return Commute(cache_db, source, stations, load_caltrain())


def latest_trip(conn: sqlite3.Connection, place_key: str, point_id: str, mode: str, provider: str):
    return conn.execute(
        "SELECT * FROM trips WHERE place_key=? AND point_id=? AND mode=? AND provider=? "
        "ORDER BY id DESC LIMIT 1", (place_key, point_id, mode, provider)).fetchone()


def compute_for_place(conn: sqlite3.Connection, commute: Commute, place: sqlite3.Row | dict,
                      on_demand: bool = False, sets: list[str] | None = None,
                      needs: list[tuple[str, str]] | None = None) -> int:
    """Append trips for one place. Returns the number of new rows.

    Paid providers respect each set's prefilter_km and per-mode max_km unless on_demand.
    A row is appended only when no trip exists for that place, point, mode and provider, or its
    minutes changed (the leg cache makes recomputation free; this keeps the log from growing)."""
    origin = (place["lat"], place["lon"])
    if origin[0] is None or origin[1] is None:
        return 0
    paid = commute.source.name != "estimate"
    points = load_points()
    pending = []  # written after all routing: the commute cache writes via its own connection
    for set_name, cfg in load_point_sets().items():
        if sets and set_name not in sets:
            continue
        pts: list[Point] = points.get(set_name, [])
        if not pts or (paid and not cfg.paid):
            continue
        shortlist = sorted(pts, key=lambda p: haversine_m(origin, (p.lat, p.lon)))
        if cfg.k > 0:
            shortlist = shortlist[: cfg.k]
        if needs is not None:  # only what the goals' commute rules read
            wanted = {m for t, m in needs if t == set_name} | {m for t, m in needs if t.startswith(set_name + ":")}
            if not wanted:
                continue
        for p in shortlist:
            if needs is not None and not any(t in (set_name, p.id) for t, _ in needs):
                continue
            km = haversine_m(origin, (p.lat, p.lon)) / 1000
            if paid and not on_demand and cfg.prefilter_km is not None and km > cfg.prefilter_km:
                continue
            for mode in cfg.modes:
                if needs is not None and (p.id, mode) not in needs and (set_name, mode) not in needs:
                    continue
                limit = cfg.max_km.get(mode)
                if paid and not on_demand and limit is not None and km > limit:
                    continue
                t = commute.trip(origin, (p.lat, p.lon), mode)
                prev = latest_trip(conn, place["place_key"], p.id, mode, t.provider)
                if prev is not None and prev["minutes"] == t.minutes:
                    continue
                pending.append((p.id, mode, t))
    for point_id, mode, t in pending:
        append_trip(conn, place["place_key"], point_id, mode, t.minutes, t.distance_m, t.legs, t.depart,
                    t.provider, commit=False)
    conn.commit()
    return len(pending)


def compute_all(conn: sqlite3.Connection, commute: Commute, place_keys: list[str] | None = None,
                on_demand: bool = False, needs_by_place: dict[str, list[tuple[str, str]]] | None = None
                ) -> dict[str, Any]:
    q = "SELECT place_key, lat, lon FROM places WHERE lat IS NOT NULL"
    places = conn.execute(q).fetchall()
    if place_keys:
        places = [p for p in places if p["place_key"] in place_keys]
    total, done, error = 0, 0, None
    try:
        for p in places:
            total += compute_for_place(conn, commute, p, on_demand=on_demand,
                                       needs=(needs_by_place or {}).get(p["place_key"]) if needs_by_place else None)
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
    sub.add_parser("usage", help="Google calls this month against the caps in config/google.toml")
    c = sub.add_parser("compute", help="append trips for places")
    c.add_argument("--place", action="append", help="place_key (repeatable); default all")
    c.add_argument("--on-demand", action="store_true", help="ignore straight-line prefilters")
    c.add_argument("--candidates", action="store_true",
                   help="only places in active chunks that can still pass a goal apart from commute rules")
    c.add_argument("--dry-run", action="store_true",
                   help="count the uncached paid calls this would make, using estimates in a scratch cache copy")
    args = ap.parse_args(argv)

    if args.cmd == "usage":
        conn = connect(args.db)
        init_db(conn)
        print(json.dumps(google_usage(conn), indent=1))
        return
    conn = connect(args.db)
    init_db(conn)
    if args.cmd == "compute" and args.candidates:
        from .areas import chunk_of, is_active
        from .filter import routing_candidates

        from .filter import routing_needs

        cands = routing_candidates(conn)
        args.place = sorted(pk for pk, la, lo in conn.execute("SELECT place_key, lat, lon FROM places")
                            if pk in cands and is_active(chunk_of(la, lo)))
        needs_by_place = {pk: routing_needs(cands[pk]) for pk in args.place}
        print(f"{len(args.place)} candidate places in active chunks; routing only what their goals read")
    else:
        needs_by_place = None
    if args.cmd == "compute" and args.dry_run:
        import shutil
        import tempfile

        class Counting(StraightLineLegSource):
            name = args.provider
            calls = 0

            def leg(self, o, d, mode, depart):
                Counting.calls += 1
                return StraightLineLegSource.leg(self, o, d, mode if mode != "transit" else "bike", depart)

        tmp = tempfile.mkdtemp()
        cache_copy = f"{tmp}/cache.db"
        src = connect(args.db)
        dst = connect(cache_copy)
        src.backup(dst)
        dst.close()
        dry = connect(cache_copy)
        init_db(dry)
        res = compute_all(dry, build_commute(cache_copy, Counting()), args.place, args.on_demand, needs_by_place)
        shutil.rmtree(tmp)
        print(json.dumps({"would_call": Counting.calls, "places": res["places"],
                          "google_usage_now": google_usage(conn)}, indent=1))
        return
    source = make_source(args.provider, args.max_calls)
    commute = build_commute(args.db, source)
    if args.cmd == "route":
        print(json.dumps(commute.trip(_ll(args.origin), _ll(args.dest), args.mode).to_dict(), indent=2))
        return
    from .store import finish_run, start_run

    run = start_run(conn, "commute", args.provider, {"places": args.place, "on_demand": args.on_demand})
    res = compute_all(conn, commute, args.place, args.on_demand, needs_by_place)
    calls = getattr(source, "calls", 0)
    finish_run(conn, run, "error" if res["error"] else "ok", {**res, "api_calls": calls},
               est_cost=round(calls * 0.005, 3) if args.provider == "google" else 0.0,
               error=res["error"])
    print(json.dumps({**res, "api_calls": calls}, indent=2))


if __name__ == "__main__":
    main()
