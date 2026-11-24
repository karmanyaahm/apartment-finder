"""LegSource: the one piece that answers "how many minutes from A to B by walk, bike, drive or transit"."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from .geo import LatLon, haversine_m

BASIC_MODES = ("walk", "bike", "drive", "transit")


@dataclass
class LegResult:
    minutes: float
    distance_m: float | None
    # transit only: sub-steps [{"mode": "walk"|"transit", "minutes", "distance_m", "line"?}]
    steps: list[dict[str, Any]] = field(default_factory=list)


class LegSource(Protocol):
    name: str

    def leg(self, origin: LatLon, dest: LatLon, mode: str, depart: str) -> LegResult | None:
        """None means this source cannot answer (e.g. no transit route)."""
        ...


class StraightLineLegSource:
    """Free rough estimate: straight-line distance times a detour factor at a fixed speed.
    Cannot estimate transit (returns None), so transit-based filters stay needs_check."""

    name = "estimate"
    SPEED_M_PER_MIN = {"walk": 80.0, "bike": 250.0, "drive": 550.0}
    DETOUR = 1.3

    def leg(self, origin: LatLon, dest: LatLon, mode: str, depart: str) -> LegResult | None:
        if mode not in self.SPEED_M_PER_MIN:
            return None
        d = haversine_m(origin, dest) * self.DETOUR
        return LegResult(minutes=round(d / self.SPEED_M_PER_MIN[mode], 1), distance_m=round(d))


def next_departure(depart: str, tz: str = "America/Los_Angeles") -> datetime:
    """'weekday_0830' -> next Tuesday 08:30 local (a representative weekday, always in the future)."""
    kind, hhmm = depart.split("_")
    now = datetime.now(ZoneInfo(tz))
    d = now + timedelta(days=1)
    target_wd = 1 if kind == "weekday" else 5  # Tuesday / Saturday
    while d.weekday() != target_wd:
        d += timedelta(days=1)
    return d.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0, microsecond=0)


class GoogleRoutesLegSource:
    """Google Routes API (computeRoutes). Drive is traffic-unaware ("no traffic")."""

    name = "google"
    URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
    TRAVEL_MODE = {"walk": "WALK", "bike": "BICYCLE", "drive": "DRIVE", "transit": "TRANSIT"}
    FIELDS = ("routes.duration,routes.distanceMeters,routes.legs.steps.travelMode,"
              "routes.legs.steps.staticDuration,routes.legs.steps.distanceMeters,"
              "routes.legs.steps.transitDetails.transitLine.nameShort,"
              "routes.legs.steps.transitDetails.transitLine.name,"
              "routes.legs.steps.transitDetails.transitLine.vehicle.type")

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=30)
        self.calls = 0

    def leg(self, origin: LatLon, dest: LatLon, mode: str, depart: str) -> LegResult | None:
        body: dict[str, Any] = {
            "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
            "destination": {"location": {"latLng": {"latitude": dest[0], "longitude": dest[1]}}},
            "travelMode": self.TRAVEL_MODE[mode],
        }
        if mode == "drive":
            body["routingPreference"] = "TRAFFIC_UNAWARE"
        if mode == "transit":
            body["departureTime"] = next_departure(depart).isoformat()
        self.calls += 1
        resp = self.client.post(self.URL, json=body, headers={
            "X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": self.FIELDS})
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", {})
                detail = f"{err.get('status')}: {err.get('message')}"
            except ValueError:
                detail = resp.text[:300]
            raise RuntimeError(f"Google Routes API HTTP {resp.status_code} ({detail})")
        routes = resp.json().get("routes") or []
        if not routes:
            return None
        r = routes[0]
        steps = []
        if mode == "transit":
            for leg in r.get("legs", []):
                for s in leg.get("steps", []):
                    line = (s.get("transitDetails") or {}).get("transitLine") or {}
                    step = {
                        "mode": "transit" if s.get("travelMode") == "TRANSIT" else "walk",
                        "minutes": _secs(s.get("staticDuration")) / 60,
                        "distance_m": s.get("distanceMeters", 0),
                    }
                    if line:
                        step["line"] = line.get("nameShort") or line.get("name")
                        step["vehicle"] = (line.get("vehicle") or {}).get("type")
                    steps.append(step)
            steps = _merge_walks(steps)
        return LegResult(minutes=round(_secs(r.get("duration")) / 60, 1),
                         distance_m=r.get("distanceMeters"), steps=steps)


def _secs(d: str | None) -> float:
    return float(d.rstrip("s")) if d else 0.0


def _merge_walks(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in steps:
        if out and s["mode"] == "walk" and out[-1]["mode"] == "walk":
            out[-1]["minutes"] += s["minutes"]
            out[-1]["distance_m"] += s["distance_m"]
        else:
            out.append(dict(s))
    return out
