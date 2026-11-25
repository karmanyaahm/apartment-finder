"""What each intake area covers, for the Pipeline panel's map: every area of config/areas.toml with the sources
that search that kind of area, and its shape. A station box is its rectangle (plus the walk circle it was drawn
around); a ZIP or city is the outline of the listings its searches returned (`extra.area` on each observation),
so the map shows what a search really covered, e.g. Craigslist's radius around a ZIP's centre. Computed on read."""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import tomllib
from typing import Any

from . import settings
from .commute.geo import haversine_m
from .config import load_areas


def _hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Convex hull (monotone chain) of (lat, lon) points, as a closed-ring-free list."""
    pts = sorted(set(pts), key=lambda p: (p[1], p[0]))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[1] - o[1]) * (b[0] - o[0]) - (a[0] - o[0]) * (b[1] - o[1])

    lower: list[tuple[float, float]] = []
    upper: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _core(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The points near the area's median centre: within 3x the median distance to it and never beyond 8 km
    (searches return stray listings far away, e.g. a Craigslist post pinned in another city)."""
    if len(pts) < 4:
        return pts
    c = (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts))
    d = [haversine_m(c, p) for p in pts]
    limit = min(3 * statistics.median(d) + 300, 8000)
    return [p for p, x in zip(pts, d) if x <= limit]


def area_map(conn: sqlite3.Connection) -> dict[str, Any]:
    from .intake import ADAPTERS

    areas = load_areas()
    with open(settings.CONFIG_DIR / "areas.toml", "rb") as f:
        radius_m = float(tomllib.load(f).get("radius_km", 1.54)) * 1000
    kinds = {k: sorted(s for s, a in ADAPTERS.items() if a.area_kind == k) for k in ("zip", "city", "box")}
    pts: dict[str, list[tuple[float, float]]] = {}
    srcs: dict[str, set[str]] = {}
    for r in conn.execute(
            "SELECT source, json_extract(data, '$.extra.area') AS area, json_extract(data, '$.lat') AS lat, "
            "json_extract(data, '$.lon') AS lon FROM observations WHERE source <> 'direct' "
            "AND json_extract(data, '$.extra.area') IS NOT NULL AND json_extract(data, '$.lat') IS NOT NULL"):
        pts.setdefault(r["area"], []).append((r["lat"], r["lon"]))
        srcs.setdefault(r["area"], set()).add(r["source"])
    # areas scraped before rows carried `extra.area`: outline the listings whose address names that ZIP or city
    by_addr: dict[str, list[tuple[float, float]]] = {}
    for r in conn.execute("SELECT address, city, lat, lon FROM listings WHERE lat IS NOT NULL"):
        m = re.search(r"\b(9\d{4})\b", r["address"] or "")
        if m:
            by_addr.setdefault(m.group(1), []).append((r["lat"], r["lon"]))
        if r["city"]:
            by_addr.setdefault(r["city"].strip().lower(), []).append((r["lat"], r["lon"]))
    out = []
    for a in areas:
        if a.kind not in kinds:
            continue
        p, outline = pts.get(a.id, []), "searched"
        if not p and a.kind != "box":
            p, outline = by_addr.get(a.id if a.kind == "zip" else a.id.lower(), []), "by address"
        core = _core(p)
        # most of what a search returned lies far apart: it ignored the location (Craigslist did for 95089,
        # returning listings across the Bay Area), so an outline would mislead
        scattered = len(p) >= 8 and len(core) < len(p) / 2
        if scattered:
            core = []
        e: dict[str, Any] = {"kind": a.kind, "id": a.id, "label": a.label or a.id, "listings": len(p),
                             "outside": len(p) - len(core), "scattered": scattered, "outline": outline if p else None,
                             "searched_by": sorted(srcs.get(a.id, [])), "hull": [list(x) for x in _hull(core)]}
        if a.bbox:
            s, w, n, east = a.bbox
            e["bbox"] = a.bbox
            e["station"] = {"lat": (s + n) / 2, "lon": (w + east) / 2, "radius_m": radius_m}
        out.append(e)
    return {"areas": out, "sources": kinds, "radius_m": radius_m}


if __name__ == "__main__":  # quick look: python -m aptfinder.area_map
    from .store import connect

    print(json.dumps(area_map(connect(settings.DB_PATH)), indent=1)[:3000])
