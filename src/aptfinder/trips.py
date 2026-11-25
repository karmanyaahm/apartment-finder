"""Glue between the store and the independent commute module: computes `trips` rows for places.

Also the `aptf-commute` CLI (`route` for an ad-hoc A to B, `compute` for places).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from typing import Any

from . import settings
from .commute import Commute, GoogleRoutesLegSource, LegResult, Station, StraightLineLegSource
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
    # Compute Route Matrix: its own SKU and free tier, one api_calls row per element (provider google_matrix)
    elements = conn.execute("SELECT COUNT(*) FROM api_calls WHERE provider = 'google_matrix' AND ts LIKE ?",
                            (f"{month}%",)).fetchone()[0]
    m_billable = max(0, elements - caps.get("matrix_free_elements_per_month", 0))
    return {"month": month, "calls": total, "by_mode": by_mode, "cap": caps["monthly_call_cap"],
            "cap_by_mode": caps.get("monthly_cap_by_mode", {}),
            "est_cost_usd": round(billable * caps.get("price_per_1000_usd", 5.0) / 1000
                                  + m_billable * caps.get("matrix_price_per_1000_usd", 5.0) / 1000, 2),
            "matrix_elements": elements, "matrix_cap": caps.get("matrix_monthly_element_cap", 0)}


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


class MeteredMatrix:
    """Google's Compute Route Matrix for `Commute.prefetch`, metered per element: before each request, the per-run
    cap and `matrix_monthly_element_cap` in config/google.toml (none set: no matrix calls) against the `api_calls`
    log (provider `google_matrix`, one row per element), then logged. Paced under the API's 3,000 elements a
    minute. Cache rows are written under provider `google` (the same router as computeRoutes)."""

    name = "google"
    PER_MINUTE = 2500

    def __init__(self, inner: GoogleRoutesLegSource, max_elements: int, meter_db: str | None = None):
        self.inner, self.max_elements, self.elements = inner, max_elements, 0
        self.meter = connect(meter_db or settings.DB_PATH)
        init_db(self.meter)
        self.cap = load_google_caps().get("matrix_monthly_element_cap", 0)
        self._last = 0.0

    def leg(self, o, d, mode, depart):
        raise SpendCapReached("matrix source: single legs go through computeRoutes (MeteredSource)")

    def matrix(self, origins, dests, mode, depart):
        import time

        from .store import now_iso

        n = len(origins) * len(dests)
        if self.elements + n > self.max_elements:
            raise SpendCapReached(f"google_matrix: this run's cap of {self.max_elements} elements reached")
        used = google_usage(self.meter)["matrix_elements"]
        if used + n > self.cap:
            raise SpendCapReached(f"google_matrix: monthly cap of {self.cap} elements (config/google.toml "
                                  f"matrix_monthly_element_cap; {used} used)")
        wait = self._last + 60 * n / self.PER_MINUTE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.elements += n
        ok = 0
        try:
            res = self.inner.matrix(origins, dests, mode, depart)
            ok = 1
            return res
        finally:
            ts = now_iso()
            self.meter.executemany("INSERT INTO api_calls (ts, provider, mode, ok, note) VALUES (?, ?, ?, ?, ?)",
                                   [(ts, "google_matrix", mode, ok, None)] * n)
            self.meter.commit()


def make_source(provider: str, max_calls: int = 200, meter_db: str | None = None):
    if provider == "google":
        key = settings.secret("GOOGLE_MAPS_API_KEY")
        if not key:
            raise SystemExit("GOOGLE_MAPS_API_KEY is not set in .env")
        return MeteredSource(GoogleRoutesLegSource(key), max_calls, meter_db)
    return StraightLineLegSource()


def route_anchors(conn, db_path: str, provider: str, max_calls: int, dry_run: bool = False) -> dict:
    """Bike legs from every Caltrain station to every commute target: a fixed set (stations x targets), so routing
    it once with a real provider is cheap, and the estimator reuses it for every place (`Commute.leg`)."""
    pts = load_points()
    stations, targets = pts.get("caltrain_stations", []), pts.get("commute_targets", [])
    pairs = [(s, t) for s in stations for t in targets]
    if dry_run or provider == "estimate":
        return {"pairs": len(pairs), "stations": [s.name for s in stations], "targets": [t.name for t in targets]}
    source = make_source(provider, max_calls=max_calls, meter_db=db_path)
    commute = build_commute(db_path, source)
    est = build_commute(db_path, make_source("estimate"))
    out = []
    for s, t in pairs:
        before = est.source.leg((s.lat, s.lon), (t.lat, t.lon), "bike", est.depart)  # pure straight-line guess
        r = commute.leg((s.lat, s.lon), (t.lat, t.lon), "bike")
        out.append({"station": s.name, "target": t.name, "bike_min": r.minutes if r else None,
                    "estimate_min": round(before.minutes, 1) if before else None})
    return {"routed": out, "api_calls": getattr(source, "calls", None)}


# bike+train tries the K_STATIONS stations nearest the place and the destination (straight line); the KNN grid's
# labels read it, so the text and the routing agree
K_STATIONS = 2


def build_commute(cache_db: str, source) -> Commute:
    stations = [Station(p.name, p.lat, p.lon) for p in load_points().get("caltrain_stations", [])]
    return Commute(cache_db, source, stations, load_caltrain(), k_stations=K_STATIONS)


def latest_trip(conn: sqlite3.Connection, place_key: str, point_id: str, mode: str, provider: str):
    return conn.execute(
        "SELECT * FROM trips WHERE place_key=? AND point_id=? AND mode=? AND provider=? "
        "ORDER BY id DESC LIMIT 1", (place_key, point_id, mode, provider)).fetchone()


def nearest_points(origin: tuple[float, float], pts: list[Point], k: int) -> list[Point]:
    """A set's points a place is routed to: the k nearest by straight line (k = 0: all of them)."""
    shortlist = sorted(pts, key=lambda p: haversine_m(origin, (p.lat, p.lon)))
    return shortlist[:k] if k > 0 else shortlist


