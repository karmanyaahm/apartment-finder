from __future__ import annotations

import math

LatLon = tuple[float, float]


def haversine_m(a: LatLon, b: LatLon) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def round_ll(p: LatLon, nd: int = 4) -> LatLon:
    return (round(p[0], nd), round(p[1], nd))
