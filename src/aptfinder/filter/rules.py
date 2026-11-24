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


@dataclass
class Ctx:
    """Everything a rule may need beyond the listing row itself."""
    trips: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)  # (point_id, mode) -> best
    nearest: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)  # (set, mode) -> best
    annotations: dict[str, dict[str, Any]] = field(default_factory=dict)  # attribute -> latest
    untrusted: set[str] = field(default_factory=set)  # fields of this listing's source with low accuracy


def _label(rule: dict[str, Any]) -> str:
    t = rule["type"]
    if t == "equals":
        return f"{rule['field']} = {rule['value']}"
    if t == "max":
        return f"{rule['field']} {'<' if rule.get('strict') else '<='} {rule['value']}"
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
    return t


def _untrusted(ctx: Ctx, *fields: str) -> str | None:
    bad = [f for f in fields if f in ctx.untrusted]
    return f"low measured accuracy for {', '.join(bad)} from this source" if bad else None


def _minutes_status(trip: dict[str, Any] | None, limit: float) -> tuple[str, str]:
    if not trip or trip.get("minutes") is None:
        return CHECK, "not computed"
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
        return (PASS, label) if ok else (FAIL, f"{label}: is {v}")

    if t == "max":
        f = rule["field"]
        v = row.get(f)
        deps = ("price", "beds") if f == "price_per_bed" else ("price",) if f == "price_month" else (f,)
        if v is None:
            return CHECK, f"{label}: {f} unknown"
        if (u := _untrusted(ctx, *deps)):
            return CHECK, f"{label}: {u}"
        ok = v < rule["value"] if rule.get("strict") else v <= rule["value"]
        return (PASS, f"{label} ({v:,.0f})") if ok else (FAIL, f"{label}: is {v:,.0f}")

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
        return (PASS, label) if any(name in a.lower() for a in am) else (FAIL, f"{label}: not listed")

    if t == "known":
        return (PASS, label) if row.get(rule["field"]) is not None else (CHECK, f"{rule['field']} unknown")

    raise ValueError(f"unknown rule type {t!r}")


def combine(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: RANK[s]) if statuses else PASS
