"""KNN commute times: a place without its own Google trip for a (point, mode) reads the range of the up to K nearest
places within RADIUS_M that have one (`provider` 'knn'). Derived at read time from stored Google trips; nothing is
written. Only the cells (point or `set:<name>`, mode) checked in the dashboard's Pipeline grid get KNN (`cells`).

A KNN trip is {minutes: the middle neighbour, min, max, neighbors: [{place_key, minutes, km}]}; rules read the range
padded by rules.KNN_PAD_MIN (rules._minutes_status). With no Google neighbour within RADIUS_M the place keeps its
straight-line estimate. A place's own Google trip always wins; a KNN range beats a straight-line estimate.
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
    found = []
    for dy in range(-reach_lat, reach_lat + 1):
        for dx in range(-reach_lon, reach_lon + 1):
            for pk, la, lo, m in grid.get((cy + dy, cx + dx), ()):
                if pk == exclude:
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
    its own."""
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