def missing_estimates(conn: sqlite3.Connection) -> list[str]:
    """Routable located places (a stay place as its building, as compute_all) lacking a straight-line estimate row
    for some (point, mode) the current config asks of them: for each set, its k nearest points by each of its
    `modes`. A point added to a set (a new Google bus stop) is then nearest to places that already had estimates to
    the old points; they are listed again, so the stop shows up without anyone recomputing by hand."""
    located = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT place_key, lat, lon FROM places WHERE lat IS NOT NULL")}
    routable = [pk for pk in located if pk.split("~", 1)[0] == pk or pk.split("~", 1)[0] not in located]
    have: dict[str, set[tuple[str, str]]] = {}
    for pk, pt, mode in conn.execute("SELECT DISTINCT place_key, point_id, mode FROM trips WHERE provider = 'estimate'"):
        have.setdefault(pk, set()).add((pt, mode))
    points, point_sets = load_points(), load_point_sets()
    sets = [(points.get(name, []), cfg) for name, cfg in point_sets.items()]
    out = []
    for pk in sorted(routable):
        mine = have.get(pk, set())
        if any((p.id, m) not in mine for pts, cfg in sets if pts
               for p in nearest_points(located[pk], pts, cfg.k) for m in cfg.modes):
            out.append(pk)
    return out


