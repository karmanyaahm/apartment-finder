"""Commute engine: basic and composed modes over a cached LegSource.

Every single leg goes through the `commute_legs` cache, so composed modes reuse shared legs
(station-to-office legs are fetched once ever). Callers never manage cache state.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .geo import LatLon, haversine_m, round_ll
from .sources import BASIC_MODES, LegResult, LegSource

MODES = BASIC_MODES + ("bike_train_bike", "bike_bus_bike")

CACHE_DDL = """
CREATE TABLE IF NOT EXISTS commute_legs (
    o_lat REAL NOT NULL, o_lon REAL NOT NULL, d_lat REAL NOT NULL, d_lon REAL NOT NULL,
    mode TEXT NOT NULL, depart TEXT NOT NULL, provider TEXT NOT NULL,
    found INTEGER NOT NULL, minutes REAL, distance_m REAL, steps TEXT NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (o_lat, o_lon, d_lat, d_lon, mode, depart, provider)
)
"""


@dataclass
class Station:
    name: str
    lat: float
    lon: float

    @property
    def ll(self) -> LatLon:
        return (self.lat, self.lon)


@dataclass
class Trip:
    minutes: float | None
    distance_m: float | None
    legs: list[dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    depart: str = ""
    approximate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Commute:
    def __init__(self, cache_path: str | Path, source: LegSource, stations: list[Station] | None = None,
                 train: dict[str, Any] | None = None, depart: str = "weekday_0830",
                 k_stations: int = 2, bike_m_per_min: float = 250.0):
        self.source = source
        self.stations = stations or []
        self.train = train or {}
        self.depart = depart
        self.k_stations = k_stations
        self.bike_m_per_min = bike_m_per_min
        # timeout: wait for another process's write (the default 5 s fails a paid leg's cache write under load)
        self._db = sqlite3.connect(str(cache_path), check_same_thread=False, timeout=300)
        self._db.execute(CACHE_DDL)
        self._db.commit()

    # ---- cached single legs -------------------------------------------------
    def leg(self, origin: LatLon, dest: LatLon, mode: str) -> LegResult | None:
        o, d = round_ll(origin), round_ll(dest)
        key = (o[0], o[1], d[0], d[1], mode, self.depart, self.source.name)
        row = self._db.execute(
            "SELECT found, minutes, distance_m, steps FROM commute_legs WHERE o_lat=? AND o_lon=? "
            "AND d_lat=? AND d_lon=? AND mode=? AND depart=? AND provider=?", key).fetchone()
        if self.source.name == "estimate":
            # a leg already routed by a real provider beats a straight-line guess, cached or not (e.g. the
            # station -> workplace bike legs routed once and shared by every place's bike+train estimate)
            routed = self._db.execute(
                "SELECT minutes, distance_m, steps FROM commute_legs WHERE o_lat=? AND o_lon=? AND d_lat=? "
                "AND d_lon=? AND mode=? AND depart=? AND provider != 'estimate' AND found = 1 "
                "ORDER BY fetched_at DESC LIMIT 1", key[:6]).fetchone()
            if routed:
                return LegResult(routed[0], routed[1], json.loads(routed[2]))
        if row:
            return LegResult(row[1], row[2], json.loads(row[3])) if row[0] else None
        if o == d:
            res: LegResult | None = LegResult(0.0, 0.0)
        else:
            res = self.source.leg(o, d, mode, self.depart)
        self._db.execute(
            "INSERT OR REPLACE INTO commute_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            key + (1 if res else 0, res.minutes if res else None, res.distance_m if res else None,
                   json.dumps(res.steps if res else []), datetime.now(timezone.utc).isoformat()))
        self._db.commit()
        return res

    def prefetch(self, legs: list[tuple[LatLon, LatLon, str]], batch: int = 625) -> dict[str, int]:
        """Fill the cache for many single legs with the source's `matrix` (one request per destination and mode, up
        to `batch` origins each), so later `leg` / `trip` calls are cache hits. Legs already cached are skipped; an
        element the source left out stays uncached."""
        todo: dict[tuple[LatLon, str], set[LatLon]] = {}
        for origin, dest, mode in legs:
            o, d = round_ll(origin), round_ll(dest)
            if o == d or self._db.execute(
                    "SELECT 1 FROM commute_legs WHERE o_lat=? AND o_lon=? AND d_lat=? AND d_lon=? AND mode=? "
                    "AND depart=? AND provider=?", (*o, *d, mode, self.depart, self.source.name)).fetchone():
                continue
            todo.setdefault((d, mode), set()).add(o)
        requests = elements = cached = 0
        for (d, mode), origins in sorted(todo.items()):
            olist = sorted(origins)
            for i in range(0, len(olist), batch):
                chunk = olist[i:i + batch]
                res = self.source.matrix(chunk, [d], mode, self.depart)
                requests += 1
                elements += len(chunk)
                now = datetime.now(timezone.utc).isoformat()
                for k, o in enumerate(chunk):
                    if (k, 0) not in res:
                        continue
                    r = res[(k, 0)]
                    self._db.execute(
                        "INSERT OR REPLACE INTO commute_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (*o, *d, mode, self.depart, self.source.name, 1 if r else 0, r.minutes if r else None,
                         r.distance_m if r else None, "[]", now))
                    cached += 1
                self._db.commit()
        return {"requests": requests, "elements": elements, "cached": cached}

    # ---- public API ------------------------------------------------------------
    def trip(self, origin: LatLon, dest: LatLon, mode: str) -> Trip:
        if mode in BASIC_MODES:
            r = self.leg(origin, dest, mode)
            legs = ([{"mode": mode, "minutes": r.minutes, "distance_m": r.distance_m, "steps": r.steps}]
                    if r else [])
            return self._trip(r.minutes if r else None, r.distance_m if r else None, legs)
        if mode == "bike_train_bike":
            return self._bike_train_bike(origin, dest)
        if mode == "bike_bus_bike":
            return self._bike_bus_bike(origin, dest)
        raise ValueError(f"unknown mode {mode!r}")

    def nearest(self, origin: LatLon, points: list[Any], mode: str, k: int,
                ll=lambda p: (p.lat, p.lon)) -> list[tuple[Any, Trip]]:
        """Shortlist by straight line, route to the top k, return sorted by minutes."""
        shortlist = sorted(points, key=lambda p: haversine_m(origin, ll(p)))[: k if k > 0 else None]
        routed = [(p, self.trip(origin, ll(p), mode)) for p in shortlist]
        return sorted(routed, key=lambda pt: (pt[1].minutes is None, pt[1].minutes or 0))

    # ---- composed modes ----------------------------------------------------------
    def _trip(self, minutes, distance, legs, approximate=False) -> Trip:
        return Trip(minutes=minutes, distance_m=distance, legs=legs, provider=self.source.name,
                    depart=self.depart, approximate=approximate or self.source.name == "estimate")

    def _nearest_stations(self, p: LatLon) -> list[Station]:
        return sorted(self.stations, key=lambda s: haversine_m(p, s.ll))[: self.k_stations]

    def ride_minutes(self, a: str, b: str) -> float | None:
        best = None
        for pattern in self.train.get("patterns", {}).values():
            offs = pattern.get("offsets", {})
            if a in offs and b in offs:
                t = abs(offs[a] - offs[b])
                best = t if best is None else min(best, t)
        return best

    def wait_minutes(self, station: str) -> float:
        tph = self.train.get("peak_trains_per_hour", {}).get(station)
        if not tph:
            hw = min((p.get("headway_min", 30) for p in self.train.get("patterns", {}).values()),
                     default=30)
            return hw / 2
        return 60 / tph / 2

    def _bike_train_bike(self, origin: LatLon, dest: LatLon) -> Trip:
        bike = self.trip(origin, dest, "bike")
        best = bike if bike.minutes is not None else None
        for s1 in self._nearest_stations(origin):
            for s2 in self._nearest_stations(dest):
                if s1.name == s2.name:
                    continue
                ride = self.ride_minutes(s1.name, s2.name)
                if ride is None:
                    continue
                a = self.leg(origin, s1.ll, "bike")
                b = self.leg(s2.ll, dest, "bike")
                if not a or not b:
                    continue
                wait = self.wait_minutes(s1.name)
                total = a.minutes + wait + ride + b.minutes
                if best is None or total < best.minutes:
                    legs = [
                        {"mode": "bike", "to": s1.name, "minutes": a.minutes, "distance_m": a.distance_m},
                        {"mode": "wait", "at": s1.name, "minutes": wait},
                        {"mode": "train", "from": s1.name, "to": s2.name, "minutes": ride},
                        {"mode": "bike", "from": s2.name, "minutes": b.minutes, "distance_m": b.distance_m},
                    ]
                    best = self._trip(round(total, 1), None, legs)
        return best or self._trip(None, None, [])

    def _bike_bus_bike(self, origin: LatLon, dest: LatLon) -> Trip:
        """Google's transit route with walking legs re-timed as bike legs. Approximate."""
        bike = self.trip(origin, dest, "bike")
        best = bike if bike.minutes is not None else None
        t = self.leg(origin, dest, "transit")
        if t and t.steps:
            legs, total = [], 0.0
            for s in t.steps:
                if s["mode"] == "walk":
                    m = (s.get("distance_m") or 0) / self.bike_m_per_min
                    legs.append({"mode": "bike", "minutes": round(m, 1), "distance_m": s.get("distance_m"),
                                 "note": "re-timed walking leg"})
                else:
                    m = s["minutes"]
                    legs.append(dict(s))
                total += m
            # waiting/transfer time is in the route duration but not in the step durations
            wait = max(0.0, t.minutes - sum(s["minutes"] for s in t.steps))
            if wait:
                legs.append({"mode": "wait", "minutes": round(wait, 1), "note": "transfers and waits"})
                total += wait
            if best is None or total < best.minutes:
                best = self._trip(round(total, 1), t.distance_m, legs, approximate=True)
        return best or self._trip(None, None, [])
