"""Airbnb via kaix/airbnb-listing-scraper (since 2026-11-25): a coordinate search around each station box, then the
full listing page of every result (description, amenities, rooms, photos) with the price for the queried dates.
Items of the earlier actor, tri_angle/airbnb-scraper, still parse, so `reparse` of old raw files works."""

from __future__ import annotations

import html
import math
import re
from typing import Any

from ..store.models import ObservationData
from . import apify
from .apify import ActorRun
from .base import Adapter, Parsed, money

# Airbnb words a stay of 28+ nights as "monthly" (2026-11-24 runs on) or "month" (earlier): a price per month,
# not the total for the dates.
BASIS = {"month": "month", "monthly": "month", "night": "night", "week": "week", "total": "stay"}


# A search that does not end on its own is split into 4 quadrants, each searched the same way, down to this depth
# (1 box -> 4 -> 16 searches at most); a quadrant still capped at the last depth leaves the box partial.
MAX_SPLIT_DEPTH = 2
PAGE_SIZE = 18  # Airbnb search results per page; kaix logs "Search: found N listings" per page
SEARCH_PAGE = re.compile(r"Search: found (\d+) listings")


def search_input(cfg: dict[str, Any], bbox: list[float]) -> dict[str, Any]:
    """kaix searches a square of half-width `maxLocationDeviation` around the point and drops results farther than
    that, so the deviation is half the box's diagonal: the corners are reached, and results outside the box are
    attributed by `area_of`."""
    s, w, n, e = bbox
    lat, lon = (s + n) / 2, (w + e) / 2
    half_ns = (n - s) / 2 * 111.0
    half_ew = (e - w) / 2 * 111.0 * math.cos(math.radians(lat))
    return {"latitude": round(lat, 6), "longitude": round(lon, 6),
            "maxLocationDeviation": round(math.hypot(half_ns, half_ew) + 0.05, 2),
            "checkIn": cfg["checkin"], "checkOut": cfg["checkout"], "adults": cfg.get("adults", 1),
            "currency": "USD", "maxListings": cfg.get("max_items", 270), "maxSearchPages": 15,
            "includeReviews": False}


def build_input(cfg: dict[str, Any], areas: list) -> dict[str, Any]:
    """One coordinate search per box (the adapter is not batched)."""
    if len(areas) != 1:
        raise ValueError(f"airbnb searches one box per actor run, not {len(areas)}")
    return search_input(cfg, areas[0].bbox)


def search_ended(log: str | None, n_items: int, cap: int) -> bool:
    """The search ran out of results on its own: its last page was short and the item cap was not reached. The
    item count alone cannot say so: kaix drops results beyond the radius, so a capped search can return fewer."""
    pages = [int(x) for x in SEARCH_PAGE.findall(log or "")]
    return bool(pages) and pages[-1] < PAGE_SIZE and n_items < cap


def quadrants(bbox: list[float]) -> list[list[float]]:
    s, w, n, e = bbox
    ms, mw = (s + n) / 2, (w + e) / 2
    return [[s, w, ms, mw], [s, mw, ms, e], [ms, w, n, mw], [ms, mw, n, e]]


def fetch(cfg: dict[str, Any], areas: list, cap_each: int, max_usd: float, conn=None) -> ActorRun:
    """Search the box; a search that hit the cap is split into quadrants (recursively, to MAX_SPLIT_DEPTH) so the
    whole box gets searched. Returns one run: the items merged by listing id, `complete` when every leaf search
    ended on its own. Items are still attributed to boxes by their coordinates (`area_of`)."""
    from .base import event_cost

    if len(areas) != 1:
        raise ValueError(f"airbnb searches one box per actor run, not {len(areas)}")
    items: dict[Any, dict[str, Any]] = {}
    events: dict[str, int] = {}
    runs: list[ActorRun] = []
    state = {"spent": 0.0, "status": "SUCCEEDED", "complete": True}

    def search(bbox: list[float], depth: int) -> None:
        left = max_usd - state["spent"]
        if left <= 0.02:
            state["status"], state["complete"] = f"BUDGET_REACHED: ${left:.2f} left of ${max_usd:.2f}", False
            return
        run = apify.run_actor(cfg["actor"], search_input(cfg, bbox), max_items=cap_each, max_charge_usd=left,
                              memory_mb=cfg.get("memory_mb"), with_log=True)
        runs.append(run)
        state["spent"] += event_cost(cfg["actor"], run.charged_events) or (run.cost_usd or 0.0)
        for k, v in (run.charged_events or {}).items():
            events[k] = events.get(k, 0) + v
        for it in run.items:
            items.setdefault(it.get("id") or len(items), it)
        if run.status != "SUCCEEDED":
            state["status"], state["complete"] = run.status, False
        elif not search_ended(run.log, len(run.items), cap_each):
            if depth >= MAX_SPLIT_DEPTH:
                state["complete"] = False
            else:
                for q in quadrants(bbox):
                    search(q, depth + 1)

    search(areas[0].bbox, 0)
    return ActorRun(actor=cfg["actor"], run_id=",".join(r.run_id for r in runs), status=state["status"],
                    items=list(items.values()), cost_usd=round(state["spent"], 4), charged_events=events,
                    console_url=runs[0].console_url if runs else "", complete=state["complete"])