def compute_for_place(conn: sqlite3.Connection, commute: Commute, place: sqlite3.Row | dict,
                      on_demand: bool = False, sets: list[str] | None = None,
                      only: tuple[str, str] | None = None, points: dict | None = None,
                      point_sets: dict | None = None) -> int:
    """Append trips for one place. Returns the number of new rows. `points` / `point_sets`: load_points() and
    load_point_sets() already read by the caller (compute_all reads them once, not once per place).

    Paid providers respect each set's prefilter_km, per-mode max_km and `auto` (point -> modes) unless
    on_demand, which routes the set's full `modes` table.
    A row is appended only when no trip exists for that place, point, mode and provider, or its
    minutes changed (the leg cache makes recomputation free; this keeps the log from growing).
    `only` = (point, mode) computes one attribute: a point id (`commute_targets:Meta HQ`) or `set:<name>` (that
    set's k nearest points), by that one mode, whether or not the set's `modes` lists it."""
    origin = (place["lat"], place["lon"])
    if origin[0] is None or origin[1] is None:
        return 0
    paid = commute.source.name != "estimate"
    points = points if points is not None else load_points()
    pending = []  # written after all routing: the commute cache writes via its own connection
    for set_name, cfg in (point_sets if point_sets is not None else load_point_sets()).items():
        if sets and set_name not in sets:
            continue
        pts: list[Point] = points.get(set_name, [])
        modes = cfg.modes
        if only:
            target, mode_only = only
            if target.startswith("set:") and target[4:] != set_name:
                continue
            if not target.startswith("set:"):
                if target.split(":", 1)[0] != set_name:
                    continue
                pts = [p for p in pts if p.id == target]
            modes = [mode_only]
        if not pts or (paid and not cfg.paid):
            continue
        for p in nearest_points(origin, pts, cfg.k):
            km = haversine_m(origin, (p.lat, p.lon)) / 1000
            if paid and not on_demand and cfg.prefilter_km is not None and km > cfg.prefilter_km:
                continue
            for mode in modes:
                if paid and not on_demand and cfg.auto is not None and mode not in cfg.auto.get(p.name, []):
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
                on_demand: bool = False, only: tuple[str, str] | None = None,
                progress=None) -> dict[str, Any]:
    # stay places (`<building>~<operator>`) share their building's trips (filter._Trips): never routed twice
    q = "SELECT place_key, lat, lon FROM places WHERE lat IS NOT NULL"
    places = conn.execute(q).fetchall()
    if place_keys:
        places = [p for p in places if p["place_key"] in place_keys]
    else:
        keys = {p["place_key"] for p in places}
        places = [p for p in places if p["place_key"].split("~", 1)[0] == p["place_key"]
                  or p["place_key"].split("~", 1)[0] not in keys]
    total, done, error = 0, 0, None
    points, point_sets = load_points(), load_point_sets()
    try:
        for p in places:
            total += compute_for_place(conn, commute, p, on_demand=on_demand, only=only, points=points,
                                       point_sets=point_sets)
            done += 1
            if progress:
                progress(done, len(places), total)
    except SpendCapReached as e:
        error = str(e)
    return {"places": len(places), "places_done": done, "new_trips": total, "error": error}


def estimate_missing(conn: sqlite3.Connection, db_path: str, after: str = "intake") -> dict[str, Any]:
    """Free straight-line trips for the places missing one their current nearest points need (`missing_estimates`,
    the Pipeline panel's rule), logged as a `commute`/`estimate` run. Complete places are not routed again
    (`compute_all` would walk every place)."""
    from .store import finish_run, start_run

    todo = missing_estimates(conn)
    if not todo:
        return {"places": 0, "places_done": 0, "new_trips": 0, "error": None}
    run = start_run(conn, "commute", "estimate", {"places": len(todo), "after": after})
    res = compute_all(conn, build_commute(db_path, make_source("estimate")), todo)
    finish_run(conn, run, "error" if res["error"] else "ok", {**res, "api_calls": 0}, est_cost=0.0, error=res["error"])
    return res


