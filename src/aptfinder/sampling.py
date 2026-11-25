"""Which places to route with Google so that KNN ranges (filter.knn) cover the rest.

The places come from a scope (the dashboard: the places the list shows, every place in a walk zone, or the routing
candidates anywhere). A pair (cell) is (point id, mode) or (`set:<name>`, mode), as
`trips.compute_all(only=...)` takes it. Three plans, each over the scope's places without a Google trip for the pair:
- `random_plan`: a random `pct` % of the scope (the same places for every pair);
- `cover_plan`: greedy set cover: repeatedly the place that brings the most not-yet-covered scope places within
  `radius_m` of a Google-routed place (places already routed for the pair count as covering);
- `refine_plan`: over the places that can still pass a goal with a commute rule on the pair (apart from commute
  rules, `candidate_goals`), those with a KNN range that straddles that goal's limit (needs_check) or is wider than
  REFINE_MAX_SPREAD_MIN; places with only a straight-line estimate are counted, not picked (cover's job).
`coverage` counts the scope's places with a Google-routed place (itself or another) within knn.RADIUS_M.
`hinges` gives the places whose goal status waits on a commute (needs_check on commute rules only); `commute_status`
is the panel's per-cell table: expected error p90/p99, places still undecided and a verdict.
"""

from __future__ import annotations

import random
import sqlite3
from typing import Any

from .filter import CHECK, best_trips, nearest_by_set, routing_candidates
from .filter.knn import RADIUS_M, index, neighbours
from .filter.rules import _minutes_status

Pair = tuple[str, str]

REFINE_MAX_SPREAD_MIN = 2.0  # refine routes a relevant place whose KNN range (max - min) is wider than this
DONE_P99_MIN = 2.0  # a cell is done for a scope when no place waits on it and the p99 expected error is within this


