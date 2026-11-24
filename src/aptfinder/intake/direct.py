"""Direct enrichment: a community's own website, fetched through Apify's generic cheerio-scraper.

The page function returns the page's embedded data scripts (JSON-LD, __NEXT_DATA__, Fusion state, floor plan
apps); `parse` dispatches on the site's platform (config/direct_sites.toml). Observations use source
`direct`, source_id `<site>:u:<unit>` for units and `<site>:m:<plan>` for floor plans, and the site's own street
address, so identity puts them on the same place as the aggregator listings.
"""

from __future__ import annotations

import json
import re
import tomllib
from functools import lru_cache
from typing import Any

from .. import settings
from ..store.models import ObservationData
from .base import Adapter, Parsed, money

PAGE_FUNCTION = r"""async function pageFunction(context) {
  const $ = context.$;
  const scripts = $('script').map((i, s) => ({ type: $(s).attr('type') || '', id: $(s).attr('id') || '',
                                               text: $(s).html() || '' })).get()
    .filter(s => s.text.length > 300 && (s.type.includes('json') || s.id === '__NEXT_DATA__'
             || /Fusion\.globalContent|"floorplans"/.test(s.text.slice(0, 50000))));
  return { url: context.request.loadedUrl || context.request.url, requested: context.request.url,
           status: context.response && context.response.status, title: $('title').text(), scripts };
}"""


@lru_cache(maxsize=1)
def sites() -> dict[str, dict[str, Any]]:
    path = settings.CONFIG_DIR / "direct_sites.toml"
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return {s["id"]: s for s in tomllib.load(f).get("site", [])}


def build_input(cfg: dict[str, Any], areas: list) -> dict[str, Any]:
    return {"startUrls": [{"url": sites()[a.id]["url"], "userData": {"site": a.id}} for a in areas],
            "pageFunction": PAGE_FUNCTION, "proxyConfiguration": {"useApifyProxy": True},
            "maxRequestsPerCrawl": len(areas)}


def _site_for(item: dict[str, Any]) -> dict[str, Any] | None:
    for s in sites().values():
        if item.get("requested", "").rstrip("/") == s["url"].rstrip("/") or item.get("url", "").rstrip("/") == \
                s["url"].rstrip("/"):
            return s
    return None


def area_of(item: dict[str, Any], ids: list[str]) -> str | None:
    s = _site_for(item)
    return s["id"] if s and s["id"] in ids else None


def _script(item: dict[str, Any], pred) -> str | None:
    return next((s["text"] for s in item.get("scripts") or [] if pred(s)), None)