def planned_legs(db_path: str, place_keys: list[str], only: tuple[str, str],
                 located: dict[str, tuple[float, float]] | None = None) -> list[tuple]:
    """The single legs (origin, destination, mode) that `compute_all(place_keys, on_demand=True, only=only)` with
    Google would route and are not in the cache yet. Walks the same code with a recording source over an in-memory
    copy of just these places (composed modes ask for their legs the same way); reads the real cache read-only,
    routes and writes nothing. `located`: place_key -> (lat, lon) already read (sampling.located), else read here."""
    legs: list[tuple] = []
    real = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    has_cache = real.execute("SELECT 1 FROM sqlite_master WHERE name = 'commute_legs'").fetchone()

    class Recording(StraightLineLegSource):
        name = "google"

        def leg(self, o, d, mode, depart):
            row = has_cache and real.execute(
                "SELECT found, minutes, distance_m FROM commute_legs WHERE o_lat=? AND o_lon=? AND d_lat=? AND "
                "d_lon=? AND mode=? AND depart=? AND provider='google'", (*o, *d, mode, depart)).fetchone()
            if row:
                return LegResult(row[1], row[2]) if row[0] else None
            legs.append((o, d, mode))
            return StraightLineLegSource.leg(self, o, d, mode if mode != "transit" else "bike", depart)

    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE places (place_key TEXT, lat REAL, lon REAL)")
    mem.execute("CREATE TABLE trips (id INTEGER PRIMARY KEY, computed_at TEXT, place_key TEXT, point_id TEXT, "
                "mode TEXT, minutes REAL, distance_m REAL, legs TEXT, depart_assumption TEXT, provider TEXT)")
    want = set(place_keys)
    rows = ([(pk, *ll) for pk, ll in located.items()] if located is not None
            else real.execute("SELECT place_key, lat, lon FROM places WHERE lat IS NOT NULL"))
    mem.executemany("INSERT INTO places VALUES (?, ?, ?)", [r for r in rows if r[0] in want])
    compute_all(mem, build_commute(":memory:", Recording()), list(place_keys), on_demand=True, only=only)
    real.close()
    return legs


def dry_run(db_path: str, provider: str, place_keys: list[str] | None = None, on_demand: bool = False,
            only: tuple[str, str] | None = None) -> dict[str, Any]:
    """The uncached calls `provider` would make for these places, counted on a scratch copy of the database
    with straight-line answers (so nothing is routed, paid for or written), and what they would cost against
    the monthly caps in config/google.toml."""
    import shutil
    import tempfile

    class Counting(StraightLineLegSource):
        name = provider
        calls = 0
        by_mode: dict[str, int] = {}

        def leg(self, o, d, mode, depart):
            Counting.calls += 1
            Counting.by_mode[mode] = Counting.by_mode.get(mode, 0) + 1
            return StraightLineLegSource.leg(self, o, d, mode if mode != "transit" else "bike", depart)

    tmp = tempfile.mkdtemp()
    try:
        cache_copy = f"{tmp}/cache.db"
        src = connect(db_path)
        dst = connect(cache_copy)
        src.backup(dst)
        dst.close()
        dry = connect(cache_copy)
        init_db(dry)
        res = compute_all(dry, build_commute(cache_copy, Counting()), place_keys, on_demand, only)
        dry.close()
    finally:
        shutil.rmtree(tmp)
    usage = google_usage(src)
    src.close()
    caps = load_google_caps()
    free, price = caps.get("free_calls_per_month", 0), caps.get("price_per_1000_usd", 5.0)
    billable = max(0, usage["calls"] + Counting.calls - free) - max(0, usage["calls"] - free)
    over = []
    if usage["calls"] + Counting.calls > usage["cap"]:
        over.append(f"monthly cap {usage['cap']} ({usage['calls']} used)")
    for mode, n in Counting.by_mode.items():
        cap = usage["cap_by_mode"].get(mode)
        if cap is not None and usage["by_mode"].get(mode, 0) + n > cap:
            over.append(f"monthly {mode} cap {cap} ({usage['by_mode'].get(mode, 0)} used)")
    return {"would_call": Counting.calls, "by_mode": dict(Counting.by_mode), "places": res["places"],
            "est_cost_usd": round(billable * price / 1000, 2), "over_caps": over, "google_usage_now": usage}


