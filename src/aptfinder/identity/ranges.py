"""A building written as a house-number range absorbs the places inside it that share its name or operator.

RentCafe lists University Gardens as '1222-1244 University Dr, Menlo Park, CA 94025' while Zillow and Craigslist
list its townhouses one by one ('1226 University Dr'). A place whose address is a range ('1222-1244', no spaces:
`keys.parse_address` keeps it whole; a spaced hyphen or a comma is a list, read as its first number) absorbs every
place on the same street, city and ZIP whose house number falls inside the range, when the two share a property
name (normalised) or an operator. A range alone is no evidence: on a long street it spans unrelated houses.

Run inside `identity.run` on the rule decisions, before same-site, so it is recomputed every run (idempotent, undone
by answering 'not the same building' on the pair). An absorbed listing's unit is its house number
('<range>/1226', or '<range>/1226-<unit>' when it names its own unit), so two townhouses never become one unit and
the sources listing one townhouse share it. A user link (split, drawer merge) still wins over the rule row.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from typing import Any

from ..store.keys import parse_address
from .same_site import _ok_key, _pair_key

AUTHOR = "auto-merge:address-range"
RANGE_RE = re.compile(r"^(\d+)-(\d+)\s+(\S.*)$")
NUM_RE = re.compile(r"^(\d+)\s+(\S.*)$")
ZIP_RE = re.compile(r"\b(?:ca|california)\.?\s+(\d{5})\b", re.IGNORECASE)
NAME_STOP = {"the", "apartments", "apartment", "apts", "apt", "homes", "townhomes", "townhouses", "residences"}


def norm_name(name: str | None) -> str | None:
    """'University Gardens' = 'the university gardens apartments'; None when nothing is left."""
    toks = re.sub(r"[^a-z0-9]+", " ", (name or "").lower().replace("&", " and ")).split()
    return " ".join(t for t in toks if t not in NAME_STOP) or None


def _get(r: Any, k: str) -> Any:
    try:
        return r[k]
    except (KeyError, IndexError):
        return None


def reading(r: Any, geocodes: dict) -> dict[str, Any] | None:
    """{lo, hi, street, city, zip} of one listing's address: Google's precise reading when there is one (never for a
    range: the geocoder answers one number of it), else the parser. lo == hi for a single house number."""
    addr = r["address"]
    if not addr:
        return None
    p = parse_address(addr, r["city"])
    g = geocodes.get(addr) or {}
    street, city = p["street"], p["city"]
    if street and not RANGE_RE.match(street) and g.get("street") and not g.get("fallback"):
        street, city = g["street"], g.get("city") or city
    m = ZIP_RE.search(addr)
    zip_ = (_get(r, "zip") or (g.get("zip") if not g.get("fallback") else None) or (m.group(1) if m else None))
    if not street or not city:
        return None
    if (m := RANGE_RE.match(street)):
        lo, hi, rest = int(m.group(1)), int(m.group(2)), m.group(3)
        if lo >= hi:
            return None
    elif (m := NUM_RE.match(street)):
        lo = hi = int(m.group(1))
        rest = m.group(2)
    else:
        return None
    return {"lo": lo, "hi": hi, "street": rest, "city": city, "zip": str(zip_) if zip_ else None}


def _answered_no(conn: sqlite3.Connection | None) -> set[str]:
    if conn is None:
        return set()
    out: dict[str, str] = {}
    for r in conn.execute("SELECT entity_key, comment FROM annotations WHERE entity_type = 'place_pair' "
                          "AND attribute = 'same_building' ORDER BY id"):
        out[r["entity_key"]] = (r["comment"] or "").strip().lower()
    return {k for k, v in out.items() if v == "no"}


def candidates(conn: sqlite3.Connection | None, decisions: dict, rows: list, geocodes: dict) -> dict[str, Any]:
    """{'merges': [...], 'near_misses': [...]} over the rule decisions. A merge: a single-number place inside a range
    place on the same street, city and ZIP, sharing a name or operator. A near miss: inside a range on the same street
    and city but no shared name or operator, or a ZIP that differs or is unknown (shown in the dry run, not merged)."""
    from ..commute.geo import haversine_m

    places: dict[str, dict[str, Any]] = defaultdict(lambda: {"reads": set(), "names": set(), "ops": set(),
                                                             "pts": [], "address": None, "n": 0})
    for r in rows:
        d = decisions.get((r["source"], r["source_id"]))
        if not d or not _ok_key(d[1]):
            continue
        p = places[d[1]]
        p["n"] += 1
        p["address"] = p["address"] or r["address"]
        if (rd := reading(r, geocodes)):
            p["reads"].add((rd["lo"], rd["hi"], rd["street"], rd["city"], rd["zip"]))
        if (n := norm_name(r["property_name"])):
            p["names"].add(n)
        if (o := norm_name(_get(r, "operator"))):
            p["ops"].add(o)
        if r["lat"] is not None and r["lon"] is not None:
            p["pts"].append((r["lat"], r["lon"]))
    ranges: dict[tuple[str, str], list[tuple[str, int, int, str | None]]] = defaultdict(list)
    for pk, p in places.items():
        for lo, hi, street, city, zip_ in p["reads"]:
            if lo < hi:
                ranges[(street, city)].append((pk, lo, hi, zip_))
    no = _answered_no(conn)

    def centre(pk: str) -> tuple[float, float] | None:
        v = places[pk]["pts"]
        return (sum(x[0] for x in v) / len(v), sum(x[1] for x in v) / len(v)) if v else None

    best: dict[str, dict[str, Any]] = {}
    near: list[dict[str, Any]] = []
    for pk, p in places.items():
        if any(lo < hi for lo, hi, *_ in p["reads"]):
            continue  # a range place is never absorbed
        for lo, _hi, street, city, zip_ in p["reads"]:
            for rpk, rlo, rhi, rzip in ranges.get((street, city), []):
                if rpk == pk or not rlo <= lo <= rhi:
                    continue
                rp = places[rpk]
                why = sorted(f"name {n!r}" for n in p["names"] & rp["names"]) + \
                    sorted(f"operator {o!r}" for o in p["ops"] & rp["ops"])
                a, b = centre(pk), centre(rpk)
                item = {"merged": pk, "kept": rpk, "number": lo, "range": f"{rlo}-{rhi}", "street": street,
                        "city": city, "zip": zip_, "range_zip": rzip, "address": p["address"],
                        "range_address": rp["address"], "names": sorted(p["names"]), "range_names": sorted(rp["names"]),
                        "evidence": why, "listings": p["n"],
                        "metres": round(haversine_m(a, b)) if a and b else None}
                if not zip_ or not rzip or zip_ != rzip:
                    near.append({**item, "why_not": "ZIP differs or unknown"})
                elif not why:
                    near.append({**item, "why_not": "no shared name or operator"})
                elif _pair_key(pk, rpk) in no:
                    near.append({**item, "why_not": "you answered not the same building"})
                else:
                    cur = best.get(pk)
                    if cur is None or (rhi - rlo, rpk) < (cur["_span"], cur["kept"]):
                        best[pk] = {**item, "_span": rhi - rlo}
    merges = [{k: v for k, v in m.items() if k != "_span"} for m in best.values()]
    merged = {m["merged"] for m in merges}
    near = [n for n in near if n["merged"] not in merged]
    return {"merges": sorted(merges, key=lambda m: (m["kept"], m["number"])),
            "near_misses": sorted(near, key=lambda m: (m["kept"], m["number"]))}


def apply(decisions: dict, merges: list[dict[str, Any]]) -> int:
    """Re-point the absorbed places' rule decisions at the range place (in place). The unit is the house number, with
    the listing's own unit label after it when it names one (not a '~beds+price' key: the address names the unit).
    Returns the listings moved."""
    by = {m["merged"]: m for m in merges}
    n = 0
    for key, d in decisions.items():
        if not d or d[1] not in by:
            continue
        pk, uk = d[1], d[2]
        m = by[pk]
        label = uk[len(pk) + 1:] if uk and uk.startswith(pk + "/") else ""
        unit = f"{m['kept']}/{m['number']}" + (f"-{label}" if label and not label.startswith("~") else "")
        decisions[key] = (AUTHOR, m["kept"], unit,
                          f"{m['address']!r} inside {m['range_address']!r} ({', '.join(m['evidence'])}): "
                          f"{pk} merged into {m['kept']}")
        n += 1
    return n
