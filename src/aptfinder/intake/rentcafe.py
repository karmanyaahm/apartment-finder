"""RentCafe (rentcafe.com, Yardi's listing site), searched per city through benthepythondev/rentcafe-apartments-scraper
with property details (chosen 2026-11-25, see design.md). The actor pages through the whole city search (`siteTotal`
is the site's own count) and, per property, gives its floor plans: id, name, beds (0 = studio), baths, sqft, rent
from/to, whether units are available now.

One row per floor plan (`plan:<floorPlanId>`), priced or not: a plan that says "Ask for pricing" is a row with its
beds, baths and sqft and no price (a plan, not an offer; goals judge it for fit only). A plan that is no dwelling
('Office', 'RV Storage', 'Warehouse with Office', which the actor gives as 0 beds, i.e. a studio) is dropped. A
property with no floor plan left is one `site_info` row `building:<propertyId>`. Every item's url is
its rentcafe.com page, which is an aggregator page, not the building's own site, so `extra.website` stays empty unless
the actor ever links elsewhere (`clean_url` refuses rentcafe.com, keeps rentcafewebsite.com).
The search spills into neighbouring cities: an item counts for the city it states.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from ..store.models import ObservationData
from .base import Adapter, Parsed
from .direct import SITE_INFO
from .discover import clean_url
from .zumper import city_of


# a floor plan that is not a home: RentCafe lists offices, storage and parking among some properties' plans, with 0
# beds (read as a studio) and no baths. Only a plan without bedrooms is dropped ('2 Bed with Office' is a home).
NOT_A_HOME = re.compile(r"\b(office|storage|warehouse|parking|garage|retail|commercial)\b", re.I)


def is_home(f: dict[str, Any]) -> bool:
    return bool(f.get("beds")) or not NOT_A_HOME.search(f.get("name") or "")


def rentcafe_input(cfg: dict[str, Any], areas: list) -> dict[str, Any]:
    return {"locations": [f"{a.id}, CA" for a in areas], "maxItems": cfg.get("max_items", 600),
            "includeDetails": True}


def _kind(url: str | None) -> str | None:
    """rentcafe.com/<vertical>/ca/<city>/<slug>/: 'apartments', 'housing' (income-restricted), 'condos', ..."""
    parts = urlsplit(url or "").path.strip("/").split("/")
    return parts[0] if parts and parts[0] else None


def rentcafe_parse(item: dict[str, Any], cfg: dict[str, Any]) -> list[Parsed] | None:
    pid = item.get("propertyId")
    url = item.get("url")
    if not (pid or url):
        return None
    pid = pid or url
    street, city, zipc = item.get("street"), item.get("city"), item.get("zip")
    addr = f"{street}, {city}, {item.get('state') or 'CA'} {zipc or ''}".strip() if street else None
    base = dict(address=addr, city=city, zip=zipc, lat=item.get("latitude"), lon=item.get("longitude"),
                property_name=item.get("name"), title=item.get("name"),
                description=(item.get("description") or "")[:3000] or None,
                amenities=item.get("amenities") or None)
    extra = {"property_id": pid, "website": clean_url(url)[0], "vertical": _kind(url), "phone": item.get("phone"),
             "images": (item.get("images") or [])[:16], "rent_range": [item.get("rentMin"), item.get("rentMax")],
             "beds_range": [item.get("bedsMin"), item.get("bedsMax")], "units_total": item.get("units"),
             "has_available_units": item.get("hasAvailableUnits"), "rent_special": item.get("rentSpecial"),
             "application_fee": item.get("applicationFee"), "pet_deposit": item.get("petDeposit"),
             "pet_rent": item.get("petRent"), "cats_allowed": item.get("catsAllowed"),
             "dogs_allowed": item.get("dogsAllowed"), "year_built": item.get("yearBuilt"),
             "stories": item.get("stories"), "details_missing": item.get("detailsMissing")}
    # siteTotal (the search's rental count) stays in the raw file: on a row it would change every row's content hash
    # whenever the city's count moves
    out = []
    for f in item.get("floorPlans") or []:
        if not is_home(f):
            continue
        rent = f.get("rentMin") or None  # None: "Ask for pricing", a plan without an offer
        out.append(Parsed(
            source_id=f"plan:{f.get('floorPlanId') or pid + ':' + str(f.get('name'))}", url=url,
            price=rent, price_basis="month" if rent else None,
            data=ObservationData(**base, kind="apartment", term="long", floor_plan=f.get("name"), beds=f.get("beds"),
                                 baths=f.get("baths"), sqft=f.get("sqft") or None,
                                 extra={**extra, "floor_plan_id": f.get("floorPlanId"), "rent_text": f.get("rentText"),
                                        "plan_price_max": f.get("rentMax") if rent and f.get("rentMax") != rent else None,
                                        "plan_available": f.get("hasAvailableUnits"),
                                        "price_drop": f.get("priceDrop")})))
    if out:
        return out
    return [Parsed(source_id=f"building:{pid}", url=url, price=None, price_basis=None,
                   data=ObservationData(**base, kind=SITE_INFO,
                                        extra={**extra, "plan_count": item.get("floorPlanCount"),
                                               "note": "no floor plan listed: not an offer"}))]


ADAPTER = Adapter(name="rentcafe", build_input=rentcafe_input, parse=rentcafe_parse, area_kind="city",
                  area_of=city_of, required=("lat",))
