"""How many units a place's building has (`building_units`), from the best evidence its listings give. Read-time only:
nothing stored. Mainly tells an apartment building from a house (the goal editor's 'apartment building (>= N units)').

Precedence (first that knows wins):
1. a stated total: RentCafe's `extra.units_total` (0 = not given), else a count in a listing's own text
   ('a 44-unit community', 'apartment building with 12 units', 'only two units in the building');
2. a house type: Zillow / Apartments.com single family, Craigslist house / townhouse / duplex, Furnished Finder House,
   Airbnb '... home' -> 1 (duplex 2), unless the floor below says more; unknown when another row says apartment
   building (a Zillow townhome inside an apartmentComplex card);
3. the floor: the most distinct units or floor plans one source lists at the place (a plan has at least one unit),
   or the most homes a Zillow apartmentComplex card groups (`extra.complex_home_count`, the homes matching the
   search, never the complex's total). It is a lower bound (`floor`): a `min` rule passes when it already reaches the
   limit, else needs_check, never fail.
A type that only says 'apartment building' (Google Places, Zillow apartmentComplex, Zumper building, Apartments.com,
RentCafe or an own site) gives no number: the value stays unknown and the card names the type.

`apartment_building` (the goal field `is_apartment_building`): True when the value or floor reaches 5 units, or a
building-level type says apartment building and no listing there gives a house type; False when a house type decided
the value (single-family, townhome, duplex); else None (unknown). Unit-level types (a Craigslist, Airbnb or Furnished
Finder 'apartment') and a stated 2 to 4 units decide nothing.
"""

from __future__ import annotations

import re
from typing import Any

WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "eleven": 11, "twelve": 12}
_N = r"(\d{1,4}|" + "|".join(WORDS) + r")"
# 'unit' next to a building noun only: the bare 'N apartments' / 'N homes' also matches '1101 Apartments' (a name),
# 'two homes', '1/1 units: $1,999' and 'will add an additional 79 apartments' (2026-11-25 trial on the live data)
TEXT_PATTERNS = [
    re.compile(_N + r"[- ]units?[- ](?:apartment |residential |condo )?(?:building|complex|community|property|"
                    r"triplex|fourplex|duplex|bldg|apartment community)\b", re.I),
    re.compile(r"\b(?:building|complex|community|property) (?:with|of) " + _N + r" (?:apartment |residential )?units\b",
               re.I),
    re.compile(r"\bonly " + _N + r" units (?:in|on) the (?:building|property|lot)\b", re.I),
]
_SF, _TH = (1, "single-family"), (1, "townhome")
HOUSE_TYPES: dict[str, tuple[str, dict[Any, tuple[int, str]]]] = {
    # source -> (extra key, or 'kind' for the row's kind) -> value -> (units, what the card calls it)
    "zillow": ("kind", {"house": _SF, "townhome": _TH}),  # Zillow propertyType SINGLE_FAMILY / TOWNHOUSE
    "apartments_com": ("property_type", {"SingleFamily": _SF, "Townhouse": _TH}),
    "craigslist": ("housing_type", {"house": _SF, "townhouse": _TH, "duplex": (2, "duplex")}),
    "furnishedfinder": ("property_type", {"House": _SF, "Room - House": _SF}),
    "airbnb": ("property_type", {"Entire home": _SF, "Private room in home": _SF, "Shared room in home": _SF}),
}
# sources and types that say 'apartment' or 'apartment building' without a number (shown, never a value)
APARTMENT_TYPE = {"google_places": ("primary_type", {"apartment_building", "apartment_complex", "condominium_complex"}),
                  "zillow": ("group_type", {"apartmentComplex"}),
                  "zumper": ("property_type", {4}), "padmapper": ("property_type", {4}),
                  "craigslist": ("housing_type", {"apartment", "condo"}),
                  "furnishedfinder": ("property_type", {"Apartment", "Condo", "Room - Condo", "Room - Apartment"}),
                  "airbnb": ("property_type", {"Entire rental unit", "Private room in rental unit", "Entire condo",
                                               "Entire serviced apartment", "Private room in condo"})}
BUILDING_SOURCES = ("rentcafe", "direct", "portfolio", "apartments_com")  # a building's own listing page
SOURCE_ORDER = ["rentcafe", "direct", "portfolio", "apartments_com", "google_places", "zillow", "zumper",
                "padmapper", "craigslist", "furnishedfinder", "airbnb"]


def _num(s: str) -> int:
    return WORDS.get(s.lower()) or int(s)


def text_count(text: str | None) -> tuple[int, str] | None:
    """(units, quote) of a building's unit count stated in a listing's text, or None."""
    for p in TEXT_PATTERNS:
        m = p.search(text or "")
        if m:
            n = _num(m.group(1))
            if n >= 1:
                return n, " ".join(text[max(0, m.start() - 30):m.end() + 10].split())
    return None


def _rank(source: str) -> int:
    return SOURCE_ORDER.index(source) if source in SOURCE_ORDER else len(SOURCE_ORDER)


