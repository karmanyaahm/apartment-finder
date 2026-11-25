"""Zumper and PadMapper: one rental inventory (Zumper owns PadMapper; the same listing ids appear on both),
searched per city through Apify actors chosen 2026-11-24 (see design.md):

* Zumper via scrapemind/zumperCom-Scraper with `getDetails`: a building comes with its floor plans
  (`detail_entity_data_floorplan_listings`: name, beds, baths, half baths, sqft, price range, available date,
  status 1 = offered), and the building's own website (`detail_entity_data_homepage`). One row per offered plan;
  a single unit or house is one row.
* PadMapper via bovi/padmapper-scraper: search results only (no detail pages): a listing is a building summary
  (beds, baths, sqft and price as ranges) or a single unit.

A summary that spans several bedroom counts is not an offer of anything in particular, so it is stored as a
`site_info` row (the building: name, address, photos, ranges in extra), never as a plan with a made-up price.
Search results spill into nearby cities: each item is attributed to the city it states, if that is a searched area.
"""

from __future__ import annotations

import re
from typing import Any

from ..store.models import ObservationData
from .base import Adapter, Parsed
from .direct import SITE_INFO
from .discover import clean_url


def _slug(city: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-") + "-ca"


def _date(v: Any) -> str | None:
    """'2026/11/24' or '2026-11-24' -> '2026-11-24'."""
    m = re.match(r"(\d{4})[/-](\d{2})[/-](\d{2})", str(v or ""))
    return "-".join(m.groups()) if m else None


def _baths(full: Any, half: Any) -> float | None:
    if not isinstance(full, (int, float)):
        return None
    return float(full) + (half or 0) / 2


def _stay(provider_url: str | None, lease_type: Any, homepage: str | None = None) -> tuple[str | None, bool]:
    """(operator, furnished short stay) for furnished-stay listings syndicated to Zumper: `lease_type` 10 (seen on
    Blueground units and other 30-day-minimum furnished units), or a Blueground provider URL (some Blueground units
    carry lease_type 1). A building's own `min_lease_days` of 30 alone does not make a stay (El Portal: 30 to 540)."""
    op = "Blueground" if "blueground" in f"{provider_url} {homepage}".lower() else None
    return op, bool(op) or lease_type == 10


def _same(a: Any, b: Any) -> bool:
    return a is not None and a == b


# ------------------------------------------------------------------------------------------------ Zumper
FLOORPLAN_MEDIA = 2  # Zumper media_type: 1 photo, 2 floor plan drawing, 3 video, 4 3D tour


def _drawing(media: list[dict[str, Any]] | None) -> str | None:
    """The first floor plan drawing among a plan's (or a unit's) media, as a Zumper CDN URL."""
    m = next((m for m in media or [] if m.get("media_type") == FLOORPLAN_MEDIA and m.get("media_id")), None)
    return f"https://img.zumpercdn.com/{m['media_id']}/1280x960" if m else None


def _drawing_extra(media: list[dict[str, Any]] | None) -> dict[str, str]:
    d = _drawing(media)
    return {"floorplan_image": d} if d else {}  # absent, not null: rows without one keep their content hash


def zumper_input(cfg: dict[str, Any], areas: list) -> dict[str, Any]:
    return {"Urls": [{"url": f"https://www.zumper.com/apartments-for-rent/{_slug(a.id)}"} for a in areas],
            # no proxy: through Apify's datacenter proxy Zumper served a page without its data (run 2026-11-24:
            # "could not find __PRELOADED_STATE__"); the actor's own default works
            "fullScrape": cfg.get("full_scrape", True), "getDetails": True}


def zumper_parse(item: dict[str, Any], cfg: dict[str, Any]) -> list[Parsed] | None:
    lid = item.get("listing_id")
    if not lid:
        return None
    addr = f"{item.get('address')}, {item.get('city')}, {item.get('state') or 'CA'} {item.get('zipcode') or ''}".strip()
    url = "https://www.zumper.com" + item["url"] if str(item.get("url", "")).startswith("/") else item.get("url")
    photos = [f"https://img.zumpercdn.com/{i}/1280x960" for i in (item.get("image_ids") or [])[:16]]
    homepage = item.get("detail_entity_data_homepage")
    op, short = _stay(item.get("provider_url"), item.get("lease_type"), homepage)
    name = item.get("building_name") or None
    base = dict(address=addr, city=item.get("city"), zip=item.get("zipcode"), lat=item.get("lat"),
                lon=item.get("lng"), property_name=name, operator=op, title=item.get("title"),
                description=(item.get("short_description") or "")[:3000] or None,
                amenities=(item.get("amenity_tags") or []) + (item.get("building_amenity_tags") or []) or None)
    # a furnished-stay operator's link is not the building's website (Blueground's "1600 Villa Street" listing at
    # Avalon Mountain View links theblueground.com): kept as operator_url so promote-sites never takes it
    extra = {"listing_id": lid, "building_id": item.get("building_id"),
             "website": None if op else clean_url(homepage)[0], "operator_url": homepage if op else None,
             "website_raw": homepage, "provider_url": item.get("provider_url"), "brokerage": item.get("brokerage_name"),
             "phone": item.get("phone"),
             "feed": item.get("feed_name"), "padmapper_url": ("https://www.padmapper.com" + item["padmapper_url"])
             if item.get("padmapper_url") else None, "images": photos,
             "price_range": [item.get("min_price"), item.get("max_price")],
             "beds_range": [item.get("min_bedrooms"), item.get("max_bedrooms")],
             "floorplan_count": item.get("floorplan_count"), "lease_type": item.get("lease_type"),
             "min_lease_days": item.get("min_lease_days"), "max_lease_days": item.get("max_lease_days")}
    # status 1 = on the market; a plan with no unit offered and no price (Avalon Towers' B5 on 2026-11-24) is not an offer
    plans = [f for f in item.get("detail_entity_data_floorplan_listings") or [] if f.get("listing_status") == 1
             and (f.get("min_price") or (f.get("unit_data") or {}).get("active"))]
    out = []
    for f in plans:  # a building with its offered floor plans
        ud = f.get("unit_data") or {}
        out.append(Parsed(
            # the plan's own listing id: the item's id is just one of its plans (all 31 buildings, 2026-11-24), so
            # keying by it would rename every row of a building when that plan leaves the market
            source_id=f"plan:{f.get('listing_id')}", url=url, price=f.get("min_price") or None, price_basis="month",
            data=ObservationData(**base, kind="apartment", term="short" if short else "long",
                                 furnished=True if short else None, floor_plan=f.get("title"),
                                 beds=f.get("bedrooms"), baths=_baths(f.get("bathrooms"), f.get("half_bathrooms")),
                                 sqft=f.get("square_feet") or None, available_from=_date(f.get("date_available")),
                                 extra={**extra, "plan_listing_id": f.get("listing_id"),
                                        "baths_full": f.get("bathrooms"), "baths_half": f.get("half_bathrooms"),
                                        "plan_price_max": f.get("max_price"), "units_offered": ud.get("active"),
                                        **_drawing_extra(f.get("media"))})))
    if out:
        return out
    if item.get("detail_entity_data_floorplan_listings") is not None and item.get("floorplan_count", 0) > 1:
        return [_summary(lid, url, base, extra)]  # a building whose plans are all off the market
    # `min_bathrooms` counts full baths only; `min_all_bathrooms` is [full, half] (1090 Mcgregor Way: [2, 1] = 2.5)
    lo, hi = item.get("min_all_bathrooms"), item.get("max_all_bathrooms")
    if lo and hi and len(lo) == len(hi) == 2:
        if lo == hi:
            extra = {**extra, "baths_full": lo[0], "baths_half": lo[1]}
        lo, hi = _baths(*lo), _baths(*hi)
    else:
        lo, hi = item.get("min_bathrooms"), item.get("max_bathrooms")
    extra = {**extra, **_drawing_extra(item.get("detail_entity_data_media"))}  # a single unit's own drawing
    return [_single(lid, url, base, extra, item.get("min_bedrooms"), item.get("max_bedrooms"),
                    lo, hi, item.get("min_square_feet"),
                    item.get("max_square_feet"), item.get("min_price"), item.get("date_available"), op, short,
                    item.get("property_type"))]


KIND = {4: "apartment"}  # Zumper property_type 4 = "Apartment building" (jongoose's actor names it); other codes are
# not documented, so they stay unknown (the code is kept in extra.property_type)


def _single(lid, url, base, extra, bmin, bmax, tmin, tmax, smin, smax, price, avail, op, short, ptype) -> Parsed:
    """A listing without plan details: an offer when it names one bedroom count, else the building summary."""
    if not _same(bmin, bmax):
        return _summary(lid, url, base, extra)
    return Parsed(source_id=str(lid), url=url, price=price or None, price_basis="month",
                  data=ObservationData(**base, kind=KIND.get(ptype), term="short" if short else "long",
                                       furnished=True if short else None, beds=bmin,
                                       baths=tmin if _same(tmin, tmax) else None,
                                       sqft=(smin or None) if _same(smin, smax) else None,
                                       available_from=_date(avail),
                                       extra={**extra, "property_type": ptype,
                                              "price_max": None if extra["price_range"][1] == price
                                              else extra["price_range"][1]}))


def _summary(lid, url, base, extra) -> Parsed:
    return Parsed(source_id=f"building:{extra.get('building_id') or lid}", url=url, price=None, price_basis=None,
                  data=ObservationData(**base, kind=SITE_INFO,
                                       extra={**extra, "note": "building summary (beds and prices as ranges): "
                                                               "not an offer"}))


def city_of(item: dict[str, Any], ids: list[str]) -> str | None:
    c = item.get("city")
    return c if c in ids else None


ZUMPER = Adapter(name="zumper", build_input=zumper_input, parse=zumper_parse, area_kind="city", batch=True,
                 area_of=city_of, required=("lat",))


# --------------------------------------------------------------------------------------------- PadMapper
def padmapper_input(cfg: dict[str, Any], areas: list) -> dict[str, Any]:
    return {"locations": [_slug(a.id) for a in areas], "propertyType": "apartments",
            "maxResults": cfg.get("max_items", 300), "maxPages": cfg.get("max_pages", 20), "includePhotos": True,
            "proxyConfiguration": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]}}