def _ld_address(item: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    for s in item.get("scripts") or []:
        if "ld+json" not in s["type"]:
            continue
        try:
            d = json.loads(s["text"])
        except ValueError:
            continue
        for o in d if isinstance(d, list) else [d]:
            a = o.get("address") if isinstance(o, dict) else None
            if isinstance(a, dict) and a.get("streetAddress"):
                return a["streetAddress"], a.get("addressLocality"), a.get("postalCode")
    return None, None, None


def _date(v: Any) -> str | None:
    return str(v)[:10] if v else None


# ------------------------------------------------------------------------------------------------- AvalonBay
def parse_avalon(item: dict[str, Any], site: dict[str, Any]) -> list[Parsed]:
    text = _script(item, lambda s: s["id"] == "fusion-metadata" or "Fusion.globalContent" in s["text"][:50000])
    if not text:
        return []
    m = re.search(r"Fusion\.globalContent\s*=\s*", text)
    g, _ = json.JSONDecoder().raw_decode(text[m.end():])
    a = g.get("address") or {}
    co = g.get("coordinates") or {}
    base = dict(address=f"{a.get('addressLine1')}, {a.get('city')}, {a.get('state')} {a.get('zip')}",
                city=a.get("city"), zip=a.get("zip"), lat=co.get("latitude"), lon=co.get("longitude"),
                kind="apartment", term="long", property_name=g.get("name"), operator="AvalonBay")
    out = []
    for u in g.get("units") or []:
        p = (u.get("startingAtPricesUnfurnished") or {})
        prices = p.get("prices") or {}
        fp = u.get("floorPlan") or {}
        out.append(Parsed(
            source_id=f"{site['id']}:u:{u.get('unitName')}", url=site["url"], price=money(prices.get("price")),
            price_basis="month",
            data=ObservationData(**base, unit=str(u.get("unitName")), floor_plan=fp.get("name"),
                                 beds=u.get("bedroomNumber"), baths=u.get("bathroomNumber"), sqft=u.get("squareFeet"),
                                 available_from=_date(u.get("availableDateUnfurnished")),
                                 extra={"site": site["id"], "platform": "avalon", "lease_term_months": p.get("leaseTerm"),
                                        "total_price": prices.get("totalPrice"),
                                        "net_effective_price": prices.get("netEffectivePrice"),
                                        "unit_status": u.get("unitStatus"), "furnish_status": u.get("furnishStatus")})))
    return out


# ------------------------------------------------------------------------- "jd" floor plan app (e.g. Landsby)
def parse_jd_floorplans(item: dict[str, Any], site: dict[str, Any]) -> list[Parsed]:
    text = _script(item, lambda s: s["id"] == "jd-fp-data-script-app" or '"floorplans"' in s["text"][:50000])
    if not text:
        return []
    d = json.loads(text)
    street, city, zip_ = _ld_address(item)
    address = f"{street}, {city}, CA {zip_}" if street else site.get("address")
    base = dict(address=address, city=city, zip=zip_, kind="apartment",
                term="long", property_name=site.get("name"))
    out = []
    for fp in d.get("floorplans") or []:
        avail = fp.get("available_date") or None
        out.append(Parsed(
            source_id=f"{site['id']}:m:{fp.get('slug') or fp.get('id')}", url=site["url"],
            price=money(fp.get("rent_min")) if fp.get("rent_min") else None, price_basis="month",
            data=ObservationData(**base, floor_plan=fp.get("title"), beds=money(fp.get("bedrooms")),
                                 baths=money(fp.get("bathrooms")), sqft=money(fp.get("square_feet")),
                                 available_from=_date(avail) if avail else None,
                                 extra={"site": site["id"], "platform": "jd_floorplans",
                                        "availability_count": fp.get("availability_count"),
                                        "rent_max": fp.get("rent_max") or None,
                                        "note": "rent and availability load via JavaScript; static page shows plans"})))
    return out


# --------------------------------------------------------------------------------------------- Prometheus
PROM_SQFT = re.compile(r"(\d{3,4})\s*sqft", re.IGNORECASE)
PROM_BB = re.compile(r"_(\d)B(\d(?:\.\d)?)BR", re.IGNORECASE)  # e.g. ..._594sqft_11CA_1B1BR_1A.svg


def parse_prometheus(item: dict[str, Any], site: dict[str, Any]) -> list[Parsed]:
    text = _script(item, lambda s: s["id"] == "__NEXT_DATA__")
    if not text:
        return []
    apd = json.loads(text)["props"]["pageProps"]["apartmentPageData"]
    ll = apd.get("latLong") or {}
    photos = [g.get("src") for g in ((apd.get("webContents") or {}).get("fields") or {}).get("galleryImages") or []
              if g.get("src")][:16]
    base = dict(address=apd.get("fullAddress") or site.get("address"), city=apd.get("city"), lat=ll.get("lat"), lon=ll.get("lon"),
                kind="apartment", term="long", property_name=apd.get("name"), operator="Prometheus")
    out = []
    pngs = apd.get("floorplanPng") or {}
    for name, svg in (apd.get("floorplanSvg") or {}).items():
        img = pngs.get(name) or svg
        fname = (svg or "").rsplit("/", 1)[-1]
        ms, mb = PROM_SQFT.search(fname), PROM_BB.search(fname)
        sqft = float(ms.group(1)) if ms else None
        beds, baths = (float(mb.group(1)), float(mb.group(2))) if mb else (None, None)
        if beds is None and "studio" in fname.lower():
            beds = 0.0
        out.append(Parsed(
            source_id=f"{site['id']}:m:{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}", url=site["url"],
            price=None, price_basis=None,
            data=ObservationData(**base, floor_plan=name, beds=beds, baths=baths, sqft=sqft,
                                 extra={"site": site["id"], "platform": "prometheus", "floorplan_image": img,
                                        "images": photos, "sizes_from": "plan drawing file name",
                                        "note": "prices and availability load via JavaScript"})))
    return out


PARSERS = {"avalon": parse_avalon, "jd_floorplans": parse_jd_floorplans, "prometheus": parse_prometheus}


def parse(item: dict[str, Any], cfg: dict[str, Any]) -> list[Parsed] | None:
    site = _site_for(item)
    if not site:
        return None
    return PARSERS[site["platform"]](item, site) or None


ADAPTER = Adapter(name="direct", build_input=build_input, parse=parse, area_kind="site", batch=True,
                  area_of=area_of, required=())