def place_building_units(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """{value (int or None), floor (a lower bound), house (a house type decided it), type (stated, seen,
    single-family, townhome, duplex, apartment building, apartment), source, evidence} of a place's building, or None when no listing there says anything. `rows`: the place's
    listings (any status, site_info included) with `data` parsed."""
    rows = sorted(rows, key=lambda x: _rank(x["source"]))
    stated, house, apt = None, None, None
    zcount = 0  # the most homes a Zillow apartmentComplex card at the place groups
    plans: dict[str, set[str]] = {}
    for x in rows:
        src, d = x["source"], x.get("data") or {}
        extra = d.get("extra") or {}
        if stated is None and src == "rentcafe" and isinstance(extra.get("units_total"), int) and extra["units_total"] > 0:
            stated = {"value": extra["units_total"], "source": src, "evidence": f"RentCafe: {extra['units_total']} units"}
        if stated is None:
            got = text_count(" ".join(t for t in (d.get("title"), d.get("description")) if t))
            if got:
                stated = {"value": got[0], "source": src, "evidence": f'"{got[1]}"'}
        if house is None and src in HOUSE_TYPES:
            key, m = HOUSE_TYPES[src]
            v = x.get("kind") if key == "kind" else extra.get(key)
            if v in m:
                house = {"value": m[v][0], "name": m[v][1], "source": src, "evidence": f"{key} {v}"}
        if apt is None:
            if src in APARTMENT_TYPE:
                key, vals = APARTMENT_TYPE[src]
                if extra.get(key) in vals:
                    apt = {"source": src, "evidence": f"{key} {extra[key]}",
                           "type": "apartment building" if src in ("google_places", "zillow", "zumper", "padmapper")
                           else "apartment"}
            elif src in BUILDING_SOURCES and not (src == "apartments_com" and extra.get("property_type")):
                apt = {"source": src, "evidence": f"listed as a community on {src}", "type": "apartment building"}
        if src == "zillow" and isinstance(extra.get("complex_home_count"), int):
            zcount = max(zcount, extra["complex_home_count"])
        kind = x.get("kind")
        label = (x.get("unit") or "", x.get("floor_plan") or "")
        if kind not in ("site_info", "room") and any(label):
            plans.setdefault(src, set()).add("|".join(s.strip().lower() for s in label))
    floor = max(((len(v), s) for s, v in plans.items()), default=(0, None), key=lambda t: (t[0], -_rank(t[1])))
    floor_d = {"value": floor[0], "floor": True, "source": floor[1],
               "evidence": f"{floor[0]} units or floor plans listed"} if floor[0] >= 2 else None
    if zcount >= 2 and zcount > floor[0]:
        floor_d = {"value": zcount, "floor": True, "source": "zillow",
                   "evidence": f"Zillow: {zcount} homes listed in the complex"}
    b = _units(stated, house, apt, floor_d)
    if b is not None:
        b["apartment_building"], b["apartment_evidence"] = _apartment_building(b, house, apt)
    return b


def _apartment_building(b: dict[str, Any], house: dict | None, apt: dict | None) -> tuple[bool | None, str]:
    """(True: 5+ units, stated or a floor, or a building-level apartment type with no house type at the place;
    False: a house type decided the value; None otherwise (rule 5: unknown never rejects), its evidence and source)."""
    if b["value"] is not None and b["value"] >= 5:
        return True, building_note(b)
    if b["house"]:
        return False, building_note(b)
    if apt and apt["type"] == "apartment building" and not house:
        return True, f"{apt['evidence']} ({apt['source']})"
    return None, building_note(b)


def _units(stated, house, apt, floor_d) -> dict[str, Any] | None:
    if stated:
        return {**stated, "floor": False, "house": False, "type": "stated"}
    if house and apt and not (floor_d and floor_d["value"] > house["value"]):
        # a house type where another source (or the same Zillow card's apartmentComplex group) says apartment
        # building: contradictory, so unknown (rule 5), never a 1 that fails a rule
        return {"value": None, "floor": False, "house": False, "type": f"{house['name']} or {apt['type']}",
                "source": house["source"] if house["source"] == apt["source"] else f"{house['source']} / {apt['source']}",
                "evidence": f"{house['evidence']} vs {apt['evidence']}"}
    if house and not (floor_d and floor_d["value"] > house["value"]):
        name = house.pop("name")
        return {**house, "floor": False, "house": True, "type": name}
    if floor_d:
        return {**floor_d, "house": False, "type": "seen"}
    if apt:
        return {**apt, "value": None, "floor": False, "house": False}
    return None


def building_units_by_place(conn) -> dict[str, dict[str, Any]]:
    """place_key -> place_building_units, read from the listings view (every row, site_info included), only the
    fields it reads (no full `data` parse)."""
    rows: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("""SELECT place_key, source, kind, unit, floor_plan,
                                    json_extract(data, '$.title') AS title,
                                    json_extract(data, '$.description') AS description,
                                    json_extract(data, '$.extra.units_total') AS units_total,
                                    json_extract(data, '$.extra.property_type') AS property_type,
                                    json_extract(data, '$.extra.housing_type') AS housing_type,
                                    json_extract(data, '$.extra.group_type') AS group_type,
                                    json_extract(data, '$.extra.primary_type') AS primary_type,
                                    json_extract(data, '$.extra.complex_home_count') AS complex_home_count
                             FROM listings WHERE place_key IS NOT NULL"""):
        extra = {k: r[k] for k in ("units_total", "property_type", "housing_type", "group_type", "primary_type",
                                              "complex_home_count")
                 if r[k] is not None}
        rows.setdefault(r["place_key"], []).append(
            {"source": r["source"], "kind": r["kind"], "unit": r["unit"], "floor_plan": r["floor_plan"],
             "data": {"title": r["title"], "description": r["description"], "extra": extra}})
    return {pk: b for pk, rs in rows.items() if (b := place_building_units(rs))}


def building_note(b: dict[str, Any] | None) -> str | None:
    """'"a 44-unit community" (craigslist)', 'RentCafe: 402 units (rentcafe)': the evidence and its source, for
    reasons and hovers."""
    return f"{b['evidence']} ({b['source']})" if b else None

