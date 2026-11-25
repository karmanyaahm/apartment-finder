"""Deterministic keys and normalization. Keys must be stable across rebuilds."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SUFFIXES = {
    "street": "st", "str": "st", "avenue": "ave", "av": "ave", "drive": "dr", "boulevard": "blvd",
    "road": "rd", "lane": "ln", "court": "ct", "place": "pl", "parkway": "pkwy", "circle": "cir",
    "terrace": "ter", "expressway": "expy", "highway": "hwy", "square": "sq", "trail": "trl",
}
DIRECTIONS = {"north": "n", "south": "s", "east": "e", "west": "w"}
UNIT_RE = re.compile(
    r"(?:\b(?:apt|apartment|unit|ste|suite|bldg|building|fl|floor|rm|room)\b\.?|#)\s*([a-z0-9][\w-]*)",
    re.IGNORECASE,
)
# street-type words after normalization: where a street line ends when no comma separates it from the city
STREET_TYPES = set(SUFFIXES.values()) | {"way", "real", "row", "walk", "plz", "plaza", "loop", "aly", "pass", "path",
                                         "cres", "crescent", "run", "xing", "mall"}
STATE_ZIP_RE = re.compile(r"^(ca|california)?\s*(\d{5})?(-\d{4})?$", re.IGNORECASE)
# a complex written as a list of house numbers on one street: '1024, 1026, 1028 Middle Avenue',
# '670,680,690,2333 Eastridge Ave', '2491 2483, 2485, 2489, 2491 Whitney Drive' (the list ends before a word)
NUM_LIST_RE = re.compile(r"\s*(\d+[a-z]?)((?:(?:\s*[,&]\s*|\s+and\s+|\s+-\s+|\s+)\d+[a-z]?)+)(?=\s+[a-z])",
                         re.IGNORECASE)
# Sources whose map pin is not the building: a Craigslist pin is the poster's account or area point (one pin,
# 37.4056, -122.0775, carries 23 posts of 4 buildings). The places view ignores these pins when any other source
# gives the place a position. Airbnb and Furnished Finder pins are approximate too, but their stays never share a
# place with a precisely placed source (2026-11-25: 0 places), so they are not listed.
APPROX_PIN_SOURCES = ("craigslist",)
# a route number belongs to the street ('2615 State Highway 82')
NUMBERED_ROUTES = {"hwy", "route", "rte"}


def house_numbers(address: str | None) -> list[str]:
    """The house numbers a number-list address names, in order ('1024, 1026, 1028 Middle Ave' -> 1024, 1026, 1028);
    [] for any other address."""
    parts = (address or "").split(",")
    idx = next((i for i, p in enumerate(parts) if re.match(r"^\s*\d+[a-z]?\b", p, re.IGNORECASE)), None)
    m = NUM_LIST_RE.match(",".join(parts[idx:])) if idx is not None else None
    return re.findall(r"\d+[a-z]?", m.group(0).lower()) if m else []


def _norm_tokens(text: str) -> str:
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^\w\s-]", " ", text)
    out = []
    for tok in text.split():
        tok = SUFFIXES.get(tok, tok)
        tok = DIRECTIONS.get(tok, tok)
        out.append(tok)
    return " ".join(out)


def parse_address(address: str | None, city: str | None = None) -> dict[str, str | None]:
    """Split a free-form address into normalized street, unit and city.

    Handles "Name, 123 Main St Apt 4, City, CA 94000 - notes" by taking the first comma part that
    starts with a street number as the street line and the following part as the city.
    """
    # the city written in the address wins; the listing's own city field (e.g. a Craigslist area label) is only
    # the fallback when the address has none
    result: dict[str, str | None] = {"street": None, "unit": None, "city": None}
    fallback_city = _norm_tokens(city) if city else None
    if not address:
        result["city"] = fallback_city
        return result
    parts = [p.strip() for p in address.split(",")]
    idx = next((i for i, p in enumerate(parts) if re.match(r"^\d+[a-z]?\b", p, re.IGNORECASE)), None)
    if idx is None:
        result["city"] = fallback_city
        return result
    # a list of house numbers is the first of them ('1024, 1026, 1028 Middle Avenue' -> '1024 Middle Avenue'):
    # before, '1026' was read as a unit in its own comma slot and '1028 Middle Avenue' as the city
    rest = ", ".join(parts[idx:])
    if (m := NUM_LIST_RE.match(rest)):
        parts = parts[:idx] + [p.strip() for p in (m.group(1) + rest[m.end():]).split(",")]
    # a unit in its own comma slot right after the street ("1870 California St, 11, Mountain View")
    if idx + 2 < len(parts) and re.fullmatch(r"#?\s*[a-z]?\d{1,5}[a-z]?", parts[idx + 1], re.IGNORECASE):
        result["unit"] = parts[idx + 1].lstrip("# ").lower()
        parts = parts[:idx + 1] + parts[idx + 2:]
    # "881-881 E El Camino Real": a range of one number is that number ("4180-4190" stays a range)
    street = re.sub(r"^(\d+)-\1\b", r"\1", parts[idx])
    unit_match = UNIT_RE.search(street)
    if unit_match:
        result["unit"] = unit_match.group(1).lower()
        street = (street[: unit_match.start()] + street[unit_match.end():]).strip()
    # drop trailing descriptive text after " - " or " (" in the street part
    street = re.split(r"\s+-\s+|\s*\(", street)[0]
    result["street"] = _norm_tokens(street)
    # no comma before the city ("234 Escuela Avenue Mountain View, CA 94040"): the street ends at its last
    # street-type word and the rest is the city ("Mountain View Ave" keeps its type word last, so stays whole)
    toks = result["street"].split()
    last = max((i for i, tok in enumerate(toks) if tok in STREET_TYPES and i > 1), default=None)
    if last is not None and last < len(toks) - 1 and len(toks) - last - 1 <= 3:
        result["street"] = " ".join(toks[:last + 1])
        rest = toks[last + 1:]
        # a bare unit after the street type ('365 Forest Ave 2A, Palo Alto') is the unit, not the city: no city
        # has a digit in its first word (a ZIP is not a unit)
        if toks[last] in NUMBERED_ROUTES and rest[0].isdigit():
            result["street"], rest = " ".join(toks[:last + 2]), rest[1:]
        elif re.search(r"\d", rest[0]) and not re.fullmatch(r"\d{5}", rest[0]):
            result["unit"] = result["unit"] or rest[0]
            rest = rest[1:]
        if any(re.search(r"\d", t) for t in rest):  # 'C323 Palo C323': not a city, read the next comma part
            rest = []
        if rest and not result["city"]:
            result["city"] = " ".join(rest)
    # the city is the next comma part that is not another street of the same complex ('670 Eastridge Ave,
    # 680 Sharon Park Dr, Menlo Park') nor a state and ZIP
    cand = next((p for p in parts[idx + 1:] if not re.match(r"^\d", p)), None) if not result["city"] else None
    if cand is not None:
        cand = re.split(r"\s+-\s+", cand)[0]
        if not STATE_ZIP_RE.match(cand.strip()):
            result["city"] = _norm_tokens(cand)
    result["city"] = result["city"] or fallback_city
    return result


def same_street(a: str | None, b: str | None) -> bool:
    """Two addresses name the same street line: same street number, and the shorter normalized street (parse_address:
    suffixes and directions abbreviated, unit dropped) is the start of the longer, so '240 Ortega Avenue' = '240 Ortega
    Ave, Mountain View', '707 Leahy' = '707 Leahy St' and '750 Harvard Ave., Unit #3' = '750 Harvard Ave APT 3'.
    Either side without a street number: the first comma parts compared, normalized."""
    sa, sb = parse_address(a or "")["street"], parse_address(b or "")["street"]
    if not sa or not sb:
        return _norm_tokens((a or "").split(",")[0]) == _norm_tokens((b or "").split(",")[0])
    ta, tb = sorted((sa.split(), sb.split()), key=len)
    return ta[0] == tb[0] and tb[:len(ta)] == ta


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def place_key(address: str | None, city: str | None, lat: float | None, lon: float | None) -> str | None:
    p = parse_address(address, city)
    if p["street"]:
        return "p_" + _h(f"{p['street']}|{p['city'] or ''}")
    if lat is not None and lon is not None:
        return "g_" + _h(f"{round(lat, 4):.4f},{round(lon, 4):.4f}")
    return None


def unit_key(pkey: str, address: str | None, unit: str | None, floor_plan: str | None) -> str:
    """The listing's own unit, else its floor plan, else a unit written in the address. A floor-plan row (an own
    site's plan, a RentCafe or Zumper plan) carries the building's address, which may name some other listing's unit
    (850 Roble's site took '850 Roble Ave #C' from the Craigslist ad that named it): that unit is not the plan's."""
    label = (unit or floor_plan or parse_address(address)["unit"] or "").strip().lower()
    label = re.sub(r"[^\w-]", "", label)
    return f"{pkey}/{label}" if label else pkey


def content_hash(data: dict[str, Any], price: float | None, price_basis: str | None,
                 checkin: str | None, checkout: str | None) -> str:
    """Hash of normalized parsed fields plus query dates. Media paths and raw payloads are excluded
    because image URLs expire and would make every run look like a change."""
    core = {k: v for k, v in data.items() if k not in ("media",) and v is not None}
    blob = json.dumps([core, price, price_basis, checkin, checkout], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()
