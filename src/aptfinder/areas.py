"""Which chunk (station walk zone group) a point falls in, and whether that chunk is being worked now.
Computed on read from config/areas.toml and the station list; nothing is stored."""

from __future__ import annotations

import tomllib
from functools import lru_cache

from . import settings
from .commute.geo import haversine_m
from .config import load_points


@lru_cache(maxsize=1)
def _cfg() -> tuple[tuple[str, ...], float, tuple[tuple[str, float, float], ...], tuple[str, ...]]:
    with open(settings.CONFIG_DIR / "areas.toml", "rb") as f:
        raw = tomllib.load(f)
    stations = {p.name: p for p in load_points().get("caltrain_stations", [])}
    zones = tuple((chunk, stations[n].lat, stations[n].lon)
                  for chunk in raw.get("chunks", []) for n in raw.get("chunk_stations", {}).get(chunk, [])
                  if n in stations)
    return tuple(raw.get("active", [])), float(raw.get("radius_km", 1.54)), zones, tuple(raw.get("chunks", []))


def chunk_of(lat: float | None, lon: float | None) -> str | None:
    """The first chunk (in work order) with a station within the walk radius, else None."""
    if lat is None or lon is None:
        return None
    _, radius, zones, _ = _cfg()
    for chunk, la, lo in zones:
        if haversine_m((lat, lon), (la, lo)) <= radius * 1000:
            return chunk
    return None


def is_active(chunk: str | None) -> bool:
    return chunk is not None and chunk in _cfg()[0]


def zones() -> dict:
    active, radius, z, order = _cfg()
    return {"active": list(active), "radius_km": radius, "order": list(order),
            "zones": [{"chunk": c, "lat": la, "lon": lo} for c, la, lo in z]}