def padmapper_parse(item: dict[str, Any], cfg: dict[str, Any]) -> list[Parsed] | None:
    lid = item.get("listing_id")
    if not lid:
        return None
    addr = f"{item.get('address')}, {item.get('city')}, {item.get('state') or 'CA'} {item.get('zipcode') or ''}".strip()
    base = dict(address=addr, city=item.get("city"), zip=item.get("zipcode"), lat=item.get("latitude"),
                lon=item.get("longitude"), property_name=None, title=item.get("name"),
                amenities=item.get("amenities") or None)
    url = item.get("url")
    # PadMapper's building URL names the building: /buildings/p103602/the-camille-north-apartments-at-2685-...
    m = re.search(r"/buildings/[^/]+/(.+?)-at-\d", url or "")
    if m and m.group(1) != "apartments":  # unnamed buildings read "apartments-at-801-church-st-..."
        base["property_name"] = m.group(1).replace("-", " ").title()
    extra = {"listing_id": lid, "building_id": None, "name": item.get("name"), "phone": item.get("phone"),
             "images": [f"https://img.zumpercdn.com/{i}/1280x960" for i in (item.get("image_ids") or [])[:16]],
             "price_range": [item.get("price_min"), item.get("price_max")],
             "beds_range": [item.get("beds_min"), item.get("beds_max")], "has_fees": item.get("has_fees"),
             "website": None}
    return [_single(lid, url, base, extra, item.get("beds_min"), item.get("beds_max"), item.get("baths_min"),
                    item.get("baths_max"), item.get("square_feet_min"), item.get("square_feet_max"),
                    item.get("price_min"), item.get("date_available"), None, False, item.get("property_type_code"))]


PADMAPPER = Adapter(name="padmapper", build_input=padmapper_input, parse=padmapper_parse, area_kind="city",
                    batch=True, area_of=city_of, required=("lat",))