def route_plan(conn: sqlite3.Connection, db_path: str, plan: dict[tuple[str, str], list[str]], label: str,
               max_elements: int, max_calls: int = 20, progress=None, origin: str | None = None) -> dict[str, Any]:
    """Route a sampling plan (pair -> places) with Google: per pair, the uncached legs go through Compute Route
    Matrix (`MeteredMatrix`, per-element cap) into the leg cache, then `compute_all` appends the trips from it; a leg
    the matrix left out goes to computeRoutes under `max_calls`. One `commute` run row per pair (params `from` =
    `origin`: the dashboard closes its own runs a restart cut off). Each matrix request's elements are in the leg
    cache as soon as it returns (`Commute.prefetch` commits per request), so a rerun never pays for them again."""
    from .store import finish_run, start_run

    key = settings.secret("GOOGLE_MAPS_API_KEY")
    if not key:
        raise SystemExit("GOOGLE_MAPS_API_KEY is not set in .env")
    matrix = MeteredMatrix(GoogleRoutesLegSource(key), max_elements, meter_db=db_path)
    prefetch = build_commute(db_path, matrix)
    source = make_source("google", max_calls, meter_db=db_path)
    commute = build_commute(db_path, source)
    out: dict[str, Any] = {"pairs": {}, "error": None, "new_trips": 0}
    done_places = 0
    for p, keys in plan.items():
        if not keys:
            continue
        before_e, before_c = matrix.elements, source.calls
        run = start_run(conn, "commute", "google", {"sample": label, "only": list(p), "places": len(keys),
                                                    **({"from": origin} if origin else {})})
        pre: dict[str, int] = {}
        try:
            pre = prefetch.prefetch(planned_legs(db_path, keys, p))
            res = compute_all(conn, commute, keys, on_demand=True, only=p,
                              progress=(lambda d, n, t: progress(done_places + d, t)) if progress else None)
        except (SpendCapReached, RuntimeError) as e:
            res = {"places": len(keys), "places_done": 0, "new_trips": 0, "error": str(e)}
        done_places += res["places_done"]
        counts = {**res, **pre, "matrix_elements": matrix.elements - before_e, "api_calls": source.calls - before_c}
        finish_run(conn, run, "error" if res["error"] else "ok", counts,
                   est_cost=round((counts["matrix_elements"] + counts["api_calls"]) * 0.005, 3), error=res["error"])
        out["pairs"]["|".join(p)] = counts
        out["new_trips"] += res["new_trips"]
        if res["error"]:
            out["error"] = res["error"]
            break
    out["matrix_elements"], out["api_calls"] = matrix.elements, source.calls
    return out


