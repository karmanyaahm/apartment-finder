"""Direct enrichment: a community's own website, fetched through Apify's generic cheerio-scraper.

The page function returns the page's embedded data scripts (JSON-LD, __NEXT_DATA__, Fusion state, floor plan
apps); `parse` dispatches on the site's platform (config/direct_sites.toml). Observations use source
`direct`, source_id `<site>:u:<unit>` for units and `<site>:m:<plan>` for floor plans, and the site's own street
address, so identity puts them on the same place as the aggregator listings.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from ..config import load_sites
from ..store.models import ObservationData
from .base import Adapter, Parsed, money

PAGE_FUNCTION = r"""async function pageFunction(context) {
  const $ = context.$;
  const scripts = $('script').map((i, s) => ({ type: $(s).attr('type') || '', id: $(s).attr('id') || '',
                                               text: $(s).html() || '' })).get()
    .filter(s => s.text.length > 300 && (s.type.includes('json') || s.id === '__NEXT_DATA__'
             || /Fusion\.globalContent|"floorplans"/.test(s.text.slice(0, 50000))));
  // every candidate image with its attributes; `parse` decides which are photos (tuning is a free reparse)
  const images = [];
  $('meta[property="og:image"], meta[name="twitter:image"]').each((i, m) =>
    images.push({ src: $(m).attr('content') || '', from: 'meta' }));
  $('img, source').each((i, el) => {
    const e = $(el), srcset = e.attr('srcset') || e.attr('data-srcset') || '';
    const last = srcset.split(/,\s+/).map(x => x.trim().split(/\s+/)[0]).filter(Boolean).pop() || '';
    images.push({ src: e.attr('data-src') || e.attr('data-lazy-src') || e.attr('src') || '', srcset: last,
                  alt: e.attr('alt') || '', cls: e.attr('class') || '', w: e.attr('width') || '',
                  h: e.attr('height') || '', from: el.tagName });
  });
  $('[style*="background-image"]').each((i, el) => {
    const m = /url\(['"]?([^'")]+)/.exec($(el).attr('style') || '');
    if (m) images.push({ src: m[1], cls: $(el).attr('class') || '', from: 'style' });
  });
  return { url: context.request.loadedUrl || context.request.url, requested: context.request.url,
           status: context.response && context.response.status, title: $('title').text(), scripts,
           images: images.slice(0, 400), site: (context.request.userData || {}).site || null,
           text: $('body').text().replace(/\s+/g, ' ').slice(0, 100000) };
}"""


@lru_cache(maxsize=1)
def sites() -> dict[str, dict[str, Any]]:
    return load_sites()


def build_input(cfg: dict[str, Any], areas: list) -> dict[str, Any]:
    return {"startUrls": [{"url": sites()[a.id]["url"], "userData": {"site": a.id}} for a in areas],
            "pageFunction": PAGE_FUNCTION, "proxyConfiguration": {"useApifyProxy": True},
            "maxRequestsPerCrawl": len(areas)}


def _site_for(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("site") in sites():  # the request's own tag survives redirects
        return sites()[item["site"]]
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
AVALON_MEDIA = "https://resource.avalonbay.com"  # host of the plan drawings (the page's own <img alt="Floorplan">)


def avalon_drawing(fp: dict[str, Any] | None) -> dict[str, str]:
    """{'floorplan_image': URL} from a unit's `floorPlan.highResolution` ('/floorplans/ca049/ca049-a1.png/1024/768'),
    or {} when the unit has none."""
    path = (fp or {}).get("highResolution") or (fp or {}).get("lowResolution")
    if not path:
        return {}
    return {"floorplan_image": path if path.startswith("http") else f"{AVALON_MEDIA}/{path.lstrip('/')}"}


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
        drawing = avalon_drawing(u.get("floorPlan"))
        prices = p.get("prices") or {}
        fp = u.get("floorPlan") or {}
        # the unit as the site shows it: "00D-130" (building 00D, unit 130) from unitId "AVB-CA049-00D-130"
        uid = str(u.get("unitId") or "")
        unit = uid.split("-", 2)[2] if uid.count("-") >= 2 else str(u.get("unitName"))
        out.append(Parsed(
            source_id=f"{site['id']}:u:{u.get('unitName')}", url=u.get("url") or site["url"],
            price=money(prices.get("price")), price_basis="month",
            data=ObservationData(**base, unit=unit, floor_plan=fp.get("name"),
                                 beds=u.get("bedroomNumber"), baths=u.get("bathroomNumber"), sqft=u.get("squareFeet"),
                                 available_from=_date(u.get("availableDateUnfurnished")),
                                 extra={"site": site["id"], "platform": "avalon", "lease_term_months": p.get("leaseTerm"),
                                        "total_price": prices.get("totalPrice"),
                                        "net_effective_price": prices.get("netEffectivePrice"),
                                        "unit_status": u.get("unitStatus"), "furnish_status": u.get("furnishStatus"),
                                        **drawing})))
        # AvalonBay's own furnished option ("Furnished+") on the same unit: a separate offer with its own price
        fpr = u.get("startingAtPricesFurnished") or {}
        fprices = fpr.get("prices") or {}
        if fprices.get("price"):
            out.append(Parsed(
                source_id=f"{site['id']}:uf:{u.get('unitName')}", url=u.get("url") or site["url"],
                price=money(fprices.get("price")), price_basis="month",
                data=ObservationData(**base, unit=unit, floor_plan=fp.get("name"), furnished=True,
                                     beds=u.get("bedroomNumber"), baths=u.get("bathroomNumber"),
                                     sqft=u.get("squareFeet"), available_from=_date(u.get("availableDateFurnished")),
                                     extra={"site": site["id"], "platform": "avalon", "offer": "Furnished+",
                                            "lease_term_months": fpr.get("leaseTerm"),
                                            "total_price": fprices.get("totalPrice"),
                                            "unit_status": u.get("unitStatus"),
                                            "furnish_status": u.get("furnishStatus"), **drawing})))
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


def parse_prometheus(item: dict[str, Any], site: dict[str, Any]) -> list[Parsed]:
    """A Prometheus community page: one `site_info` row (photos, link, address), no plans. The page data carries
    floor plan *drawings* (`floorplanSvg`, named like `..._1100SQFT_3B2BR_NORTH-3A.svg`), including plans not
    offered now (e.g. Park Place's 3-bedroom drawings, 2026-11-24); the plans actually offered load with JavaScript.
    A drawing is not evidence of a plan, so none is read until a rendered fetch shows the offered plans."""
    text = _script(item, lambda s: s["id"] == "__NEXT_DATA__")
    if not text:
        return []
    apd = json.loads(text)["props"]["pageProps"]["apartmentPageData"]
    ll = apd.get("latLong") or {}
    photos = [g.get("src") for g in ((apd.get("webContents") or {}).get("fields") or {}).get("galleryImages") or []
              if g.get("src")][:16]
    return [Parsed(source_id=f"{site['id']}:site", url=site["url"], price=None, price_basis=None,
                   data=ObservationData(address=apd.get("fullAddress") or site.get("address"), city=apd.get("city"),
                                        lat=ll.get("lat"), lon=ll.get("lon"), kind=SITE_INFO,
                                        property_name=apd.get("name") or site.get("name"), operator="Prometheus",
                                        extra={"site": site["id"], "platform": "prometheus", "images": photos,
                                               # plan name -> drawing URL; plans.plan_image matches it to the
                                               # plans a render reads from the same site
                                               "plan_drawings": {k: v for k, v in (apd.get("floorplanSvg") or {})
                                                                 .items() if isinstance(v, str) and v},
                                               "note": "community website: photos and link; offered plans load "
                                                       "via JavaScript"}))]


# ------------------------------------------------------------------------------- any site: photos only
SITE_INFO = "site_info"  # kind of a row that only carries a site's photos and link: not a listing (no plan, no goal)
NOT_PHOTO = re.compile(r"logo|icon|favicon|sprite|pixel|badge|placeholder|spinner|loader|avatar|equal[-_]?housing|"
                       r"\bada\b|accessib|/maps?/|staticmap|social|facebook|instagram|twitter|google|yelp|"
                       r"gettyimages|istockphoto|istock-|shutterstock|\.svg|\.gif|\.mp4|\.webm|\.m3u8|stream\.mux\.com|^data:", re.IGNORECASE)


CDN_SIZE = re.compile(r"[/,][wh]_(\d+)")  # Cloudinary-style size transforms, e.g. /w_80/ or ,h_80,
NAME_SIZE = re.compile(r"(\d{2,4})x(\d{2,4})")  # sizes in a file name, e.g. logo250x25.png


def site_photos(item: dict[str, Any], site: dict[str, Any] | None = None, limit: int = 24) -> list[str]:
    """Photo URLs from the page's image candidates: largest srcset entry first, absolute, no logos or icons.
    A site's `photo_match` (regex) keeps only matching URLs, for operator pages that mix in company-wide images."""
    keep = re.compile(site["photo_match"]) if site and site.get("photo_match") else None
    from urllib.parse import urljoin

    out: list[str] = []
    names: set[str] = set()  # one copy per file: CDNs repeat a photo at several widths (w_598, w_998, ...)
    for im in item.get("images") or []:
        # largest srcset entry; entries are split on ", " because CDN URLs carry commas (q_auto,f_auto)
        srcset = re.split(r",\s+", (im.get("srcset") or "").strip())[-1].split(" ")[0]
        for raw in (srcset, im.get("src")):
            if not raw or not raw.startswith(("http", "/")):  # a fragment of a comma-split URL: use src
                continue
            u = urljoin(item.get("url") or "", raw.strip())
            text = " ".join(str(im.get(k) or "") for k in ("cls", "alt")) + " " + u
            small = any(str(im.get(k) or "").isdigit() and int(im[k]) < 300 for k in ("w", "h")) \
                or any(int(n) < 300 for n in CDN_SIZE.findall(u)) \
                or any(min(int(a), int(b)) < 100 for a, b in NAME_SIZE.findall(u.rsplit("/", 1)[-1]))
            name = u.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            if u.startswith("http") and not NOT_PHOTO.search(text) and not small and name not in names \
                    and (keep is None or keep.search(u)):
                names.add(name)
                out.append(u)
            break  # srcset's largest entry stands for the element
    return out[:limit]


DRAWING_HINT = re.compile(r"floor[\s_-]*plans?|floorplan", re.IGNORECASE)
# not a drawing: a missing one, a page decoration, or a room photo captioned with the amenity "open floor plan"
NOT_DRAWING = re.compile(r"coming[\s_-]*soon|placeholder|icon|logo|banner|hero|open[\s_-]*floor|^data:", re.IGNORECASE)
WP_SIZE = re.compile(r"-(\d{2,4})x(\d{2,4})(?=\.)")  # WordPress copies: plan-300x249.png beside plan.png


def plan_drawings(item: dict[str, Any]) -> dict[str, str]:
    """Floor plan drawings among a page's images: {label: URL}, label = the image's alt text plus its file name.
    Only images whose alt, class or URL says floor plan; one URL per drawing (the largest of a file's sized copies).
    No plan is read from these: plans.plan_image gives a drawing to a plan only when exactly one label names it."""
    from urllib.parse import unquote, urljoin

    best: dict[str, tuple[int, str]] = {}  # file name without size suffix -> (size, URL) of its largest copy
    alts: dict[str, list[str]] = {}  # every alt text the file's copies carry (WordPress gives it to one copy)
    for im in item.get("images") or []:
        srcset = re.split(r",\s+", (im.get("srcset") or "").strip())[-1].split(" ")[0]
        raw = srcset if srcset.startswith(("http", "/")) else im.get("src") or ""
        if not raw.startswith(("http", "/")) or im.get("from") == "style":  # background images are decoration
            continue
        u = urljoin(item.get("url") or "", raw.strip())
        name = unquote(unquote(u.split("?")[0].rstrip("/").rsplit("/", 1)[-1]))
        hint = " ".join(str(im.get(k) or "") for k in ("alt", "cls")) + " " + u
        if not u.startswith("http") or not DRAWING_HINT.search(hint) or NOT_DRAWING.search(hint):
            continue
        sz = WP_SIZE.search(name)
        size = int(sz.group(1)) * int(sz.group(2)) if sz else 10 ** 9
        key = WP_SIZE.sub("", name)
        if key not in best or size > best[key][0]:
            best[key] = (size, u)
        alt = (im.get("alt") or "").strip()
        if alt and alt not in alts.setdefault(key, []):
            alts[key].append(alt)
    return {" ".join([*alts.get(k, []), k]): u for k, (_, u) in best.items()}


def parse_photos(item: dict[str, Any], site: dict[str, Any]) -> list[Parsed]:
    """A site whose plans and prices we cannot read: one `site_info` row with its photos, on the site's address."""
    photos = site_photos(item, site)
    if not photos:
        return []
    street, city, zip_ = _ld_address(item)
    drawings = plan_drawings(item)
    return [Parsed(source_id=f"{site['id']}:site", url=site["url"], price=None, price_basis=None,
                   data=ObservationData(address=site.get("address") or (f"{street}, {city}, CA {zip_}" if street else None),
                                        kind=SITE_INFO, property_name=site.get("name"),
                                        extra={"site": site["id"], "platform": "photos", "images": photos,
                                               **({"plan_drawings": drawings} if drawings else {}),
                                               "note": "community website: photos and link only"}))]


PARSERS = {"avalon": parse_avalon, "jd_floorplans": parse_jd_floorplans, "prometheus": parse_prometheus,
           "photos": parse_photos}


# --------------------------------------------------------------------- is this page the building's own site?
DIRECTIONS = {"n", "s", "e", "w", "north", "south", "east", "west"}
SUFFIXES = {"st", "street", "ave", "avenue", "rd", "road", "blvd", "dr", "drive", "ct", "court", "way", "ln", "lane",
            "pl", "place", "real", "ter", "cir", "pkwy", "hwy", "el", "la", "de", "del"}


def address_on_page(item: dict[str, Any], address: str | None) -> bool | None:
    """True when the page shows the street number and the street's main name word (e.g. 2400 and 'camino' for
    '2400 W El Camino Real'); None when it cannot be told (no address, or an older raw file without page text)."""
    if not address or not item.get("text"):
        return None
    street = address.split(",")[0].lower()
    num = re.match(r"\s*(\d+)", street)
    words = [w for w in re.findall(r"[a-z]+", street) if w not in DIRECTIONS and w not in SUFFIXES]
    if not num or not words:
        return None
    hay = (item["text"] + " " + " ".join(s["text"] for s in item.get("scripts") or [] if "ld+json" in s["type"])).lower()
    return bool(re.search(rf"\b{num.group(1)}\b", hay)) and max(words, key=len) in hay


def parse(item: dict[str, Any], cfg: dict[str, Any]) -> list[Parsed] | None:
    site = _site_for(item)
    if not site:
        return None
    if site.get("verify_address") and address_on_page(item, site.get("address")) is False:
        return None  # found by search but not this building's page: never attach it (discover records the check)
    return PARSERS[site["platform"]](item, site) or None


ADAPTER = Adapter(name="direct", build_input=build_input, parse=parse, area_kind="site", batch=True,
                  area_of=area_of, required=())
