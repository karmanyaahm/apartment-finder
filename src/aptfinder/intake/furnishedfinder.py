"""Furnished Finder via shahidirfan/furnished-finder-scraper ($0.001 per listing on the free tier).
Trialled 2026-11-24 on Mountain View: 20/20 items had coordinates, monthly rent, beds, baths, an
availability date, minimum stay and property type. Rooms do not say whether the bath is private."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..store.models import ObservationData
from .base import Adapter, Parsed, money


def slug(city: str) -> str:
    return "us--ca--" + re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")


def build_input(cfg: dict[str, Any], areas: list) -> dict[str, Any]:
    return {"startUrls": [{"url": f"https://www.furnishedfinder.com/housing/{slug(a.id)}"} for a in areas],
            "results_wanted": cfg.get("max_items", 200), "max_pages": cfg.get("max_pages", 15)}


def _date(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"([A-Z][a-z]{2})\.?\s+(\d{1,2}),\s+(\d{4})", text)
    if not m:
        return None
    try:
        return datetime.strptime(" ".join(m.groups()), "%b %d %Y").date().isoformat()
    except ValueError:
        return None


def parse(item: dict[str, Any], cfg: dict[str, Any]) -> Parsed | None:
    lid = item.get("listing_id")
    if not lid:
        return None
    cls = (item.get("property_type_class") or "").lower()
    ptype = item.get("property_type") or ""
    is_room = cls == "room" or ptype.lower().startswith("room")
    kind = "room" if is_room else {"house": "house", "condo": "apartment", "apartment": "apartment",
                                   "townhouse": "townhome"}.get(ptype.lower(), "entire_place")
    rent = item.get("rent_amount")
    basis = "month" if rent and "month" in str(rent).lower() else ("night" if rent and "night" in str(rent).lower()
                                                                    else "month")
    avail = _date(item.get("available_on"))
    if item.get("is_available_now") and not avail:
        avail = datetime.now().date().isoformat()
    data = ObservationData(
        city=(item.get("city") or "").replace("-", " ").title() or None,
        lat=item.get("latitude"), lon=item.get("longitude"),
        beds=1.0 if is_room else item.get("bedrooms"), baths=item.get("bathrooms"),
        kind=kind, term="short", furnished=True,
        private_bath=None if is_room else True,  # rooms: unknown until checked on the listing
        available_from=avail, title=item.get("title"),
        description=(item.get("description") or "")[:3000] or None,
        extra={"property_type": ptype, "minimum_stay_days": item.get("minimum_stay_days"),
               "laundry": item.get("laundry_type"), "sleeps": item.get("total_sleeps"),
               "available_label": item.get("available_on"),
               "images": [item["featured_photo"]] if item.get("featured_photo") else []},
    )
    return Parsed(source_id=str(lid), url=item.get("url"), data=data, price=money(rent), price_basis=basis)


ADAPTER = Adapter(name="furnishedfinder", build_input=build_input, parse=parse, area_kind="city")