def located(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    return {pk: (la, lo) for pk, la, lo in conn.execute("SELECT place_key, lat, lon FROM places WHERE lat IS NOT NULL")}


def _base(pk: str, loc: dict) -> str:
    """A stay place (`<building>~<operator>`) is routed as its building when the building is a place."""
    base = pk.split("~", 1)[0]
    return base if base in loc else pk


def candidate_goals(conn: sqlite3.Connection, res: dict | None = None,
                    loc: dict[str, tuple[float, float]] | None = None) -> dict[str, set[str]]:
    """place_key -> the goals some listing there can still pass apart from commute rules (filter.routing_candidates),
    a stay place merged into its building; every located place, in a walk zone or not."""
    loc = loc if loc is not None else located(conn)
    merged: dict[str, set[str]] = {}
    for pk, goals in routing_candidates(conn, res=res).items():
        merged.setdefault(_base(pk, loc), set()).update(goals)
    return {pk: g for pk, g in merged.items() if pk in loc}


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


def goal_cover_plan(trips: dict, pairs: list[Pair], keys: list[str], loc: dict[str, tuple[float, float]], cfg,
                    goals_of: dict[str, set[str]], radius_m: float = RADIUS_M) -> dict[Pair, list[str]]:
    """cover_plan per pair over only the scope's places that can still pass a goal with a commute rule on the pair
    (`goals_of`, candidate_goals): the candidates scope, in a walk zone or not, where a stay outside every walk zone
    needs transit and a lease candidate does not."""
    limits = goal_limits(cfg)
    out: dict[Pair, list[str]] = {}
    for p in pairs:
        gs = {g for g, _ in limits.get(p, [])}
        out.update(cover_plan(trips, [p], [pk for pk in keys if goals_of.get(pk, set()) & gs], loc, radius_m))
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


def hard_limits(cfg) -> dict[str, list[list[tuple[Pair, float]]]]:
    """goal id -> its must-have commute rules, each as its [(pair, limit)] options (a `near` rule has one)."""
    out: dict[str, list[list[tuple[Pair, float]]]] = {}
    for gid, g in cfg.goals.items():
        for r in g.rules:
            if r["type"] == "commute_any":
                out.setdefault(gid, []).append([((r["target"], o["mode"]), o["max"]) for o in r["options"]])
            elif r["type"] == "near":
                out.setdefault(gid, []).append([((f"set:{r['set']}", r["mode"]), r["max"])])
    return out


def refine_plan(trips: dict, pairs: list[Pair], keys: list[str], cfg, goals_of: dict[str, set[str]]
                ) -> tuple[dict[Pair, list[str]], dict[Pair, dict[str, int]]]:
    """Per pair, over the scope's places not routed for it: for each goal the place can still pass apart from commute
    rules (`goals_of`, candidate_goals) with a commute rule on the pair, the place is picked when its KNN range
    straddles that goal's limit (needs_check) or is wider than REFINE_MAX_SPREAD_MIN. An absolute bar, so refine
    converges: a place once routed is never picked again. Places with no KNN range (only a straight-line estimate,
    no routed neighbour within knn.RADIUS_M) are counted in the notes (`no_neighbour`), not picked: cover's job.
    Returns (plan, notes per pair: straddle, wide, no_neighbour)."""
    limits = goal_limits(cfg)
    plan: dict[Pair, list[str]] = {}
    notes: dict[Pair, dict[str, int]] = {}
    for pair in pairs:
        done = routed(trips, pair)
        straddle, wide, far = set(), set(), 0
        for pk in keys:
            lims = [lim for g, lim in limits.get(pair, []) if g in goals_of.get(pk, ())]
            if pk in done or not lims:
                continue
            v = _value(trips, pk, pair)
            if v is None or v.get("minutes") is None or v["provider"] != "knn":
                far += 1
            elif any(_minutes_status(v, lim)[0] == CHECK for lim in lims):
                straddle.add(pk)
            elif v["max"] - v["min"] > REFINE_MAX_SPREAD_MIN:
                wide.add(pk)
        plan[pair] = sorted(straddle | wide)
        notes[pair] = {"straddle": len(straddle), "wide": len(wide), "no_neighbour": far}
    return plan, notes


def hinges(res: dict, trips: dict, cfg, loc: dict[str, tuple[float, float]]) -> dict[str, dict[str, set[Pair]]]:
    """place_key (a stay place as its building) -> goal -> the commute pairs it waits on: the place does not pass the
    goal, and some listing there that decides it is needs_check on must-have commute rules only (every other rule
    passes), so a commute time alone would settle it. The pairs: the options of those commute rules that are
    needs_check (none of the rule's options passing); empty when only the goal's net-commute rule is open."""
    from .filter import PASS, ROUTING_RULES

    rules = hard_limits(cfg)
    out: dict[str, dict[str, set[Pair]]] = {}
    for gid, listings in res["listings"].items():
        passed: set[str] = set()
        open_: set[str] = set()
        for r in listings.values():
            pk = r.get("place_key")
            if not pk or not r.get("decides", True):
                continue
            key = _base(pk, loc)
            if r["status"] == PASS:
                passed.add(key)
            elif r["status"] == CHECK and all(s == PASS or (s == CHECK and t in ROUTING_RULES)
                                              for s, _, t in r["reasons"]):
                open_.add(key)
        for key in open_ - passed:
            ps: set[Pair] = set()
            for opts in rules.get(gid, []):
                st = [(p, _minutes_status(_value(trips, key, p), lim)[0]) for p, lim in opts]
                if not any(s == PASS for _, s in st):
                    ps.update(p for p, s in st if s == CHECK)
            out.setdefault(key, {})[gid] = ps
    return out


def hinge_counts(h: dict[str, dict[str, set[Pair]]], keys: list[str], pairs: list[Pair],
                 plan: dict[Pair, list[str]] | None = None) -> dict[str, Any]:
    """Over the scope `keys`: places waiting on a commute in total, per goal and per pair (`pairs` and every other
    pair some place waits on); with `plan`, also after it if each routed place's time settles its pair (neighbours'
    ranges narrowing too is not counted, so 'after' is an upper bound)."""
    planned = {p: set(v) for p, v in (plan or {}).items()}

    def left(pk: str, goals: dict[str, set[Pair]]) -> dict[str, set[Pair]]:
        # a goal stays open unless it waits on pairs and the plan routes the place for all of them
        return {g: ps for g, ps in goals.items() if not ps or not all(pk in planned.get(p, ()) for p in ps)}

    def count(view: dict[str, dict[str, set[Pair]]]) -> dict[str, Any]:
        by_pair: dict[str, int] = {"|".join(p): 0 for p in pairs}
        by_goal: dict[str, int] = {}
        for goals in view.values():
            for g in goals:
                by_goal[g] = by_goal.get(g, 0) + 1
            for p in set().union(*goals.values()) if goals else ():
                by_pair["|".join(p)] = by_pair.get("|".join(p), 0) + 1
        return {"total": sum(1 for g in view.values() if g), "by_goal": by_goal, "by_pair": by_pair}

    scope = {pk: h[pk] for pk in keys if pk in h}
    out = count(scope)
    if plan is not None:
        out["after"] = count({pk: left(pk, goals) for pk, goals in scope.items()})
    return out


def summary(conn: sqlite3.Connection, db_path: str, plan: dict[Pair, list[str]], keys: list[str],
            trips: dict | None = None, loc: dict[str, tuple[float, float]] | None = None) -> dict[str, Any]:
    """Per pair: places to route, uncached Route Matrix elements (trips.planned_legs) and coverage before -> after
    (scope places with a Google-routed place within knn.RADIUS_M); the total counts a leg shared by pairs once."""
    from .trips import planned_legs

    trips = trips if trips is not None else best_trips(conn)
    loc = loc if loc is not None else located(conn)
    out: dict[str, Any] = {"scope": len(keys), "radius_m": RADIUS_M, "pairs": {}}
    unique: set[tuple] = set()
    for p, pks in plan.items():
        legs = planned_legs(db_path, pks, p, loc) if pks else []
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


def estimate_errors(conn: sqlite3.Connection, keys: list[Pair]) -> list[float]:
    """|straight-line estimate - Google| in minutes over the places with both (latest of each) for the (point, mode)
    trip keys of a cell (cell_keys): what an estimate-only place's time may be off by."""
    if not keys:
        return []
    have: dict[tuple, dict[str, float]] = {}
    q = ("SELECT place_key, point_id, mode, provider, minutes FROM (SELECT t.*, ROW_NUMBER() OVER (PARTITION BY "
         "place_key, point_id, mode, provider ORDER BY id DESC) rn FROM trips t WHERE provider IN ('estimate', "
         "'google') AND point_id = ? AND mode = ?) WHERE rn = 1 AND minutes IS NOT NULL")
    for point, mode in keys:
        for pk, pt, md, prov, m in conn.execute(q, (point, mode)):
            have.setdefault((pk, pt, md), {})[prov] = m
    return [abs(v["estimate"] - v["google"]) for v in have.values() if len(v) == 2]


def _quantile(items: list[tuple[float, float]], q: float) -> float:
    """Nearest-rank quantile of weighted values [(value, weight)]."""
    items = sorted(items)
    total, acc = sum(w for _, w in items), 0.0
    for v, w in items:
        acc += w
        if acc >= q * total - 1e-9:
            return v
    return items[-1][0]


def error_items(trips: dict, pair: Pair, keys: list[str], loo: list[tuple[float, float]], est: list[float]
                ) -> list[tuple[float, float]] | None:
    """The expected error of the scope's places for a cell as weighted values (each place weighs 1): 0 for a place
    with its own Google route; a KNN place spreads over the cell's leave-one-out errors (filter.knn.loo_samples) of
    ranges on its side of REFINE_MAX_SPREAD_MIN (all of them when that side has none); an estimate-only place over
    the cell's |estimate - Google| errors. None when a needed distribution is empty (no measurement: unknown)."""
    n = {"google": 0, "narrow": 0, "wide": 0, "estimate": 0}
    for pk in keys:
        v = _value(trips, pk, pair)
        p = v["provider"] if v and v.get("minutes") is not None else None
        if p == "google":
            n["google"] += 1
        elif p == "knn":
            n["narrow" if v["max"] - v["min"] <= REFINE_MAX_SPREAD_MIN else "wide"] += 1
        else:
            n["estimate"] += 1
    narrow = [e for e, w in loo if w <= REFINE_MAX_SPREAD_MIN] or [e for e, _ in loo]
    wide = [e for e, w in loo if w > REFINE_MAX_SPREAD_MIN] or [e for e, _ in loo]
    items = [(0.0, float(n["google"]))] if n["google"] else []
    for k, dist in (("narrow", narrow), ("wide", wide), ("estimate", est)):
        if n[k]:
            if not dist:
                return None
            items += [(e, n[k] / len(dist)) for e in dist]
    return items


def _p(items: list[tuple[float, float]] | None) -> dict[str, float | None]:
    if items is None or not items:
        return {"p90": None, "p99": None}
    return {"p90": round(_quantile(items, 0.9), 1), "p99": round(_quantile(items, 0.99), 1)}


def commute_status(trips: dict, pairs: list[Pair], keys: list[str], h: dict, loo_of, est_of, plan_cost
                   ) -> dict[str, Any]:
    """The panel's table for one scope: per checked cell, the p90 / p99 expected error over the scope's places
    (error_items), the places waiting on it (hinges) and a verdict: 'done' when none waits and p99 <= DONE_P99_MIN;
    else 'run' with the cheaper of cover (400 m) and refine that picks any place (`plan_cost(method, pair)` ->
    (places, elements)); else 'settled' (no sample would pick a place: what is left is KNN's measured error at places
    refine skips, or a time no sampling decides). A total row pools every cell, and lists the pairs places wait on
    that are not checked. `loo_of(pair)`, `est_of(pair)`: the cell's distributions (cached by the caller)."""
    hc = hinge_counts(h, keys, pairs)
    cells: dict[str, Any] = {}
    pooled: list[tuple[float, float]] | None = []
    for pair in pairs:
        k = "|".join(pair)
        items = error_items(trips, pair, keys, loo_of(pair), est_of(pair))
        pooled = None if items is None or pooled is None else pooled + items
        row = {**_p(items), "undecided": hc["by_pair"].get(k, 0)}
        if not items:
            row["verdict"] = {"state": "unknown", "why": "no measured error yet for this cell (route a sample first)"}
        elif row["undecided"] == 0 and row["p99"] <= DONE_P99_MIN:
            row["verdict"] = {"state": "done"}
        else:
            opts = [(el, n, m) for m in ("cover", "refine") for n, el in [plan_cost(m, pair)] if n]
            if opts:
                el, n, m = min(opts)
                row["verdict"] = {"state": "run", "method": m, "places": n, "elements": el}
            else:
                row["verdict"] = {"state": "settled", "why": "no sample picks a place: the rest is KNN's measured "
                                  "error at places that cannot pass a goal, or ranges narrower than "
                                  f"{REFINE_MAX_SPREAD_MIN:g} min"}
        cells[k] = row
    unticked = {p: n for p, n in hc["by_pair"].items() if n and tuple(p.rsplit("|", 1)) not in set(pairs)}
    return {"scope": len(keys), "cells": cells, "undecided": hc["total"], "by_goal": hc["by_goal"],
            "unticked": unticked, **_p(pooled),
            "done": all(c["verdict"]["state"] == "done" for c in cells.values()) and hc["total"] == 0}
