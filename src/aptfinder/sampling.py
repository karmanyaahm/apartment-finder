"""Which places to route with Google so that KNN ranges (filter.knn) cover the rest.

The places come from a scope (the dashboard: the places the list shows, every place in a walk zone, or the routing
candidates in a walk zone). A pair (cell) is (point id, mode) or (`set:<name>`, mode), as
`trips.compute_all(only=...)` takes it. Three plans, each over the scope's places without a Google trip for the pair:
- `random_plan`: a random `pct` % of the scope (the same places for every pair);
- `cover_plan`: greedy set cover: repeatedly the place that brings the most not-yet-covered scope places within
  `radius_m` of a Google-routed place (places already routed for the pair count as covering);
- `refine_plan`: the places whose goal rule on the pair is needs_check (a KNN range straddling a limit, an estimate
  too close to call), plus the widest `frac` of KNN ranges.
`coverage` counts the scope's places with a Google-routed place (itself or another) within knn.RADIUS_M.
"""

from __future__ import annotations

import random
import sqlite3
from typing import Any

from .filter import CHECK, best_trips, nearest_by_set, routing_candidates
from .filter.knn import RADIUS_M, index, neighbours
from .filter.rules import _minutes_status

Pair = tuple[str, str]


def located(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    return {pk: (la, lo) for pk, la, lo in conn.execute("SELECT place_key, lat, lon FROM places WHERE lat IS NOT NULL")}


def walk_zone_candidates(conn: sqlite3.Connection, res: dict | None = None) -> dict[str, set[str]]:
    """place_key -> goals, for the routing candidates inside a station walk zone (a stay place as its building)."""
    from .areas import in_walk_zone

    loc = located(conn)
    merged: dict[str, set[str]] = {}
    for pk, goals in routing_candidates(conn, res=res).items():
        base = pk.split("~", 1)[0]
        merged.setdefault(base if base in loc else pk, set()).update(goals)
    return {pk: g for pk, g in merged.items() if pk in loc and in_walk_zone(*loc[pk])}


def routed(trips: dict, pair: Pair) -> set[str]:
    """Places with a Google trip for the pair (for a set: to any point of it by that mode)."""
    point, mode = pair
    return {pk for pk, ts in trips.items()
            if any(t["provider"] == "google" and t["minutes"] is not None and k[1] == mode
                   and (k[0].startswith(point[4:] + ":") if point.startswith("set:") else k[0] == point)
                   for k, t in ts.items())}


def coverage(keys: list[str], loc: dict[str, tuple[float, float]], routed_keys: set[str]) -> int:
    """How many of `keys` have a routed place (itself or another) within knn.RADIUS_M."""
    train = [(pk, *loc[pk], 0.0) for pk in routed_keys if pk in loc]
    grid = index(train)
    return sum(1 for pk in keys if pk in loc and (pk in routed_keys or neighbours(
        train, *loc[pk], k=1, radius_km=RADIUS_M / 1000, grid=grid)))


def random_plan(trips: dict, pairs: list[Pair], keys: list[str], pct: float, seed: int | None = None
                ) -> dict[Pair, list[str]]:
    pick = random.Random(seed).sample(sorted(keys), round(len(keys) * pct / 100))
    return {p: sorted(set(pick) - routed(trips, p)) for p in pairs}


def cover_plan(trips: dict, pairs: list[Pair], keys: list[str], loc: dict[str, tuple[float, float]],
               radius_m: float = RADIUS_M) -> dict[Pair, list[str]]:
    """Per pair, a greedy set cover of the scope: picks until every scope place has a routed place within radius_m."""
    import heapq

    keys = [pk for pk in keys if pk in loc]
    train = [(pk, *loc[pk], 0.0) for pk in keys]
    grid = index(train)
    # scope places within radius of each scope place (itself included)
    near = {pk: {q for _, q, _ in neighbours(train, *loc[pk], k=len(train), radius_km=radius_m / 1000, grid=grid)}
            | {pk} for pk in keys}
    out: dict[Pair, list[str]] = {}
    for p in pairs:
        done = routed(trips, p)
        done_train = [(pk, *loc[pk], 0.0) for pk in done if pk in loc]
        dgrid = index(done_train)
        uncovered = {pk for pk in keys if pk not in done and not neighbours(
            done_train, *loc[pk], k=1, radius_km=radius_m / 1000, grid=dgrid)}
        heap = [(-len(near[pk] & uncovered), pk) for pk in keys if pk not in done]
        heapq.heapify(heap)
        picks = []
        while uncovered and heap:
            neg, pk = heapq.heappop(heap)
            gain = len(near[pk] & uncovered)
            if gain == 0:
                continue
            if gain < -neg:  # stale count: push back with the current gain (lazy greedy)
                heapq.heappush(heap, (-gain, pk))
                continue
            picks.append(pk)
            uncovered -= near[pk]
        out[p] = sorted(picks)
    return out


def goal_limits(cfg) -> dict[Pair, list[tuple[str, float]]]:
    """Pair -> [(goal id, limit)] for every commute rule (must-have or nice-to-have) of every goal."""
    out: dict[Pair, list[tuple[str, float]]] = {}
    for gid, g in cfg.goals.items():
        for r in [*g.rules, *g.nice]:
            if r["type"] == "commute_any":
                for o in r["options"]:
                    out.setdefault((r["target"], o["mode"]), []).append((gid, o["max"]))
            elif r["type"] == "near":
                out.setdefault((f"set:{r['set']}", r["mode"]), []).append((gid, r["max"]))
    return out


def _value(trips: dict, pk: str, pair: Pair) -> dict[str, Any] | None:
    point, mode = pair
    t = trips.get(pk, {})
    return nearest_by_set(t).get((point[4:], mode)) if point.startswith("set:") else t.get((point, mode))


def refine_plan(trips: dict, pairs: list[Pair], keys: list[str], cfg, goals_of: dict[str, set[str]] | None = None,
                frac: float = 0.1) -> dict[Pair, list[str]]:
    """Per pair, the places whose rule on the pair is needs_check for a goal (one they are a routing candidate for,
    when `goals_of` is given), then the widest `frac` of KNN ranges."""
    limits = goal_limits(cfg)
    out: dict[Pair, list[str]] = {}
    for pair in pairs:
        done = routed(trips, pair)
        straddle, spreads = [], []
        for pk in keys:
            if pk in done:
                continue
            v = _value(trips, pk, pair)
            if v is None or v.get("minutes") is None:
                continue
            if any((goals_of is None or g in goals_of.get(pk, ())) and _minutes_status(v, lim)[0] == CHECK
                   for g, lim in limits.get(pair, [])):
                straddle.append(pk)
            elif v["provider"] == "knn":
                spreads.append((v["max"] - v["min"], pk))
        spreads.sort(reverse=True)
        out[pair] = sorted(set(straddle) | {pk for _, pk in spreads[:int(len(spreads) * frac)]})
    return out


def summary(conn: sqlite3.Connection, db_path: str, plan: dict[Pair, list[str]], keys: list[str],
            trips: dict | None = None) -> dict[str, Any]:
    """Per pair: places to route, uncached Route Matrix elements (trips.planned_legs) and coverage before -> after
    (scope places with a Google-routed place within knn.RADIUS_M); the total counts a leg shared by pairs once."""
    from .trips import planned_legs

    trips = trips if trips is not None else best_trips(conn)
    loc = located(conn)
    out: dict[str, Any] = {"scope": len(keys), "radius_m": RADIUS_M, "pairs": {}}
    unique: set[tuple] = set()
    for p, pks in plan.items():
        legs = planned_legs(db_path, pks, p) if pks else []
        unique.update(legs)
        done = routed(trips, p)
        out["pairs"]["|".join(p)] = {
            "places": len(pks), "elements": len(legs), "examples": pks[:3],
            "covered_before": coverage(keys, loc, done), "covered_after": coverage(keys, loc, done | set(pks))}
    out["elements"] = len(unique)
    out["modes"] = sorted({m for _, _, m in unique})
    return out


def cell_keys(trips: dict, pair: Pair) -> list[tuple[str, str]]:
    """The (point, mode) trip keys a cell stands for: the point itself, or every point of a set seen in `trips`."""
    point, mode = pair
    if not point.startswith("set:"):
        return [pair]
    return sorted({k for ts in trips.values() for k in ts if k[1] == mode and k[0].startswith(point[4:] + ":")})


def cell_loo(trips: dict, pair: Pair, loc: dict[str, tuple[float, float]]) -> dict[str, Any]:
    """KNN accuracy for a cell by leave-one-out over every place with its own Google route (filter.knn.loo_errors)."""
    from .filter.knn import accuracy, loo_errors

    return accuracy(loo_errors(trips, loc, cell_keys(trips, pair)))


def cell_shown(trips: dict, pair: Pair, keys: list[str]) -> dict[str, Any]:
    """How the scope's places get their time for a cell, by the provider of the value the list shows: their own Google
    route, a KNN range (a routed place within knn.RADIUS_M), or the straight-line estimate (or nothing)."""
    n = {"google": 0, "knn": 0, "estimate": 0}
    for pk in keys:
        v = _value(trips, pk, pair)
        p = v["provider"] if v and v.get("minutes") is not None else None
        n[p if p in ("google", "knn") else "estimate"] += 1
    total = len(keys) or 1
    return {"scope": len(keys), **{f"{k}_pct": round(100 * v / total, 1) for k, v in n.items()}, "counts": n}
