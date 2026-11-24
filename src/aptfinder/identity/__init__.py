"""identity: dedupe rules that append to `links`. Never overrules a user link; appends only on change."""

from __future__ import annotations

import re
import sqlite3

from ..commute.geo import haversine_m
from ..store import append_link
from ..store.keys import place_key, unit_key

# A listing with only coordinates joins the nearest address-keyed *building* within this distance:
# Craigslist complexes (Novo, Highland Gardens, ...) otherwise never meet their Apartments.com rows.
# Buildings only (a property name or 3+ listings): single-family houses sit closer than 80 m apart.
# Airbnb is excluded: it deliberately offsets map pins, so distance says nothing.
NEAR_M = 80.0
NO_NEAR_MERGE = {"airbnb"}


def stay_key(pk: str, row: sqlite3.Row | dict) -> str | None:
    """A furnished short stay rented by a third party (Blueground, an unnamed operator in its listing template,
    a Furnished Finder host) at a building's address is its own place, `<building>~<operator>`: a different
    landlord, closer to a hotel than to the building's leases. The building's own furnished option is not: it
    comes from the building (source `direct`) on long terms."""
    if row["term"] != "short" or row["source"] == "direct":
        return None
    who = re.sub(r"[^a-z0-9]+", "-", (row["operator"] or row["source"]).lower()).strip("-")
    return f"{pk}~{who}"


def _beds_price(row: sqlite3.Row | dict) -> str:
    beds = "" if row["beds"] is None else f"{row['beds']:g}b"
    price = "" if row["price"] is None else f"{row['price']:.0f}"
    return f"{beds}{price}"


def _unit(pk: str, row: sqlite3.Row | dict) -> str:
    """A listing that names its unit or floor plan joins that unit. In an apartment building (a named community,
    or kind apartment), one that names neither (a Craigslist ad giving only the building's address) is keyed by
    beds and price, so reposts of one ad merge but a studio and a 2-bed are not taken for one unit. A house or
    an unnamed place stays one unit, so two sources quoting it differently still conflict."""
    uk = unit_key(pk, row["address"], row["unit"], row["floor_plan"])
    get = (lambda k: row[k] if k in row.keys() else None) if hasattr(row, "keys") else row.get
    building = bool(get("property_name")) or get("kind") == "apartment"
    if uk == pk and building and (bp := _beds_price(row)):
        return f"{pk}/~{bp}"
    return uk


def decide(row: sqlite3.Row | dict) -> tuple[str, str, str, str] | None:
    """(rule, place_key, unit_key, reason) for one listing, or None when nothing identifies it."""
    pk = place_key(row["address"], row["city"], None, None)
    if pk and (sk := stay_key(pk, row)):
        return ("addr_stay", sk, _unit(sk, row),
                f"third-party stay ({row['operator'] or row['source']}) at {row['address']!r}")
    if pk:
        return "addr_unit", pk, _unit(pk, row), f"address {row['address']!r}"
    if row["lat"] is not None and row["lon"] is not None:
        pk = place_key(None, None, row["lat"], row["lon"])
        bp = _beds_price(row)
        uk = f"{pk}/{bp}" if bp else pk
        return "geo_beds_price", pk, uk, f"no address; lat/lon {row['lat']:.4f},{row['lon']:.4f}"
    return None


def _near_address(decisions: dict, rows: list) -> None:
    """Re-point coordinate-only decisions at the nearest address place within NEAR_M (in place)."""
    pts: dict[str, list[tuple[float, float]]] = {}
    named: set[str] = set()
    for r in rows:
        d = decisions.get((r["source"], r["source_id"]))
        if d and d[0] == "addr_unit" and r["lat"] is not None:
            pts.setdefault(d[1], []).append((r["lat"], r["lon"]))
            if r["property_name"]:
                named.add(d[1])
    centroids = {pk: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v)) for pk, v in pts.items()
                 if pk in named or len(v) >= 3}
    cells: dict[tuple[int, int], list[str]] = {}
    for pk, (la, lo) in centroids.items():
        cells.setdefault((int(la * 500), int(lo * 500)), []).append(pk)  # ~200 m cells
    for r in rows:
        key = (r["source"], r["source_id"])
        d = decisions.get(key)
        if not d or d[0] != "geo_beds_price" or r["source"] in NO_NEAR_MERGE or r["term"] == "short":
            continue
        cx, cy = int(r["lat"] * 500), int(r["lon"] * 500)
        best, best_m = None, NEAR_M
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                for pk in cells.get((cx + i, cy + j), []):
                    m = haversine_m((r["lat"], r["lon"]), centroids[pk])
                    if m <= best_m:
                        best, best_m = pk, m
        if best:
            suffix = d[2].split("/", 1)[1] if "/" in d[2] else ""
            decisions[key] = ("geo_near_address", best, f"{best}/{suffix}" if suffix else best,
                              f"no address; {best_m:.0f} m from address place")


def run(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT source, source_id, address, unit, city, floor_plan, property_name, kind, lat, lon, beds, price, term, "
        "operator "
        "FROM listings"
    ).fetchall()
    last_rule = {
        (r["source"], r["source_id"]): (r["place_key"], r["unit_key"])
        for r in conn.execute(
            "SELECT * FROM (SELECT l.*, ROW_NUMBER() OVER (PARTITION BY source, source_id ORDER BY id DESC)"
            " rn FROM links l WHERE author != 'user') WHERE rn = 1")
    }
    counts = {"listings": len(rows), "linked": 0, "unchanged": 0, "unidentified": 0}
    decisions = {(r["source"], r["source_id"]): decide(r) for r in rows}
    _near_address(decisions, rows)
    for r in rows:
        d = decisions[(r["source"], r["source_id"])]
        if d is None:
            counts["unidentified"] += 1
            continue
        rule, pk, uk, reason = d
        if last_rule.get((r["source"], r["source_id"])) == (pk, uk):
            counts["unchanged"] += 1
            continue
        append_link(conn, rule, r["source"], r["source_id"], pk, uk, reason, commit=False)
        counts["linked"] += 1
    conn.commit()
    return counts
