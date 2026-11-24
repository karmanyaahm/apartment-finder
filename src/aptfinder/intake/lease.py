"""Lease aggregators. Trialled 2026-11-24 on Mountain View:

* Zillow via clearpath/zillow-zip-code-search-scraper: address, coordinates, beds, baths, sqft. Buildings
  come back as one entry with a `unitsGroup` breakdown (bedrooms, min price, no baths), stored as one
  listing per building and bedroom count with baths unknown.
* Apartments.com via epctex/apartments-scraper-api: per-unit rows (unit number, beds, baths, sqft, price,
  available date), or floor plans when no unit is posted.
* Craigslist via solidcode/craigslist-scraper: coordinates, price, beds, baths, sqft, availability; no
  street address (identified by coordinates).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from ..store.keys import UNIT_RE
from ..store.models import ObservationData
from .base import Adapter, Parsed, money

TODAY = date.today


def _first_num(text: Any) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"\d[\d,]*(\.\d+)?", str(text))
    return float(m.group(0).replace(",", "")) if m else None


def _avail(text: str | None, ref: date | None = None) -> str | None:
    """'now' / 'Now' -> today; 'dec 13' / 'Dec. 13' -> next such date; ISO stays ISO."""
    if not text:
        return None
    t = str(text).strip().lower()
    ref = ref or TODAY()
    if t.startswith("now") or t.startswith("available now"):
        return ref.isoformat()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", t)
    if m:
        return m.group(1)
    m = re.search(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2})", t)
    if m:
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)} {ref.year}", "%b %d %Y").date()
        except ValueError:
            return None
        if d < ref.replace(day=1):
            d = d.replace(year=ref.year + 1)
        return d.isoformat()
    return None


# ------------------------------------------------------------------------------------------- Zillow
def zillow_input(cfg: dict[str, Any], areas: list) -> dict[str, Any]:
    return {"locations": [a.id for a in areas], "propertyType": "forRent",
            "maxResultsPerLocation": cfg.get("max_items", 400)}  # per ZIP; the run's maxItems scales with ZIPs


ZILLOW_KIND = {"apartment": "apartment", "condo": "apartment", "singlefamily": "house", "single_family": "house",
               "townhouse": "townhome", "townhome": "townhome", "multifamily": "apartment"}


def zillow_parse(item: dict[str, Any], cfg: dict[str, Any]) -> list[Parsed] | None:
    p = (item.get("rawData") or {}).get("property") or {}
    zpid = p.get("zpid")
    if not zpid:
        return None
    a = p.get("address") or {}
    street = a.get("streetAddress") or ""
    loc = p.get("location") or {}
    price = p.get("price")
    price = price.get("value") if isinstance(price, dict) else price
    price = price or (p.get("hdpView") or {}).get("price")
    kind = ZILLOW_KIND.get(str(p.get("propertyType", "")).lower().replace(" ", ""), "apartment")
    amen = [r.get("displayString") for r in ((p.get("listCardRecommendation") or {})
                                              .get("flexFieldRecommendations") or []) if r.get("displayString")]
    base = dict(city=a.get("city"), zip=a.get("zipcode"), lat=loc.get("latitude") or item.get("latitude"),
                lon=loc.get("longitude") or item.get("longitude"), kind=kind, term="long",
                property_name=p.get("title"), amenities=amen or None)
    extra = {"zpid": zpid, "group_type": p.get("groupType"), "days_on_zillow": p.get("daysOnZillow"),
             "rent_zestimate": item.get("rentZestimate"), "special_offer": item.get("specialOffer"),
             "images": [((p.get("media") or {}).get("propertyPhotoLinks") or {}).get("highResolutionLink")]}
    url = item.get("listingUrl") or f"https://www.zillow.com/homedetails/{zpid}_zpid/"
    groups = p.get("unitsGroup") or []
    if groups:
        # a building: one listing per bedroom count; the street line's unit is a placeholder, drop it
        street_only = UNIT_RE.sub("", street).strip(" ,")
        addr = f"{street_only}, {a.get('city')}, {a.get('state')} {a.get('zipcode')}"
        out = []
        for g in groups:
            if g.get("isRoomForRent"):
                continue
            beds = g.get("bedrooms")
            label = "studio" if beds == 0 else f"{beds}bd"
            out.append(Parsed(
                source_id=f"{zpid}:{label}", url=url, price=money(g.get("minPrice")), price_basis="month",
                data=ObservationData(address=addr, beds=beds, baths=None, floor_plan=label,
                                     extra={**extra, "building_min_price": p.get("minPrice"),
                                            "building_max_price": p.get("maxPrice"), "from_units_group": True},
                                     **base)))
        return out
    addr = f"{street}, {a.get('city')}, {a.get('state')} {a.get('zipcode')}"
    return [Parsed(source_id=str(zpid), url=url, price=money(price), price_basis="month",
                   data=ObservationData(address=addr, beds=p.get("bedrooms"), baths=p.get("bathrooms"),
                                        sqft=p.get("livingArea"), extra=extra, **base))]


# -------------------------------------------------------------------------------------- Apartments.com
def apartments_input(cfg: dict[str, Any], city: str) -> dict[str, Any]:
    slug = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-") + "-ca"
    return {"startUrls": [f"https://www.apartments.com/{slug}/"], "maxItems": cfg.get("max_items", 150)}


def _bed_bath(details: list[str] | None) -> tuple[float | None, float | None]:
    beds = baths = None
    for d in details or []:
        dl = d.lower()
        if "studio" in dl:
            beds = 0.0
        elif "bed" in dl:
            beds = _first_num(dl)
        elif "bath" in dl:
            baths = _first_num(dl)
    return beds, baths


def apartments_parse(item: dict[str, Any], cfg: dict[str, Any]) -> list[Parsed] | None:
    pid = item.get("id")
    if not pid:
        return None
    loc = item.get("location") or {}
    co = item.get("coordinates") or {}
    amen = [v for g in item.get("amenities") or [] for v in g.get("value") or [] if not v.startswith("**")]
    base = dict(address=loc.get("fullAddress"), city=loc.get("city"), zip=loc.get("postalCode"),
                lat=co.get("latitude"), lon=co.get("longitude"), kind="apartment", term="long",
                property_name=item.get("propertyName"), operator=(item.get("contact") or {}).get("name"),
                amenities=amen or None)
    extra = {"specials": (item.get("specials") or {}).get("description"), "rating": item.get("rating"),
             "website": item.get("propertyWebsite"), "phone": (item.get("contact") or {}).get("phone"),
             "rent_range": [(item.get("baseRent") or {}).get("min"), (item.get("baseRent") or {}).get("max")]}
    url = item.get("url")
    out = []
    for r in item.get("rentals") or []:
        beds = r.get("beds")
        out.append(Parsed(
            source_id=f"{pid}:u:{r.get('key') or r.get('unitNumber')}", url=url,
            price=money(r.get("basePrice")), price_basis="month",
            data=ObservationData(unit=r.get("unitNumber"), floor_plan=r.get("modelName"), beds=beds,
                                 baths=r.get("baths"), sqft=_first_num(r.get("squareFeet")),
                                 available_from=_avail(r.get("availableDate") or r.get("availability")),
                                 extra={**extra, "images": [r.get("image")] if r.get("image") else []}, **base)))
    if not out:  # no unit posted: one listing per floor plan
        for m in item.get("models") or []:
            beds, baths = _bed_bath(m.get("details"))
            out.append(Parsed(
                source_id=f"{pid}:m:{m.get('modelId')}", url=url, price=money(m.get("basePrice")),
                price_basis="month",
                data=ObservationData(floor_plan=m.get("modelName"), beds=beds, baths=baths,
                                     sqft=_first_num(m.get("squareFeet")),
                                     available_from=_avail(m.get("availabilityInfo")),
                                     extra={**extra, "availability": m.get("availability"),
                                            "images": [m.get("image")] if m.get("image") else []}, **base)))
    return out


# -------------------------------------------------------------------------------------------- Craigslist
def craigslist_input(cfg: dict[str, Any], areas: list) -> dict[str, Any]:
    miles = cfg.get("radius_miles", 1.5)
    return {"startUrls": [{"url": f"https://sfbay.craigslist.org/search/apa?postal={a.id}&search_distance={miles}"}
                          for a in areas],
            "includeDetails": True, "maxResults": cfg.get("max_items", 300)}


CL_KIND = {"apartment": "apartment", "condo": "apartment", "house": "house", "townhouse": "townhome",
           "duplex": "house", "flat": "apartment", "in-law": "apartment", "cottage/cabin": "house",
           "loft": "apartment"}
CL_BASIS = {"monthly": "month", "weekly": "week", "daily": "night"}


def craigslist_parse(item: dict[str, Any], cfg: dict[str, Any]) -> Parsed | None:
    pid = item.get("postId")
    if not pid:
        return None
    at = item.get("attributes") or {}
    period = (at.get("rent_period") or "monthly").lower()
    data = ObservationData(
        city=item.get("location"), lat=item.get("latitude"), lon=item.get("longitude"),
        beds=_first_num(at.get("bedrooms")), baths=_first_num(at.get("bathrooms")), sqft=_first_num(at.get("sqft")),
        kind=CL_KIND.get((at.get("housing_type") or "").lower()), term="long" if period == "monthly" else "short",
        furnished=True if at.get("furnished") else None,
        available_from=_avail(at.get("available"), datetime.fromisoformat(item["postedAt"]).date()
                              if item.get("postedAt") else None),
        title=item.get("title"), description=(item.get("description") or "")[:3000] or None,
        amenities=[v for k, v in at.items() if k in ("laundry", "parking", "airconditioning", "ev_charging")
                   and v] or None,
        extra={"posted_at": item.get("postedAt"), "updated_at": item.get("updatedAt"),
               "housing_type": at.get("housing_type"), "pets": [at.get("pets_cat"), at.get("pets_dog")],
               "images": (item.get("imageUrls") or [])[:8]},
    )
    return Parsed(source_id=str(pid), url=item.get("url"), data=data, price=money(item.get("priceUsd")),
                  price_basis=CL_BASIS.get(period, "month"))


def zillow_area(item: dict[str, Any], ids: list[str]) -> str | None:
    zp = str(((item.get("rawData") or {}).get("property") or {}).get("address", {}).get("zipcode") or "")
    loc = str(item.get("location") or "")
    return loc if loc in ids else (zp if zp in ids else None)


def apartments_area(item: dict[str, Any], ids: list[str]) -> str | None:
    z = str(item.get("listingZip") or "")
    return z if z in ids else None


ZILLOW = Adapter(name="zillow", build_input=zillow_input, parse=zillow_parse, area_kind="zip", batch=True,
                 area_of=zillow_area)
CRAIGSLIST = Adapter(name="craigslist", build_input=craigslist_input, parse=craigslist_parse, area_kind="zip")


# ------------------------------------------------------------- Apartments.com (memo23, no free-plan cap)
def apartments2_input(cfg: dict[str, Any], areas: list) -> dict[str, Any]:
    def url(a):
        city = re.sub(r"[^a-z0-9]+", "-", (a.city or "").lower()).strip("-")
        return f"https://www.apartments.com/{city}-ca-{a.id}/"
    return {"startUrls": [url(a) for a in areas], "includeMoreDetails": True,
            "maxItems": cfg.get("max_items", 150)}


def apartments2_parse(item: dict[str, Any], cfg: dict[str, Any]) -> list[Parsed] | None:
    pid = item.get("listingId")
    if not pid:
        return None
    loc = item.get("location") or {}
    amen = [v for g in item.get("amenities") or [] for v in g.get("value") or [] if not str(v).startswith("**")]
    addr = f"{item.get('listingAddress')}, {item.get('listingCity')}, {item.get('listingState')} {item.get('listingZip')}"
    base = dict(address=addr, city=item.get("listingCity"), zip=item.get("listingZip"), lat=loc.get("latitude"),
                lon=loc.get("longitude"), kind="apartment", term="long", property_name=item.get("listingName"),
                amenities=amen or None)
    photos = [i.get("url") for i in item.get("images") or [] if isinstance(i, dict) and i.get("url")]
    if not photos and item.get("imageUrl"):
        photos = [item["imageUrl"]]
    extra = {"rent_range": [item.get("listingMinRent"), item.get("listingMaxRent")],
             "specialties": item.get("listingSpecialties"), "has_specials": item.get("hasRentSpecials"),
             "images": photos[:16]}
    url = item.get("url")
    out = []
    for m in item.get("models") or []:
        beds, baths = _bed_bath(m.get("details"))
        plan = m.get("modelName")
        plan_key = re.sub(r"[^a-z0-9]+", "-", (plan or "plan").lower()).strip("-")[:40]
        sqft_plan = _first_num(next((d for d in m.get("details") or [] if "sq ft" in d.lower()), None))
        if sqft_plan:  # same-named plans (e.g. two "Three Bedroom" plans) differ by size
            plan_key = f"{plan_key}-{int(sqft_plan)}"
        units = m.get("units") or []
        pextra = {**extra, "floorplan_image": m.get("imageLarge") or m.get("image")}
        for u in units:
            out.append(Parsed(
                source_id=f"{pid}:u:{u.get('type')}", url=url, price=money(u.get("price")), price_basis="month",
                data=ObservationData(unit=u.get("type"), floor_plan=plan, beds=beds, baths=baths,
                                     sqft=_first_num(u.get("sqft")), available_from=_avail(u.get("availability")),
                                     extra=pextra, **base)))
        if not units:
            out.append(Parsed(
                source_id=f"{pid}:m:{plan_key}", url=url, price=money(m.get("rentLabel")), price_basis="month",
                data=ObservationData(floor_plan=plan, beds=beds, baths=baths,
                                     sqft=sqft_plan,
                                     available_from=_avail(m.get("availability")),
                                     extra={**pextra, "availability": m.get("availability")}, **base)))
    if not item.get("models"):
        # a single unit, condo or house for rent: details live in `rentals`
        ptype = item.get("propertyType") or ""
        kind = {"SingleFamily": "house", "Townhouse": "townhome", "Condo": "apartment"}.get(ptype, "apartment")
        for r in item.get("rentals") or []:
            desc = r.get("Description") or ""
            out.append(Parsed(
                source_id=f"{pid}:r:{r.get('RentalKey')}", url=url, price=money(r.get("Rent")), price_basis="month",
                data=ObservationData(**{**base, "kind": kind, "property_name": item.get("listingName") or None},
                                     unit=r.get("UnitNumber") or None, beds=r.get("Beds"), baths=r.get("Baths"),
                                     sqft=_first_num(r.get("SquareFeet")),
                                     available_from=_avail(r.get("AvailableDate") or r.get("AvailableDateText")),
                                     description=desc[:3000] or None,
                                     furnished=True if "furnished" in desc.lower() else None,
                                     extra={**extra, "property_type": ptype, "min_lease_months": r.get("MinLeaseTerm"),
                                            "max_lease_months": r.get("MaxLeaseTerm")})))
    return out


APARTMENTS = Adapter(name="apartments_com", build_input=apartments2_input, parse=apartments2_parse,
                     area_kind="zip", batch=True, area_of=apartments_area)
