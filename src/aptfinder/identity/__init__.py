"""identity: dedupe rules that append to `links`. Never overrules a user link; appends only on change."""

from __future__ import annotations

import sqlite3

from ..store import append_link
from ..store.keys import place_key, unit_key


def decide(row: sqlite3.Row | dict) -> tuple[str, str, str, str] | None:
    """(rule, place_key, unit_key, reason) for one listing, or None when nothing identifies it."""
    pk = place_key(row["address"], row["city"], None, None)
    if pk:
        uk = unit_key(pk, row["address"], row["unit"], row["floor_plan"])
        return "addr_unit", pk, uk, f"address {row['address']!r}"
    if row["lat"] is not None and row["lon"] is not None:
        pk = place_key(None, None, row["lat"], row["lon"])
        beds = "" if row["beds"] is None else f"{row['beds']:g}b"
        price = "" if row["price"] is None else f"{row['price']:.0f}"
        uk = f"{pk}/{beds}{price}" if (beds or price) else pk
        return "geo_beds_price", pk, uk, f"no address; lat/lon {row['lat']:.4f},{row['lon']:.4f}"
    return None


def run(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT source, source_id, address, unit, city, floor_plan, lat, lon, beds, price FROM listings"
    ).fetchall()
    last_rule = {
        (r["source"], r["source_id"]): (r["place_key"], r["unit_key"])
        for r in conn.execute(
            "SELECT * FROM (SELECT l.*, ROW_NUMBER() OVER (PARTITION BY source, source_id ORDER BY id DESC)"
            " rn FROM links l WHERE author != 'user') WHERE rn = 1")
    }
    counts = {"listings": len(rows), "linked": 0, "unchanged": 0, "unidentified": 0}
    for r in rows:
        d = decide(r)
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