def sample(conn: sqlite3.Connection, args) -> dict[str, Any]:
    """`aptf-commute sample`: plan per pair over the candidates, in a walk zone or not (cover, random % or refine), then a dry run
    (places, matrix elements, coverage) or the Google run (`route_plan`)."""
    from . import sampling
    from .filter import best_trips
    from .filter.goals import effective_goals

    pairs = [tuple(p.split("|", 1)) for p in args.pair]
    for p in pairs:
        if len(p) != 2:
            raise SystemExit(f"--pair must be POINT|MODE, got {p!r}")
    cands = sampling.candidate_goals(conn)
    keys = sorted(cands)
    trips = best_trips(conn)
    if args.refine:
        plan, notes = sampling.refine_plan(trips, pairs, keys, effective_goals(conn), cands)
        label = "refine"
    elif args.pct:
        plan = sampling.random_plan(trips, pairs, keys, args.pct, args.seed)
        label = f"random {args.pct:g}%"
    else:  # only places that can still pass a goal with a rule on the pair (a stay needs transit, a lease does not)
        plan = sampling.goal_cover_plan(trips, pairs, keys, sampling.located(conn), effective_goals(conn), cands,
                                        args.radius_m)
        label = f"cover within {args.radius_m:g} m"
    out = {"plan": label, **sampling.summary(conn, args.db, plan, keys, trips), "google_usage_now": google_usage(conn)}
    if args.refine:
        for p, n in notes.items():
            out["pairs"]["|".join(p)].update(n)
    if args.dry_run:
        return out
    return {**out, "run": route_plan(conn, args.db, plan, label, args.max_elements, args.max_calls)}


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
    an = sub.add_parser("anchors", help="route each Caltrain station -> commute target bike leg once (Google); every "
                                        "place's bike+train estimate then uses these real legs")
    an.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("compute", help="append trips for places")
    c.add_argument("--place", action="append", help="place_key (repeatable); default all")
    c.add_argument("--on-demand", action="store_true", help="ignore straight-line prefilters")
    c.add_argument("--candidates", action="store_true",
                   help="only places in a station walk zone that can still pass a goal apart from commute rules")
    c.add_argument("--dry-run", action="store_true",
                   help="count the uncached paid calls this would make, using estimates in a scratch cache copy")
    sm = sub.add_parser("sample", help="route a covering set (default), a random sample (or, with --refine, the ambiguous places) of the "
                                       "candidates (in a walk zone or not) with Google Route Matrix so KNN ranges cover the rest "
                                       "(aptfinder/sampling.py); the dashboard's Pipeline panel does the same")
    sm.add_argument("--pair", action="append", required=True,
                    help="POINT|MODE, e.g. 'commute_targets:Meta HQ|drive' or 'set:caltrain_stations|walk' (repeatable)")
    sm.add_argument("--radius-m", type=float, default=400,
                    help="cover: route places until every candidate has a routed place within this distance")
    sm.add_argument("--pct", type=float, help="a random sample of this many percent of the candidates")
    sm.add_argument("--seed", type=int)
    sm.add_argument("--refine", action="store_true",
                    help="route candidates whose KNN range straddles a goal's limit or is wider than "
                         "sampling.REFINE_MAX_SPREAD_MIN (2 min), instead of the cover")
    sm.add_argument("--dry-run", action="store_true", help="count the uncached matrix elements per pair; no calls")
    sm.add_argument("--max-elements", type=int, default=1000, help="cap on Route Matrix elements this run")
    args = ap.parse_args(argv)

    if args.cmd == "usage":
        conn = connect(args.db)
        init_db(conn)
        print(json.dumps(google_usage(conn), indent=1))
        return
    conn = connect(args.db)
    init_db(conn)
    if args.cmd == "anchors":
        print(json.dumps(route_anchors(conn, args.db, args.provider, args.max_calls, args.dry_run), indent=1))
        return
    if args.cmd == "sample":
        print(json.dumps(sample(conn, args), indent=1))
        return
    if args.cmd == "compute" and args.candidates:
        from .areas import in_walk_zone
        from .filter import routing_candidates

        cands = routing_candidates(conn)
        # a stay place (`<building>~<operator>`) is routed as its building, whose trips it reads
        # (or as itself when no listing makes the building a place)
        located = {pk: (la, lo) for pk, la, lo in conn.execute("SELECT place_key, lat, lon FROM places")}
        merged: dict[str, set] = {}
        for pk, goals in cands.items():
            base = pk.split("~", 1)[0]
            merged.setdefault(base if base in located else pk, set()).update(goals)
        args.place = sorted(pk for pk in merged if pk in located and in_walk_zone(*located[pk]))
        print(f"{len(args.place)} candidate places in station walk zones; routing the `auto` modes in point_sets.toml")
    if args.cmd == "compute" and args.dry_run:
        print(json.dumps(dry_run(args.db, args.provider, args.place, args.on_demand), indent=1))
        return
    source = make_source(args.provider, args.max_calls)
    commute = build_commute(args.db, source)
    if args.cmd == "route":
        print(json.dumps(commute.trip(_ll(args.origin), _ll(args.dest), args.mode).to_dict(), indent=2))
        return
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
