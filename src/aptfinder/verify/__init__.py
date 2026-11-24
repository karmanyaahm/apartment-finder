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


def is_named_building(property_name: str | None, address: str | None) -> bool:
    """A named apartment community: a property name that is not an address (so not a house or a single unit),
    and an address. The set the no-website queue and the dashboard's Pipeline panel count over."""
    import re

    name = (property_name or "").strip()
    return bool(name) and address is not None and not re.match(r"^\d", name)


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
    for r in conn.execute("SELECT source, source_id, obs_id, place_key FROM listings WHERE kind IS NOT 'site_info' "
                          "ORDER BY source, source_id"):
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


def pending_render(conn: sqlite3.Connection) -> dict[str, str]:
    """place_key -> reason, for places with an own site (any `direct` row, retired ones too) that has not been
    rendered yet (`aptf-intake render`: no direct row with extra.platform = 'render'). Avalon sites never need a
    render: their page data gives exact units. A pending render will replace the own-site plans, so a conflict
    there is likely to settle without a hand check."""
    from ..config import load_sites

    avalon = {sid for sid, s in load_sites().items() if s.get("platform") == "avalon"}
    out = {}
    for r in conn.execute("""
            SELECT place_key, GROUP_CONCAT(DISTINCT json_extract(data, '$.extra.site')) AS sites,
                   SUM(json_extract(data, '$.extra.platform') IN ('render', 'avalon')) AS done
            FROM listings WHERE source = 'direct' AND place_key IS NOT NULL GROUP BY place_key"""):
        sites = sorted(set((r["sites"] or "").split(",")) - {""})
        if r["done"] or any(s in avalon for s in sites):
            continue
        out[r["place_key"]] = f"own site {', '.join(sites) or '(unnamed)'} not rendered yet"
    return out


def conflict_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Units with active listings from more than one source that disagree on a field (a retired listing never
    makes a conflict). A conflict drops out once every disagreeing field has a current check on every listing
    (see the current_checks view). Conflicts at a place whose own site awaits a render carry
    `pending_render: True` and a `pending_reason`, and come after the actionable ones."""
    by_unit: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM listings WHERE unit_key IS NOT NULL AND active = 1 "
                          "AND kind IS NOT 'site_info'"):
        by_unit[r["unit_key"]].append(r)
    checked = current_checks(conn)
    pending = pending_render(conn)
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
            pk = rows[0]["place_key"]
            out.append({"kind": "conflict", "unit_key": uk, "place_key": pk, "fields": fields,
                        "pending_render": pk in pending, "pending_reason": pending.get(pk),
                        "listings": [{"source": r["source"], "source_id": r["source_id"], "obs_id": r["obs_id"]}
                                     for r in rows]})
    return sorted(out, key=lambda c: c["pending_render"])  # stable: actionable first, unit order within


def all_conflicts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every open conflict in Verify order: actionable cross-source ones, then text conflicts (a render does not
    settle those), then cross-source ones waiting for an own-site render."""
    cross = conflict_queue(conn)
    return ([c for c in cross if not c["pending_render"]] + text_conflict_queue(conn)
            + [c for c in cross if c["pending_render"]])


def text_conflict_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Listings whose text (read by `aptf-intake extract`) contradicts a value the source stated: the source's
    value is stored, the text's is shown beside it as '<source> (text)'. Drops out once each such field has a
    current check on the listing."""
    checked = current_checks(conn)
    out = []
    for r in conn.execute("SELECT source, source_id, obs_id, place_key, "
                          "json_extract(data, '$.extra.extracted.conflicts') AS c FROM listings "
                          "WHERE json_extract(data, '$.extra.extracted.conflicts') IS NOT NULL ORDER BY source_id"):
        text = json.loads(r["c"])
        open_ = {f: v for f, v in text.items() if f not in checked.get((r["source"], r["source_id"]), {})}
        if open_:
            out.append({"kind": "conflict", "unit_key": None, "place_key": r["place_key"], "fields": sorted(open_),
                        "pending_render": False, "text": open_, "text_label": f"{r['source']} (text)",
                        "listings": [{"source": r["source"], "source_id": r["source_id"], "obs_id": r["obs_id"]}]})
    return out


def no_website_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Named apartment communities with no website known yet: unusual for a real property (a house or a single
    unit is not expected to have one), so worth a look. Each item says how far the search got. Answering
    (a URL, or "none") is an annotation `website` on the place; the item then drops out, and
    `promote-sites` turns a URL you typed into a site like any other lead."""
    from ..config import load_sites
    from ..intake.discover import _records

    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT place_key FROM listings WHERE place_key IS NOT NULL AND "
        "(source = 'direct' OR json_extract(data, '$.extra.website') IS NOT NULL)")}
    have |= {s.get("place_key") for s in load_sites().values()}
    answered = {r[0] for r in conn.execute(
        "SELECT DISTINCT entity_key FROM annotations WHERE entity_type = 'place' AND attribute = 'website'")}
    rec = _records()
    checks = {c["id"]: c for c in rec.get("check", [])}
    status: dict[str, str] = {}
    for m in rec.get("miss", []):
        status[m.get("place_key")] = f"searched ({m.get('model') or 'model?'}): no site. {m.get('note') or ''}".strip()
    for f in rec.get("found", []):
        c = checks.get(f["id"])
        why = c.get("reason") if c else "not fetched yet"
        status[f.get("place_key")] = f"found {f['url']} ({f.get('found_by')}), but {why}"
    out = []
    for r in conn.execute("SELECT place_key, property_name, address, n_listings FROM places "
                          "WHERE property_name IS NOT NULL AND address IS NOT NULL ORDER BY n_listings DESC"):
        name = r["property_name"].strip()
        if r["place_key"] in have or r["place_key"] in answered or not is_named_building(name, r["address"]):
            continue  # a site known, already answered, or an address used as a name (a house or a unit)
        out.append({"kind": "no_website", "place_key": r["place_key"], "name": name, "address": r["address"],
                    "listings": r["n_listings"], "status": status.get(r["place_key"], "not searched yet")})
    return out


def agreement(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Cross-source agreement per field over multi-source units (free signal, no manual work)."""
    by_unit: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM listings WHERE unit_key IS NOT NULL AND active = 1 "
                          "AND kind IS NOT 'site_info'"):
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
