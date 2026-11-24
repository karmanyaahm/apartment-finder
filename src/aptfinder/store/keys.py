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
    result: dict[str, str | None] = {"street": None, "unit": None, "city": None}
    if city:
        result["city"] = _norm_tokens(city)
    if not address:
        return result
    parts = [p.strip() for p in address.split(",")]
    idx = next((i for i, p in enumerate(parts) if re.match(r"^\d+[a-z]?\b", p, re.IGNORECASE)), None)
    if idx is None:
        return result
    street = parts[idx]
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
    return result


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
