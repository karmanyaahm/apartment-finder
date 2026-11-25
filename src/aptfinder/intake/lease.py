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
from .discover import clean_url

TODAY = date.today


def _first_num(text: Any) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"\d[\d,]*(\.\d+)?", str(text))
    return float(m.group(0).replace(",", "")) if m else None


def furnished_from_text(text: str | None) -> bool | None:
    """True / False when the text says so, None otherwise. 'unfurnished' is not 'furnished'."""
    t = (text or "").lower()
    neg = bool(re.search(r"\bunfurnished\b|\bnot furnished\b", t))
    pos = bool(re.search(r"(?<!un)\bfurnished\b", t))
    if neg and pos:  # e.g. Blueground: "looking for an unfurnished lease? ... upgrade to furnished living"
        return None
    return False if neg else (True if pos else None)


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
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", t)  # US numeric, as UDR's unit lists write it: 12/1/2026
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
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


# The search card's `flexFieldRecommendations` are mostly badges ('Apply instantly', '13 days ago', 'Waitlisted',
# '3D Tour', '2 available units', '$1,000 Off'); only `homeInsight` ('Swimming pool', 'Designer finishes') and the
# card's `zovInsight` ('Clubhouse', 'Fitness center') name features. Allowlisted, so a new badge type never counts.
AMENITY_TYPES = ("homeInsight",)


def zillow_amenities(p: dict[str, Any]) -> list[str]:
    rec = p.get("listCardRecommendation") or {}
    out = [r.get("displayString") for r in rec.get("flexFieldRecommendations") or []
           if r.get("contentType") in AMENITY_TYPES and r.get("displayString")]
    zov = (rec.get("zovInsight") or {}).get("displayString")
    return out + ([zov] if zov and zov not in out else [])


def _zillow_fee_range(p: dict[str, Any]) -> tuple[Any, Any]:
    # the search card carries them on `property` (buildings) or on `property.rental` (some single homes)
    rental = p.get("rental") or {}
    lo = p.get("totalRequiredMonthlyMinFee") or rental.get("totalRequiredMonthlyMinFee")
    hi = p.get("totalRequiredMonthlyMaxFee") or rental.get("totalRequiredMonthlyMaxFee")
    return lo, hi


def zillow_fees(p: dict[str, Any]) -> str | None:
    """The card's required monthly fees ('$100', '$50 to $75'), a line the text reading reads. Only those on
    `property.rental`: reading the ones on `property` too would change the text of 117 active rows (2026-11-25) and
    drop their cached readings until a new text-reading pass."""
    rental = p.get("rental") or {}
    lo, hi = rental.get("totalRequiredMonthlyMinFee"), rental.get("totalRequiredMonthlyMaxFee")
    return (f"${lo}" if lo == hi or not hi else f"${lo} to ${hi}") if lo else None


def zillow_included_fee(p: dict[str, Any]) -> float | None:
    """The required monthly fee folded into the card's prices, when Zillow says its prices include it
    (`rental.listPriceIncludesRequiredMonthlyFees`) and the fee is one amount: Landsby's 1 bd shows $4,221 =
    base rent $4,175 + $46. Base rent is the price (decision log 2026-11-25), so this is taken off."""
    if not (p.get("rental") or {}).get("listPriceIncludesRequiredMonthlyFees"):
        return None
    lo, hi = _zillow_fee_range(p)
    return float(lo) if isinstance(lo, (int, float)) and lo > 0 and (hi in (None, lo)) else None


