"""Airbnb via tri_angle/airbnb-scraper. Trialled 2026-11-24 on Mountain View: 20/20 items had
coordinates, a monthly price for a 35-night query, room type and a bath summary."""

from __future__ import annotations

import re
from typing import Any

from ..store.models import ObservationData
from .base import Adapter, Parsed, money

BASIS = {"month": "month", "night": "night", "week": "week", "total": "stay"}


def build_input(cfg: dict[str, Any], city: str) -> dict[str, Any]:
    common = {"checkIn": cfg["checkin"], "checkOut": cfg["checkout"], "adults": cfg.get("adults", 1),
              "currency": "USD", "locale": "en-US", "maxResults": cfg.get("max_items", 200)}
    if cfg.get("box"):  # map-box search; `city` is only a run label. The URL needs a place slug (an empty
        # /s/homes path returned 0 items on 2026-11-24); map bounds still restrict the results.
        s, w, n, e = cfg["box"]
        url = (f"https://www.airbnb.com/s/Palo-Alto--CA/homes?ne_lat={n}&ne_lng={e}&sw_lat={s}&sw_lng={w}&search_by_map=true"
               f"&zoom=13&checkin={cfg['checkin']}&checkout={cfg['checkout']}&adults={cfg.get('adults', 1)}")
        return {**common, "startUrls": [{"url": url}]}
    return {**common, "locationQueries": [f"{city}, California"]}


def _num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def parse(item: dict[str, Any], cfg: dict[str, Any]) -> Parsed | None:
    lid = item.get("id")
    if not lid:
        return None
    summary = item.get("sharingConfigTitle") or ""
    sub = [s.lower() for s in (item.get("subDescription") or {}).get("items") or []]
    text = " · ".join([summary] + sub).lower()
    room_type = (item.get("roomType") or "").lower()

    beds = 0.0 if "studio" in text else _num(r"(\d+)\s+bedroom", text)
    baths = _num(r"(\d+(?:\.\d+)?)\s+(?:private |shared |dedicated )?bath", text)
    if room_type.startswith("entire"):
        kind, private_bath = "entire_place", True
    else:
        kind = "room"
        if "private bath" in text or "dedicated bathroom" in text:
            private_bath = True
        elif "shared" in text and "bath" in text:
            private_bath = False
        else:
            private_bath = None
        beds = beds or 1.0  # renting one bedroom

    price = item.get("price") or {}
    qualifier = (price.get("qualifier") or "").lower()
    basis = BASIS.get(qualifier)
    amount = money(price.get("discountedPrice") or price.get("price"))
    if basis is None and amount is not None:
        basis = "stay"  # a total for the queried dates
    amen = [v["title"] for g in item.get("amenities") or [] for v in g.get("values") or []
            if v.get("available") and v.get("title")]
    coords = item.get("coordinates") or {}
    rating = item.get("rating") or {}
    data = ObservationData(
        city=item.get("location"), lat=coords.get("latitude"), lon=coords.get("longitude"),
        beds=beds, baths=baths, kind=kind, term="short", furnished=True, private_bath=private_bath,
        available_from=cfg["checkin"] if item.get("isAvailable") else None,
        available_to=cfg["checkout"] if item.get("isAvailable") else None,
        amenities=amen or None, title=item.get("title"),
        description=(item.get("description") or "")[:3000] or None,
        extra={"room_type": item.get("roomType"), "property_type": item.get("propertyType"),
               "summary": summary, "price_label": price.get("label"),
               "rating": rating.get("guestSatisfaction"), "reviews": rating.get("reviewsCount"),
               "person_capacity": item.get("personCapacity"),
               "images": [i.get("imageUrl") for i in (item.get("images") or [])[:8] if i.get("imageUrl")]
               or ([item["thumbnail"]] if item.get("thumbnail") else [])},
    )
    url = f"https://www.airbnb.com/rooms/{lid}"
    return Parsed(source_id=str(lid), url=url, data=data, price=amount, price_basis=basis,
                  query_checkin=cfg["checkin"] if item.get("isAvailable") else None,
                  query_checkout=cfg["checkout"] if item.get("isAvailable") else None)


ADAPTER = Adapter(name="airbnb", build_input=build_input, parse=parse)