def _num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _beds_baths(text: str) -> tuple[float | None, float | None]:
    beds = 0.0 if "studio" in text else _num(r"(\d+)\s+bedroom", text)
    baths = _num(r"(\d+(?:\.\d+)?)\s+(?:private |shared |dedicated )?bath", text)
    return beds, baths


def _room_bath(text: str) -> bool | None:
    """Private bath of a room: True / False only when the listing says so, else unknown."""
    if re.search(r"private (attached )?(half )?bath|dedicated bath", text):
        return True
    if "shared" in text and "bath" in text:
        return False
    return None


def _coords(item: dict[str, Any]) -> tuple[float | None, float | None]:
    if "coordinates" in item:  # tri_angle
        co = item.get("coordinates") or {}
        return co.get("latitude"), co.get("longitude")
    lo = item.get("location") if isinstance(item.get("location"), dict) else {}
    return lo.get("lat"), lo.get("lng")


def _text(h: str | None) -> str | None:
    if not h:
        return None
    t = re.sub(r"<br\s*/?>", "\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
    return t[:3000] or None


def _parse_kaix(item: dict[str, Any], cfg: dict[str, Any]) -> Parsed | None:
    lid = item.get("id")
    if not lid:
        return None
    summary = item.get("sharingTitle") or ""
    text = " · ".join([summary] + list(item.get("calendarDescriptionItems") or [])).lower()
    room_type = (item.get("roomType") or "").lower()  # entire_home | private_room | shared_room | hotel_room
    beds, baths = _beds_baths(text)
    if room_type == "entire_home":
        kind, private_bath = "entire_place", True
    else:
        kind, private_bath = "room", _room_bath(text)
        beds = beds or 1.0  # renting one bedroom
    price = item.get("price") or {}
    amount = money(price.get("amount"))
    basis = BASIS.get((price.get("qualifier") or "").lower()) or ("stay" if amount is not None else None)
    dated = amount is not None  # the search only returns stays free on the queried dates, priced for them
    amen = [a["title"] for g in item.get("amenitiesGrouped") or [] if g.get("groupTitle") != "Not included"
            for a in g.get("amenities") or [] if a.get("available") and a.get("title")]
    lat, lon = _coords(item)
    city = ((item.get("location") or {}).get("subtitle") or "").split(",")[0].strip() or None
    images = [i.get("baseUrl") for i in (item.get("photoTourImages") or item.get("heroImages") or [])[:8]
              if i.get("baseUrl")]
    data = ObservationData(
        city=city, lat=lat, lon=lon, beds=beds, baths=baths, kind=kind, term="short", furnished=True,
        private_bath=private_bath,
        available_from=cfg["checkin"] if dated else None, available_to=cfg["checkout"] if dated else None,
        amenities=amen or None, title=item.get("name"), description=_text(item.get("htmlDescription")),
        extra={"room_type": item.get("roomType"), "property_type": item.get("propertyType"), "summary": summary,
               "price_label": price.get("label"), "rating": item.get("overallRating"),
               "reviews": item.get("overallReviewCount"), "person_capacity": item.get("personCapacity"),
               "images": images},
    )
    return Parsed(source_id=str(lid), url=f"https://www.airbnb.com/rooms/{lid}", data=data, price=amount,
                  price_basis=basis, query_checkin=cfg["checkin"] if dated else None,
                  query_checkout=cfg["checkout"] if dated else None)


def parse(item: dict[str, Any], cfg: dict[str, Any]) -> Parsed | None:
    if "sharingTitle" in item or isinstance(item.get("location"), dict):
        return _parse_kaix(item, cfg)
    lid = item.get("id")
    if not lid:
        return None
    summary = item.get("sharingConfigTitle") or ""
    sub = [s.lower() for s in (item.get("subDescription") or {}).get("items") or []]
    text = " · ".join([summary] + sub).lower()
    room_type = (item.get("roomType") or "").lower()

    beds, baths = _beds_baths(text)
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
    lat, lon = _coords(item)
    rating = item.get("rating") or {}
    data = ObservationData(
        city=item.get("location"), lat=lat, lon=lon,
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


def area_of(item: dict[str, Any], ids: list[str]) -> str | None:
    """The station box that actually contains the listing (searches spill outside the box)."""
    from ..config import load_areas

    la, lo = _coords(item)
    if la is None or lo is None:
        return None
    for a in load_areas():
        if a.kind == "box" and a.id in ids:
            s, w, n, e = a.bbox
            if s <= la <= n and w <= lo <= e:
                return a.id
    return None


ADAPTER = Adapter(name="airbnb", build_input=build_input, parse=parse, area_kind="box", area_of=area_of, fetch=fetch)