def _less_fee(price: float | None, fee: float | None) -> tuple[float | None, dict[str, Any]]:
    """(base rent, extras) of a fee-inclusive price: the total is kept beside it as `total_monthly`."""
    if price is None or not fee:
        return price, {}
    return price - fee, {"total_monthly": price}


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
    amen = zillow_amenities(p)
    base = dict(city=a.get("city"), zip=a.get("zipcode"), lat=loc.get("latitude") or item.get("latitude"),
                lon=loc.get("longitude") or item.get("longitude"), kind=kind, term="long",
                property_name=p.get("title"), amenities=amen or None)
    extra = {"zpid": zpid, "group_type": p.get("groupType"), "days_on_zillow": p.get("daysOnZillow"),
             "rent_zestimate": item.get("rentZestimate"), "special_offer": item.get("specialOffer"),
             **({"required_monthly_fees": fees} if (fees := zillow_fees(p)) else {}),
             # an apartmentComplex card groups this many homes matching the search: the homes listed, a lower bound
             # on the complex's units, never its total (filter.building); only when given, so other rows hash as before
             **({"complex_home_count": n} if isinstance(n := p.get("matchingHomeCount"), int) and n > 0 else {}),
             # the whole gallery (up to 24), not just the cover photo
             "images": ([u for u in ((p.get("media") or {}).get("allPropertyPhotos") or {}).get("highResolution") or []
                         if u][:24]
                        or [((p.get("media") or {}).get("propertyPhotoLinks") or {}).get("highResolutionLink")])}
    url = item.get("listingUrl") or f"https://www.zillow.com/homedetails/{zpid}_zpid/"
    fee = zillow_included_fee(p)
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
            gprice, tot = _less_fee(money(g.get("minPrice")), fee)
            out.append(Parsed(
                source_id=f"{zpid}:{label}", url=url, price=gprice, price_basis="month",
                data=ObservationData(address=addr, beds=beds, baths=None, floor_plan=label,
                                     extra={**extra, "building_min_price": p.get("minPrice"),
                                            "building_max_price": p.get("maxPrice"), "from_units_group": True,
                                            **tot},
                                     **base)))
        return out
    addr = f"{street}, {a.get('city')}, {a.get('state')} {a.get('zipcode')}"
    # Zillow's `bathrooms` counts every bathroom (2 full + 1 half shows as 3, sometimes more); the facts give
    # full and half separately, so baths = full + half / 2 when the full count is there
    ff = p.get("factsAndFeatures") or {}
    full, half = ff.get("fullBathroomCount"), ff.get("halfBathroomCount")
    baths = full + (half or 0) / 2 if isinstance(full, (int, float)) and full > 0 else p.get("bathrooms")
    # a few single units carry Zillow's own floor plan drawing (richMedia[].rmxData[].floorPlan.heroImageUrl)
    drawing = next((r["floorPlan"]["heroImageUrl"] for m in p.get("richMedia") or [] for r in m.get("rmxData") or []
                    if (r.get("floorPlan") or {}).get("heroImageUrl")), None)
    uprice, tot = _less_fee(money(price), fee)
    extra = {**extra, "baths_total": p.get("bathrooms"), "baths_full": full, "baths_half": half,
             **({"floorplan_image": drawing} if drawing else {}), **tot}
    return [Parsed(source_id=str(zpid), url=url, price=uprice, price_basis="month",
                   data=ObservationData(address=addr, beds=p.get("bedrooms"), baths=baths,
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
    c = str(item.get("listingCity") or "")
    return c if c in ids else None


ZILLOW = Adapter(name="zillow", build_input=zillow_input, parse=zillow_parse, area_kind="zip", batch=True,
                 area_of=zillow_area)
CRAIGSLIST = Adapter(name="craigslist", build_input=craigslist_input, parse=craigslist_parse, area_kind="zip")


# ------------------------------------------------------------- Apartments.com (memo23, no free-plan cap)
def apartments2_input(cfg: dict[str, Any], areas: list) -> dict[str, Any]:
    # City pages (proven: Mountain View returned 143 buildings). The {city}-ca-{zip} form returned
    # "No listings were found" for 94306 on 2026-11-24.
    def url(a):
        return f"https://www.apartments.com/{re.sub(r'[^a-z0-9]+', '-', a.id.lower()).strip('-')}-ca/"
    return {"startUrls": [url(a) for a in areas], "includeMoreDetails": True,
            "maxItems": cfg.get("max_items", 150)}


BLUEGROUND_ID = re.compile(r"\(ID #SFO\w+\)")
STAY_UNIT = re.compile(r"\b(?:FL\d+-)?ID\d+[A-Z]?\b")
STAY_TEXT = "is the lowest available price, based on"


def apartments_included_fee(item: dict[str, Any]) -> float | None:
    """The building's required monthly fees that its 'Total Monthly Price' labels include, from `rentals`, which
    give each plan's base `Rent` beside its `MinTotalMonthlyPrice` (Landsby: $4,175 and $4,221, fee $46).
    Only when every rental agrees on one amount (all 37 such buildings in the raw files, 2026-11-25); 0 when the
    label's total is the rent itself (707 Leahy)."""
    fees = {r["MinTotalMonthlyPrice"] - r["Rent"] for r in item.get("rentals") or []
            if isinstance(r.get("Rent"), (int, float)) and r["Rent"] > 0
            and isinstance(r.get("MinTotalMonthlyPrice"), (int, float))}
    return float(fees.pop()) if len(fees) == 1 and next(iter(fees)) >= 0 else None


def apartments2_parse(item: dict[str, Any], cfg: dict[str, Any]) -> list[Parsed] | None:
    pid = item.get("listingId")
    if not pid:
        # raw files from the earlier epctex actor (run 3) have a different shape; keep them reparseable
        return apartments_parse(item, cfg) if item.get("id") and "rentals" in item else None
    loc = item.get("location") or {}
    amen = [v for g in item.get("amenities") or [] for v in g.get("value") or [] if not str(v).startswith("**")]
    addr = f"{item.get('listingAddress')}, {item.get('listingCity')}, {item.get('listingState')} {item.get('listingZip')}"
    base = dict(address=addr, city=item.get("listingCity"), zip=item.get("listingZip"), lat=loc.get("latitude"),
                lon=loc.get("longitude"), kind="apartment", term="long", property_name=item.get("listingName"),
                amenities=amen or None)
    photos = [i.get("url") for i in item.get("images") or [] if isinstance(i, dict) and i.get("url")]
    if not photos and item.get("imageUrl"):
        photos = [item["imageUrl"]]
    md = item.get("moreDetails") or {}
    mgmt = md.get("managementCompany")
    extra = {"rent_range": [item.get("listingMinRent"), item.get("listingMaxRent")],
             "specialties": item.get("listingSpecialties"), "has_specials": item.get("hasRentSpecials"),
             # the building's own website, as Apartments.com lists it: a lead for direct intake (cleaned of
             # tracking query strings and apply/tour paths; aggregator and social hosts dropped)
             "website": clean_url(md.get("propertyWebsiteUri"))[0],
             "management_company": (mgmt.get("name") if isinstance(mgmt, dict) else mgmt) or None,
             "phone": md.get("phone") or item.get("phone"),
             "images": photos[:16]}
    url = item.get("url")
    out = []
    fee = apartments_included_fee(item)
    for m in item.get("models") or []:
        beds, baths = _bed_bath(m.get("details"))
        # a plan labelled 'Total Monthly Price' shows rent plus required monthly fees, on the label and its units
        is_total = "total monthly price" in (m.get("rentLabel") or "").lower()
        mfee = fee if is_total else None
        # a total whose fee the rentals do not give is kept as it is, and said to be one (none in the raw files)
        flag = {"price_includes_fees": True} if is_total and fee is None else {}
        plan = m.get("modelName")
        plan_key = re.sub(r"[^a-z0-9]+", "-", (plan or "plan").lower()).strip("-")[:40]
        sqft_plan = _first_num(next((d for d in m.get("details") or [] if "sq ft" in d.lower()), None))
        if sqft_plan:  # same-named plans (e.g. two "Three Bedroom" plans) differ by size
            plan_key = f"{plan_key}-{int(sqft_plan)}"
        units = m.get("units") or []
        pextra = {**extra, "floorplan_image": m.get("imageLarge") or m.get("image")}
        for u in units:
            uprice, tot = _less_fee(money(u.get("price")), mfee)
            out.append(Parsed(
                source_id=f"{pid}:u:{u.get('type')}", url=url, price=uprice, price_basis="month",
                data=ObservationData(unit=u.get("type"), floor_plan=plan, beds=beds, baths=baths,
                                     sqft=_first_num(u.get("sqft")), available_from=_avail(u.get("availability")),
                                     extra={**pextra, **tot, **flag}, **base)))
        if not units:
            mprice, tot = _less_fee(money(m.get("rentLabel")), mfee)
            out.append(Parsed(
                source_id=f"{pid}:m:{plan_key}", url=url, price=mprice, price_basis="month",
                data=ObservationData(floor_plan=plan, beds=beds, baths=baths,
                                     sqft=sqft_plan,
                                     available_from=_avail(m.get("availability")),
                                     extra={**pextra, "availability": m.get("availability"), **tot, **flag},
                                     **base)))
    if not item.get("models"):
        # a single unit, condo or house for rent: details live in `rentals`
        ptype = item.get("propertyType") or ""
        kind = {"SingleFamily": "house", "Townhouse": "townhome", "Condo": "apartment"}.get(ptype, "apartment")
        for r in item.get("rentals") or []:
            desc = r.get("Description") or ""
            operator = "Blueground" if ("blueground" in desc.lower() or BLUEGROUND_ID.search(desc)) else None
            # the furnished-stay listing template (Blueground's, also used by units that never name an operator):
            # unit label "SI FL1-ID13012A" / "GO ID1309221P", "The advertised rate of $X/month is the lowest
            # available price, based on a 30 day(s) stay", 1-12 month lease
            template = bool(STAY_UNIT.search(r.get("UnitNumber") or "")) or STAY_TEXT in desc
            # Blueground only rents furnished homes; its text also advertises against "unfurnished leases"
            furnished = True if (operator == "Blueground" or template) else furnished_from_text(desc)
            # furnished monthly rentals (Blueground: 1-12 month stays) are short-term stays, not leases
            short = bool(furnished) and (r.get("MinLeaseTerm") or 12) <= 3
            out.append(Parsed(
                source_id=f"{pid}:r:{r.get('RentalKey')}", url=url, price=money(r.get("Rent")), price_basis="month",
                data=ObservationData(**{**base, "kind": kind, "property_name": item.get("listingName") or None,
                                        "term": "short" if short else "long",
                                        "operator": operator or base.get("operator")},
                                     unit=r.get("UnitNumber") or None, beds=r.get("Beds"), baths=r.get("Baths"),
                                     sqft=_first_num(r.get("SquareFeet")),
                                     available_from=_avail(r.get("AvailableDate") or r.get("AvailableDateText")),
                                     description=desc[:3000] or None,
                                     furnished=furnished,
                                     # a Blueground (or stay-template) home is a whole furnished unit: its bath
                                     # is the renter's own, as Airbnb's and Furnished Finder's whole units say
                                     private_bath=True if (operator == "Blueground" or template) else None,
                                     extra={**extra, "property_type": ptype, "stay_template": template or None,
                                            "min_lease_months": r.get("MinLeaseTerm"),
                                            "max_lease_months": r.get("MaxLeaseTerm")})))
    return out


APARTMENTS = Adapter(name="apartments_com", build_input=apartments2_input, parse=apartments2_parse,
                     area_kind="city", batch=True, area_of=apartments_area,
                     is_empty_marker=lambda item: bool(item.get("noResults")))
