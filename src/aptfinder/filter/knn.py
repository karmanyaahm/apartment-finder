"""KNN commute times: a place without its own Google trip for a (point, mode) reads the range of the up to K nearest
places within RADIUS_M that have one (`provider` 'knn'). Derived at read time from stored Google trips; nothing is
written. Only the cells (point or `set:<name>`, mode) checked in the dashboard's Pipeline grid get KNN (`cells`).

A KNN trip is {minutes: the middle neighbour, min, max, neighbors: [{place_key, minutes, km}]}; rules read the range
padded by rules.KNN_PAD_MIN (rules._minutes_status). With no Google neighbour within RADIUS_M the place keeps its
straight-line estimate; where that has no time (transit) or Google found no route, `far_estimate` gives the middle of the K
nearest Google times at any distance as an estimate. A place's own Google trip (with a time) always wins; a KNN range
beats a straight-line estimate.
Leave-one-out on the box's Google trips (2026-11-25): within 400 m the range is about 1 minute wide (median) and the
true time, when outside it, is at most 0.7 min outside at p90, so the radius sets coverage, not accuracy.
"""

from __future__ import annotations

from collections import defaultdict
from math import cos, radians
from typing import Any

from ..commute.geo import haversine_m

K = 3
RADIUS_M = 400
RADIUS_KM = RADIUS_M / 1000
_CELL_DEG = 0.02  # grid cell for the neighbour search: about 2.2 km of latitude


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return int(lat // _CELL_DEG), int(lon // _CELL_DEG)


def neighbours(train: list[tuple[str, float, float, float]], lat: float, lon: float, exclude: str | None = None,
               k: int = K, radius_km: float = RADIUS_KM, grid: dict | None = None) -> list[tuple[float, str, float]]:
    """The k nearest (km, place_key, minutes) of `train` [(place_key, lat, lon, minutes)] within radius_km."""
    if grid is None:
        grid = index(train)
    reach_lat = int(radius_km / 111 // _CELL_DEG) + 1
    reach_lon = int(radius_km / (111 * max(cos(radians(lat)), 0.1)) // _CELL_DEG) + 1
    cy, cx = _cell(lat, lon)
    # a cheap box first: a point further than radius_km north-south (or, with slack, east-west) cannot be within
    # it, so most of the 3 x 3 cells' points skip the haversine (same neighbours, a fraction of the work)
    dlat_max = radius_km / 111.19
    dlon_max = dlat_max / max(cos(radians(abs(lat) + 1)), 0.1) * 1.1
    found = []
    for dy in range(-reach_lat, reach_lat + 1):
        for dx in range(-reach_lon, reach_lon + 1):
            for pk, la, lo, m in grid.get((cy + dy, cx + dx), ()):
                if pk == exclude or abs(la - lat) > dlat_max or abs(lo - lon) > dlon_max:
                    continue
                km = haversine_m((lat, lon), (la, lo)) / 1000
                if km <= radius_km:
                    found.append((km, pk, m))
    found.sort()
    return found[:k]


def index(train: list[tuple[str, float, float, float]]) -> dict[tuple[int, int], list]:
    grid: dict[tuple[int, int], list] = defaultdict(list)
    for row in train:
        grid[_cell(row[1], row[2])].append(row)
    return grid


def knn_trip(near: list[tuple[float, str, float]], point_id: str, mode: str) -> dict[str, Any]:
    mins = sorted(m for _, _, m in near)
    return {"provider": "knn", "point_id": point_id, "mode": mode, "minutes": mins[len(mins) // 2],
            "min": mins[0], "max": mins[-1], "distance_m": None, "legs": "[]", "id": None,
            "neighbors": [{"place_key": pk, "minutes": m, "km": round(km, 2)} for km, pk, m in near]}


CELLS_KEY = ("dashboard", "knn", "cells")  # annotations (entity_type, entity_key, attribute) of the checked cells


def cells(conn) -> list[tuple[str, str]]:
    """The checked (point or `set:<name>`, mode) cells: the latest annotation row (JSON list of pairs)."""
    import json

    row = conn.execute("SELECT comment FROM annotations WHERE entity_type = ? AND entity_key = ? AND attribute = ? "
                       "ORDER BY id DESC LIMIT 1", CELLS_KEY).fetchone()
    return [tuple(c) for c in json.loads(row[0])] if row and row[0] else []


def _in_cells(key: tuple[str, str], sel: list[tuple[str, str]]) -> bool:
    point, mode = key
    return any(m == mode and (p == point or (p.startswith("set:") and point.startswith(p[4:] + ":")))
               for p, m in sel)


def add_knn(trips: dict[str, dict[tuple[str, str], dict[str, Any]]], located: dict[str, tuple[float, float]],
            sel: list[tuple[str, str]] | None = None) -> None:
    """Fill (point, mode) entries of every located place that has no Google trip for them with a KNN range from
    the places within RADIUS_M that do, for the (point, mode) keys in the checked cells `sel` (None: every key).
    A stay place whose building is a place reads the building's trips instead (filter._Trips), so it gets none of
    its own. A place whose row for a key has no time (the transit estimate, a Google row with no route) and no
    routed place within RADIUS_M gets `far_estimate`."""
    train: dict[tuple[str, str], list] = defaultdict(list)
    for pk, ts in list(trips.items()):
        if pk not in located:
            continue
        for key, t in ts.items():
            if t["provider"] == "google" and t["minutes"] is not None and (sel is None or _in_cells(key, sel)):
                train[key].append((pk, *located[pk], t["minutes"]))
    for key, rows in train.items():
        grid = index(rows)
        for pk, (lat, lon) in located.items():
            base = pk.split("~", 1)[0]
            if base != pk and base in located:
                continue
            cur = trips[pk].get(key) if pk in trips else None
            if cur is not None and cur["provider"] == "google" and cur["minutes"] is not None:
                continue
            near = neighbours(rows, lat, lon, exclude=pk, grid=grid)
            if near:
                trips[pk][key] = knn_trip(near, *key)
            elif cur is not None and cur["minutes"] is None:
                # a trip row with no time (transit: the straight line cannot estimate it; or Google found no route):
                # the middle of the K nearest Google times at any distance, an estimate (rules' estimate band). Not
                # where the place has no row for the key at all: a set's far points (a station 15 km off) have
                # routed places only near them, so 'nearest at any distance' would be a different trip entirely
                trips[pk][key] = far_estimate(rows, lat, lon, pk, key, no_route=bool(cur and cur["provider"] == "google"))


def far_estimate(rows: list[tuple[str, float, float, float]], lat: float, lon: float, exclude: str,
                 key: tuple[str, str], k: int = K, no_route: bool = False) -> dict[str, Any] | None:
    """A place with no time for a checked cell and no Google-routed place within RADIUS_M (in practice transit,
    which the straight-line source cannot estimate; also where Google found no route): the middle of the k nearest
    Google times at any distance, `provider` 'estimate' so rules read it through the estimate band
    (rules.EST_PASS_RATIO / EST_FAIL_RATIO), with its neighbours (`far`). Leave-one-out on the box's 424 Google transit
    trips to Downtown Palo Alto where the nearest is over 400 m (2026-11-25): 97 fail, 0 wrongly; 8 pass, 1 wrongly
    (limit 25). `no_route`: Google returned no route for this place (the reason says so)."""
    cos_lat = cos(radians(lat))
    best = sorted(((la - lat) ** 2 + ((lo - lon) * cos_lat) ** 2, pk, la, lo, m)
                  for pk, la, lo, m in rows if pk != exclude)[:k]
    if not best:
        return None
    near = [(haversine_m((lat, lon), (la, lo)) / 1000, pk, m) for _, pk, la, lo, m in best]
    t = knn_trip(near, *key)
    return {**t, "provider": "estimate", "far": True, "no_route": no_route}


def loo_errors(trips: dict[str, dict[tuple[str, str], dict[str, Any]]], located: dict[str, tuple[float, float]],
               keys: list[tuple[str, str]]) -> list[float]:
    """The errors of `loo_samples`."""
    return [e for e, _ in loo_samples(trips, located, keys)]


def loo_samples(trips: dict[str, dict[tuple[str, str], dict[str, Any]]], located: dict[str, tuple[float, float]],
                keys: list[tuple[str, str]]) -> list[tuple[float, float]]:
    """Leave-one-out over the places with their own Google trip for any of `keys` ((point, mode); a set cell passes
    every point of the set, pooled): each is predicted from its other Google-routed places within RADIUS_M (up to
    K), exactly as `add_knn` would, and scored against the range padded by rules.KNN_PAD_MIN: 0 inside it, else the
    minutes outside it. Places with no routed neighbour within RADIUS_M are not scored. Each sample is (error, width
    of the unpadded range): sampling.commute_status reads a narrow range's error from narrow ranges only."""
    from .rules import KNN_PAD_MIN

    errs: list[tuple[float, float]] = []
    for key in keys:
        rows = [(pk, *located[pk], ts[key]["minutes"]) for pk, ts in list(trips.items())
                if pk in located and key in ts and ts[key]["provider"] == "google" and ts[key]["minutes"] is not None]
        grid = index(rows)
        for pk, lat, lon, true in rows:
            near = neighbours(rows, lat, lon, exclude=pk, grid=grid)
            if not near:
                continue
            t = knn_trip(near, *key)
            lo, hi = t["min"] - KNN_PAD_MIN, t["max"] + KNN_PAD_MIN
            errs.append((0.0 if lo <= true <= hi else min(abs(true - lo), abs(true - hi)), t["max"] - t["min"]))
    return errs


P99_MIN_N = 100  # accuracy(): fewer scored places than this and p99 is not reported


def accuracy(errs: list[float]) -> dict[str, Any]:
    """n, median, 95th- and 99th-percentile error (minutes outside the padded range; nearest rank) and % inside it.
    p99 is None below P99_MIN_N places (with fewer it is just the worst one)."""
    if not errs:
        return {"n": 0, "median": None, "p95": None, "p99": None, "inside_pct": None}
    from math import ceil

    s = sorted(errs)
    rank = lambda q: s[max(0, ceil(len(s) * q) - 1)]  # noqa: E731  (nearest-rank percentile)
    return {"n": len(s), "median": round(rank(0.5), 2), "p95": round(rank(0.95), 2),
            "p99": round(rank(0.99), 2) if len(s) >= P99_MIN_N else None,
            "inside_pct": round(100 * sum(1 for e in s if e == 0) / len(s), 1)}
