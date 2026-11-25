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
        if not result["city"]:
            result["city"] = " ".join(toks[last + 1:])
    if idx + 1 < len(parts) and not result["city"]:
        cand = re.split(r"\s+-\s+", parts[idx + 1])[0]
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
    label = (unit or parse_address(address)["unit"] or floor_plan or "").strip().lower()
    label = re.sub(r"[^\w-]", "", label)
    return f"{pkey}/{label}" if label else pkey


def content_hash(data: dict[str, Any], price: float | None, price_basis: str | None,
                 checkin: str | None, checkout: str | None) -> str:
    """Hash of normalized parsed fields plus query dates. Media paths and raw payloads are excluded
    because image URLs expire and would make every run look like a change."""
    core = {k: v for k, v in data.items() if k not in ("media",) and v is not None}
    blob = json.dumps([core, price, price_basis, checkin, checkout], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()
