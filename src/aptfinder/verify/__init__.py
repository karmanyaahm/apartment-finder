"""verify: sample and conflict queues, accuracy per source and field from `checks`."""

from __future__ import annotations

import json
import math
import random
import sqlite3
from collections import defaultdict
from typing import Any

CONFLICT_FIELDS = ["price_month", "beds", "baths", "sqft", "available_from"]
# conflict field -> the stored field a check verifies
CHECK_FIELD = {"price_month": "price"}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def accuracy(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """{(source, field): {correct, n, acc, lo, hi}}; cannot_tell is excluded. One vote per distinct fact:
    the latest check per (listing, field, checked value), so re-checking an unchanged value in a later
    run does not count twice, while a value the source later changed is a new fact.
    Trips are keyed as ('trip:<provider>:<mode>', 'minutes')."""
    rows = conn.execute("""
        SELECT src, field, verdict FROM (
            SELECT COALESCE(o.source, 'trip:' || t.provider || ':' || t.mode) AS src, c.field, c.verdict,
                   ROW_NUMBER() OVER (PARTITION BY
                       COALESCE(o.source || '|' || o.source_id, 'trip|' || c.trip_id), c.field, c.stored_value
                       ORDER BY c.id DESC) AS rn
            FROM checks c
            LEFT JOIN observations o ON o.id = c.observation_id
            LEFT JOIN trips t ON t.id = c.trip_id
        ) WHERE rn = 1 AND verdict != 'cannot_tell'
    """).fetchall()
    tally: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        t = tally[(r["src"], r["field"])]
        t[1] += 1
        t[0] += r["verdict"] == "correct"
    out = {}
    for key, (k, n) in tally.items():
        lo, hi = wilson(k, n)
        out[key] = {"correct": k, "n": n, "acc": k / n, "lo": lo, "hi": hi}
    return out


def low_accuracy(acc: dict, min_samples: int, min_accuracy: float) -> set[tuple[str, str]]:
    return {key for key, a in acc.items() if a["n"] >= min_samples and a["acc"] < min_accuracy}


def current_checks(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, str]]:
    """(source, source_id) -> {field: verdict} for checks still valid against the latest observation."""
    out: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for r in conn.execute("SELECT source, source_id, field, verdict FROM current_checks"):
        out[(r["source"], r["source_id"])][r["field"]] = r["verdict"]
    return out


def _checked_trips(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT DISTINCT trip_id FROM checks WHERE trip_id IS NOT NULL")}


def _round_robin(groups: dict[str, list[Any]], seed: int) -> list[Any]:
    rng = random.Random(seed)
    lists = []
    for key in sorted(groups):
        items = list(groups[key])
        rng.shuffle(items)
        lists.append(items)
    out = []
    while any(lists):
        for lst in lists:
            if lst:
                out.append(lst.pop())
    return out


def sample_queue(conn: sqlite3.Connection, seed: int = 0, include_trips: bool = True) -> list[dict[str, Any]]:
    """Unchecked items drawn evenly across sources (round robin), repeatable for a given seed.
    Listings are grouped by source; routed trips by provider and mode. Straight-line estimates are
    never sampled: they are labelled guesses, and checking them by hand measures nothing."""
    checked = current_checks(conn)
    ever: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in conn.execute("SELECT DISTINCT o.source, o.source_id, c.field FROM checks c "
                          "JOIN observations o ON o.id = c.observation_id"):
        ever[(r["source"], r["source_id"])].add(r["field"])
    groups: dict[str, list[Any]] = defaultdict(list)
    for r in conn.execute("SELECT source, source_id, obs_id, place_key FROM listings ORDER BY source, source_id"):
        key = (r["source"], r["source_id"])
        # in the queue if never checked, or if a field that was checked has since changed
        if key not in ever or not ever[key] <= set(checked.get(key, {})):
            groups[r["source"]].append({"kind": "listing", "source": r["source"], "source_id": r["source_id"],
                                        "obs_id": r["obs_id"], "place_key": r["place_key"]})
    if include_trips:
        tchecked = _checked_trips(conn)
        for r in conn.execute("SELECT id, place_key, point_id, mode, provider FROM trips "
                              "WHERE minutes IS NOT NULL AND provider != 'estimate' ORDER BY id"):
            if r["id"] not in tchecked:
                groups[f"trip:{r['provider']}:{r['mode']}"].append(
                    {"kind": "trip", "trip_id": r["id"], "place_key": r["place_key"], "point_id": r["point_id"],
                     "mode": r["mode"], "provider": r["provider"]})
    return _round_robin(groups, seed)


def _differs(field: str, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    if field == "price_month":
        return abs(a - b) > 0.01 * max(abs(a), abs(b))
    return a != b


def conflict_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Units with listings from more than one source that disagree on a field. A conflict drops out
    once every disagreeing field has a current check on every listing (see the current_checks view)."""
    by_unit: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM listings WHERE unit_key IS NOT NULL"):
        by_unit[r["unit_key"]].append(r)
    checked = current_checks(conn)
    out = []
    for uk, rows in sorted(by_unit.items()):
        if len({r["source"] for r in rows}) < 2:
            continue
        fields = []
        for f in CONFLICT_FIELDS:
            vals = [r[f] for r in rows]
            if any(_differs(f, a, b) for i, a in enumerate(vals) for b in vals[i + 1:]):
                cf = CHECK_FIELD.get(f, f)
                if not all(cf in checked.get((r["source"], r["source_id"]), {}) for r in rows):
                    fields.append(f)
        if fields:
            out.append({"kind": "conflict", "unit_key": uk, "place_key": rows[0]["place_key"], "fields": fields,
                        "listings": [{"source": r["source"], "source_id": r["source_id"], "obs_id": r["obs_id"]}
                                     for r in rows]})
    return out


def agreement(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Cross-source agreement per field over multi-source units (free signal, no manual work)."""
    by_unit: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM listings WHERE unit_key IS NOT NULL"):
        by_unit[r["unit_key"]].append(r)
    out = {f: {"agree": 0, "compared": 0} for f in CONFLICT_FIELDS}
    for rows in by_unit.values():
        if len({r["source"] for r in rows}) < 2:
            continue
        for f in CONFLICT_FIELDS:
            vals = [r[f] for r in rows if r[f] is not None]
            if len(vals) < 2:
                continue
            out[f]["compared"] += 1
            out[f]["agree"] += not any(_differs(f, a, b) for i, a in enumerate(vals) for b in vals[i + 1:])
    return out


def stored_value(row: sqlite3.Row, field: str) -> Any:
    """Value of a field in an observation row (price column or data JSON)."""
    if field == "price":
        return row["price"]
    return json.loads(row["data"]).get(field)
