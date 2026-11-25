"""Rule evaluators. Each returns (status, reason). Missing input never rejects: it returns needs_check."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PASS, FAIL, CHECK = "pass", "fail", "needs_check"
RANK = {PASS: 0, CHECK: 1, FAIL: 2}

# A straight-line estimate decides a commute rule only when clearly on one side of the threshold.
EST_PASS_RATIO = 0.67
EST_FAIL_RATIO = 1.5
# A KNN range (min..max of up to 3 nearby places' Google times, filter.knn), padded by KNN_PAD_MIN on each side (the
# true time is often just outside it), passes when its padded max is within the limit and fails only when
# KNN_FAIL_MARGIN x its padded min is over it; anything between is needs_check (user, 2026-11-25: on a boundary take
# the version that keeps more places).
KNN_FAIL_MARGIN = 0.9
KNN_PAD_MIN = 0.5


@dataclass
class Ctx:
    """Everything a rule may need beyond the listing row itself."""
    trips: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)  # (point_id, mode) -> best
    nearest: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)  # (set, mode) -> best
    annotations: dict[str, dict[str, Any]] = field(default_factory=dict)  # attribute -> latest
    untrusted: set[str] = field(default_factory=set)  # fields of this listing's source with low accuracy
    commute_rules: list[dict[str, Any]] = field(default_factory=list)  # the goal's hard commute rules (net_commute)


# ---------------------------------------------------------------- net commute: one term per commute rule of a goal
# (a roommate each). A term is the minutes that rule reads: commute_any, its fastest option with a time; near, the
# nearest point of the set by that mode. Unknown terms are never counted as 0.
AGGS = ["total", "mean", "median", "spread"]  # spread: standard deviation, low = everyone's commute is alike


def commute_terms(rules: list[dict[str, Any]], trips: dict[tuple[str, str], dict[str, Any]],
                  nearest: dict[tuple[str, str], dict[str, Any]]) -> list[tuple[float | None, bool]]:
    """(minutes or None, is an estimate) per commute rule, in rule order."""
    out: list[tuple[float | None, bool]] = []
    for r in rules:
        if r["type"] == "commute_any":
            ts = [t for o in r["options"] if (t := trips.get((r["target"], o["mode"]))) and t.get("minutes") is not None]
        elif r["type"] == "near":
            ts = [t for t in [nearest.get((r["set"], r["mode"]))] if t and t.get("minutes") is not None]
        else:
            continue
        best = min(ts, key=lambda t: t["minutes"]) if ts else None
        out.append((best["minutes"], best["provider"] in ("estimate", "knn")) if best else (None, False))
    return out


def aggregate(values: list[float], agg: str) -> float | None:
    """total, mean, median or spread (population standard deviation) of known minutes; None when there are none."""
    if not values:
        return None
    if agg == "total":
        return float(sum(values))
    mean = sum(values) / len(values)
    if agg == "mean":
        return mean
    if agg == "median":
        v = sorted(values)
        n = len(v)
        return float(v[n // 2]) if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2
    if agg == "spread":
        return (sum((x - mean) ** 2 for x in values) / len(values)) ** 0.5
    raise ValueError(f"unknown aggregate {agg!r}")


def _label(rule: dict[str, Any]) -> str:
    t = rule["type"]
    if t == "equals":
        return f"{rule['field']} = {rule['value']}"
    if t == "max":
        return f"{rule['field']} {'<' if rule.get('strict') else '<='} {rule['value']}"
    if t == "min":
        return f"{rule['field']} {'>' if rule.get('strict') else '>='} {rule['value']}"
    if t == "size_options":
        return "size " + " or ".join(f"{o['beds']}bd/{o['min_baths']}+ba" for o in rule["options"])
    if t == "dates_cover":
        return f"available {rule['start']} to {rule['end']}"
    if t == "commute_any":
        target = rule["target"].split(":", 1)[-1]
        return f"to {target}: " + " or ".join(f"{o['mode']} <= {o['max']}" for o in rule["options"])
    if t == "near":
        return f"{rule['mode']} to {rule['set']} <= {rule['max']}"
    if t == "amenity":
        return f"has {rule['name']}"
    if t == "known":
        return f"{rule['field']} known"
    if t == "net_commute":
        return f"net commute ({rule['agg']}) <= {rule['max']}"
    return t


def _untrusted(ctx: Ctx, *fields: str) -> str | None:
    bad = [f for f in fields if f in ctx.untrusted]
    return f"low measured accuracy for {', '.join(bad)} from this source" if bad else None


def _minutes_status(trip: dict[str, Any] | None, limit: float) -> tuple[str, str]:
    if not trip or trip.get("minutes") is None:
        return CHECK, "not computed"
    if trip["provider"] == "knn":
        lo, hi = trip["min"], trip["max"]
        n = len(trip.get("neighbors") or []) or 1
        span = f"{lo:.0f} min" if round(lo) == round(hi) else f"{lo:.0f}-{hi:.0f} min"
        src = f"{n} nearby Google route{'s' if n > 1 else ''}"
        if hi + KNN_PAD_MIN <= limit:
            return PASS, f"{span} ({src})"
        if KNN_FAIL_MARGIN * (lo - KNN_PAD_MIN) > limit:
            return FAIL, f"{span} ({src})"
        return CHECK, f"{src} span {span}, too close to call"
    m, est = trip["minutes"], trip["provider"] == "estimate"
    tag = f"{m:.0f} min{' (estimate)' if est else ''}"
    if not est:
        return (PASS if m <= limit else FAIL), tag
    if m <= limit * EST_PASS_RATIO:
        return PASS, tag
    if m >= limit * EST_FAIL_RATIO:
        return FAIL, tag
    return CHECK, tag + ", too close to call without routing"


def evaluate(rule: dict[str, Any], row: dict[str, Any], ctx: Ctx) -> tuple[str, str]:
    t = rule["type"]
    label = _label(rule)

    if t == "equals":
        f = rule["field"]
        v = row.get(f)
        if v is None:
            return CHECK, f"{label}: {f} unknown"
        if (u := _untrusted(ctx, f)):
            return CHECK, f"{label}: {u}"
        if isinstance(rule["value"], bool):
            v = bool(v)  # SQLite returns JSON booleans as 0/1
        ok = v == rule["value"]
        ev = row.get(f"{f}_evidence")  # restriction: the quote that states the limit
        return (PASS, label) if ok else (FAIL, f"{label}: is {v}" + (f", {ev}" if ev else ""))

    if t in ("max", "min"):
        f = rule["field"]
        v = row.get(f)
        deps = ("price", "beds") if f == "price_per_bed" else ("price",) if f == "price_month" else (f,)
        if v is None:
            return CHECK, f"{label}: {f} unknown"
        if (u := _untrusted(ctx, *deps)):
            return CHECK, f"{label}: {u}"
        lim, strict = rule["value"], rule.get("strict")
        ok = (v < lim if strict else v <= lim) if t == "max" else (v > lim if strict else v >= lim)
        via = f", price from {row['price_from']}" if row.get("price_from") and f.startswith("price") else ""
        return (PASS, f"{label} ({v:,.0f}{via})") if ok else (FAIL, f"{label}: is {v:,.0f}{via}")

    if t == "size_options":
        beds, baths = row.get("beds"), row.get("baths")
        if beds is None:
            return CHECK, f"{label}: beds unknown"
        opts = [o for o in rule["options"] if o["beds"] == beds]
        if not opts:
            if (u := _untrusted(ctx, "beds")):
                return CHECK, f"{label}: {u}"
            return FAIL, f"{label}: is {beds:g}bd"
        if baths is None:
            return CHECK, f"{label}: baths unknown"
        if (u := _untrusted(ctx, "beds", "baths")):
            return CHECK, f"{label}: {u}"
        if any(baths >= o["min_baths"] for o in opts):
            return PASS, f"{beds:g}bd/{baths:g}ba"
        return FAIL, f"{label}: is {beds:g}bd/{baths:g}ba"

    if t == "dates_cover":
        start, end = rule["start"], rule["end"]
        ci, co = row.get("query_checkin"), row.get("query_checkout")
        if ci and co and ci <= start and co >= end:
            return PASS, f"{label} (returned for query {ci} to {co})"
        af, at = row.get("available_from"), row.get("available_to")
        if af is None:
            return CHECK, f"{label}: availability unknown"
        if (u := _untrusted(ctx, "available_from")):
            return CHECK, f"{label}: {u}"
        if af > start:
            return FAIL, f"{label}: available from {af}"
        if at is not None and at < end:
            return FAIL, f"{label}: available only until {at}"
        return PASS, label

    if t == "commute_any":
        results = [(o, *_minutes_status(ctx.trips.get((rule["target"], o["mode"])), o["max"]))
                   for o in rule["options"]]
        detail = "; ".join(f"{o['mode']} {r}" for o, _, r in results)
        if any(s == PASS for _, s, _ in results):
            return PASS, f"{label} ({detail})"
        if all(s == FAIL for _, s, _ in results):
            return FAIL, f"{label}: {detail}"
        return CHECK, f"{label}: {detail}"

    if t == "near":
        s, r = _minutes_status(ctx.nearest.get((rule["set"], rule["mode"])), rule["max"])
        return s, f"{label}: {r}"

    if t == "amenity":
        name = rule["name"].lower()
        ann = ctx.annotations.get(name)
        if ann and ann.get("rating") is not None:
            return (PASS if ann["rating"] >= 3 else FAIL), f"{label}: user rated {ann['rating']}/5"
        am = row.get("amenities")
        if isinstance(am, str):
            am = json.loads(am)
        if am is None:
            return CHECK, f"{label}: amenities unknown"
        words = [w.lower() for w in rule.get("match") or [name]]  # e.g. gym: ["gym", "fitness"]
        excl = [w.lower() for w in rule.get("exclude") or []]  # e.g. parking: ["no parking"]
        hit = next((a for a in am if any(w in a.lower() for w in words) and not any(x in a.lower() for x in excl)),
                   None)
        return (PASS, f"{label}: {hit}") if hit else (FAIL, f"{label}: not listed")

    if t == "known":
        return (PASS, label) if row.get(rule["field"]) is not None else (CHECK, f"{rule['field']} unknown")

    if t == "net_commute":
        terms = commute_terms(ctx.commute_rules, ctx.trips, ctx.nearest)
        if not terms:
            return CHECK, f"{label}: the goal has no commute rules"
        unknown = sum(m is None for m, _ in terms)
        if unknown:
            return CHECK, f"{label}: {unknown} of {len(terms)} commute times not computed"
        v = aggregate([m for m, _ in terms], rule["agg"])
        s, tag = _minutes_status({"minutes": v, "provider": "estimate" if any(e for _, e in terms) else "google"},
                                 rule["max"])
        return s, f"{label}: {tag}"

    raise ValueError(f"unknown rule type {t!r}")


def fact(rule: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """The attribute a rule judges, for showing a failure on that attribute (the dashboard's cards):
    `f` names it (a listing field, or beds / baths / available / commute / near / amenity), `v` is this listing's
    value, and commute rules carry their point (`pt` or `set`) and modes."""
    t = rule["type"]
    if t in ("equals", "max", "min", "known"):
        v = row.get(rule["field"])
        if t == "equals" and isinstance(rule["value"], bool) and v is not None:
            v = bool(v)  # SQLite returns JSON booleans as 0/1
        out = {"f": rule["field"], "v": v}
        if row.get(f"{rule['field']}_evidence"):
            out["ev"] = row[f"{rule['field']}_evidence"]
        if rule.get("strict"):  # a strict limit fails on equality: the card says ">= lim" / "<= lim"
            out["strict"] = True
        if t == "min":  # a floor: the card says "< lim"
            return {**out, "lim": rule["value"], "min": True}
        return {**out, "lim": rule["value"]} if t == "max" else out
    if t == "size_options":
        beds = row.get("beds")
        if beds is None or not any(o["beds"] == beds for o in rule["options"]):
            return {"f": "beds", "v": beds}
        return {"f": "baths", "v": row.get("baths")}
    if t == "dates_cover":
        return {"f": "available", "v": row.get("available_from")}
    if t == "commute_any":
        return {"f": "commute", "pt": rule["target"], "modes": [o["mode"] for o in rule["options"]]}
    if t == "near":
        return {"f": "near", "set": rule["set"], "modes": [rule["mode"]]}
    if t == "amenity":
        return {"f": "amenity", "v": rule["name"]}
    if t == "net_commute":
        return {"f": "net_commute", "agg": rule["agg"], "lim": rule["max"]}
    return {"f": t}


def combine(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: RANK[s]) if statuses else PASS


# ---------------------------------------------------------------- rule shapes (goals edited in the dashboard)
NUMERIC_FIELDS = ["price", "price_month", "price_per_bed", "beds", "baths", "sqft"]
EQUALS_FIELDS: dict[str, list[Any]] = {"term": ["short", "long"], "furnished": [True, False],
                                       "private_bath": [True, False],
                                       "kind": ["apartment", "house", "townhome", "entire_place", "room"],
                                       # who may rent (extract's reading, pooled per place); 'none' = no limit
                                       "restriction": ["none", "income", "senior", "veterans", "student", "employer",
                                                       "other"]}
KNOWN_FIELDS = [*NUMERIC_FIELDS, "furnished", "private_bath", "term", "kind", "available_from", "address",
                "property_name", "restriction"]
# type -> (label, required keys, optional keys): what the editor offers and validate_rule checks
RULE_TYPES: dict[str, tuple[str, list[str], list[str]]] = {
    "max": ("field at most", ["field", "value"], ["strict"]),
    "min": ("field at least", ["field", "value"], ["strict"]),
    "equals": ("field equals", ["field", "value"], []),
    "size_options": ("beds / baths", ["options"], []),
    "dates_cover": ("available for dates", ["start", "end"], []),
    "commute_any": ("commute to a point", ["target", "options"], []),
    "near": ("nearest point of a set", ["set", "mode", "max"], []),
    "amenity": ("amenity", ["name"], ["match", "exclude"]),
    "known": ("field known", ["field"], []),
    "net_commute": ("net commute", ["agg", "max"], []),
}


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_rule(rule: Any, *, modes: list[str], points: set[str], sets: set[str]) -> str | None:
    """Why `rule` cannot be evaluated (a message naming the problem), or None when it is fine. The checks are the
    shapes `evaluate` and `fact` read: a rule that passes never raises there."""
    if not isinstance(rule, dict):
        return "a rule must be an object"
    t = rule.get("type")
    if t not in RULE_TYPES:
        return f"unknown rule type {t!r} (one of {', '.join(RULE_TYPES)})"
    _, req, opt = RULE_TYPES[t]
    missing = [k for k in req if rule.get(k) in (None, "")]
    if missing:
        return f"{t}: missing {', '.join(missing)}"
    extra = set(rule) - {"type", *req, *opt}
    if extra:
        return f"{t}: unknown key {', '.join(sorted(extra))}"
    if t in ("max", "min"):
        if rule["field"] not in NUMERIC_FIELDS:
            return f"{t}: field must be one of {', '.join(NUMERIC_FIELDS)}"
        if not _num(rule["value"]):
            return f"{t}: value must be a number"
        if not isinstance(rule.get("strict", False), bool):
            return f"{t}: strict must be true or false"
    if t == "equals":
        if rule["field"] not in EQUALS_FIELDS:
            return f"equals: field must be one of {', '.join(EQUALS_FIELDS)}"
        if rule["value"] not in EQUALS_FIELDS[rule["field"]] or type(rule["value"]) is not type(
                EQUALS_FIELDS[rule["field"]][0]):
            return f"equals: {rule['field']} must be one of {EQUALS_FIELDS[rule['field']]}"
    if t == "known" and rule["field"] not in KNOWN_FIELDS:
        return f"known: field must be one of {', '.join(KNOWN_FIELDS)}"
    if t == "size_options":
        opts = rule["options"]
        if not isinstance(opts, list) or not opts or not all(
                isinstance(o, dict) and set(o) == {"beds", "min_baths"} and _num(o["beds"]) and _num(o["min_baths"])
                for o in opts):
            return "size_options: options must be a non-empty list of {beds, min_baths} numbers"
    if t == "dates_cover":
        import datetime as _dt
        try:
            a, b = _dt.date.fromisoformat(rule["start"]), _dt.date.fromisoformat(rule["end"])
        except (TypeError, ValueError):
            return "dates_cover: start and end must be dates (YYYY-MM-DD)"
        if a > b:
            return "dates_cover: start is after end"
    if t == "commute_any":
        if rule["target"] not in points:
            return f"commute_any: unknown point {rule['target']!r}"
        opts = rule["options"]
        if not isinstance(opts, list) or not opts or not all(
                isinstance(o, dict) and set(o) == {"mode", "max"} and o["mode"] in modes and _num(o["max"])
                and o["max"] > 0 for o in opts):
            return f"commute_any: options must be a non-empty list of {{mode, max}} (mode one of {', '.join(modes)}, " \
                   "max minutes > 0)"
    if t == "near":
        if rule["set"] not in sets:
            return f"near: unknown point set {rule['set']!r}"
        if rule["mode"] not in modes:
            return f"near: mode must be one of {', '.join(modes)}"
        if not _num(rule["max"]) or rule["max"] <= 0:
            return "near: max must be minutes > 0"
    if t == "net_commute":
        if rule["agg"] not in AGGS:
            return f"net_commute: agg must be one of {', '.join(AGGS)}"
        if not _num(rule["max"]) or rule["max"] <= 0:
            return "net_commute: max must be minutes > 0"
    if t == "amenity":
        if not isinstance(rule["name"], str) or not rule["name"].strip():
            return "amenity: name must be text"
        for k in ("match", "exclude"):
            v = rule.get(k)
            if v is not None and not (isinstance(v, list) and all(isinstance(w, str) and w.strip() for w in v)):
                return f"amenity: {k} must be a list of words"
    return None
