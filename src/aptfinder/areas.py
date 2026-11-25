"""Whether a point lies in some Caltrain station's walk zone (config/areas.toml: each `[[box]]` station and
`radius_km`). Computed on read; nothing is stored."""

from __future__ import annotations

import tomllib
from functools import lru_cache

from . import settings
from .commute.geo import haversine_m
from .config import load_points


@lru_cache(maxsize=1)
def _cfg() -> tuple[float, tuple[tuple[str, float, float], ...]]:
    with open(settings.CONFIG_DIR / "areas.toml", "rb") as f:
        raw = tomllib.load(f)
    stations = {p.name: p for p in load_points().get("caltrain_stations", [])}
    names = list(dict.fromkeys(b["station"] for b in raw.get("box", [])))
    return (float(raw.get("radius_km", 1.54)),
            tuple((n, stations[n].lat, stations[n].lon) for n in names if n in stations))


def in_walk_zone(lat: float | None, lon: float | None) -> bool:
    """True when some station's walk radius covers the point. Unknown location: False."""
    if lat is None or lon is None:
        return False
    radius, zs = _cfg()
    return any(haversine_m((lat, lon), (la, lo)) <= radius * 1000 for _, la, lo in zs)


def zones() -> dict:
    radius, zs = _cfg()
    return {"radius_km": radius, "zones": [{"station": n, "lat": la, "lon": lo} for n, la, lo in zs]}
