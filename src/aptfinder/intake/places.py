"""Google Places API (New) Text Search: apartment communities per station box, as buildings (`site_info` rows).

  aptf-intake run google_places [--area ID] [--dry-run]

Not a listing site, an official API (AGENTS.md rule 6 allows calling it directly). Each station walk-zone box
(`[[box]]` in config/areas.toml) is searched with `locationRestriction` = the box, once per query in
`[google_places] queries` (config/sources.toml): a text plus a Table A `includedType` with strict type filtering,
so only places Google types as that kind come back. A query returns at most 60 places (3 pages of 20); a query that
fills all 3 pages marks its box partial (there may be more).

Every request is one billed call (each page too), checked against `places_monthly_call_cap` in config/google.toml
and the run's `max_calls`, and logged in `api_calls` with provider `places` (Routes calls are `google`; separate caps).
The field mask is fixed (FIELD_MASK); websiteUri, nationalPhoneNumber and rating put every call in the Text Search
Enterprise SKU (pricing in design.md).

Each place becomes one `site_info` row (a building: name, address, coordinates, website, phone, rating; never an
offer), source `google_places`, source_id the place id, url its Google Maps page. Permanently closed places are
skipped. The website (`extra.website`) is a lead for `aptf-intake promote-sites`.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

import httpx

from .. import settings
from ..store import now_iso
from ..store.models import ObservationData
from .apify import ActorRun
from .base import Adapter, Parsed
from .direct import SITE_INFO
from .discover import clean_url

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ["id", "displayName", "formattedAddress", "location", "websiteUri", "nationalPhoneNumber", "rating",
          "userRatingCount", "businessStatus", "types", "primaryType", "googleMapsUri"]
FIELD_MASK = ",".join(f"places.{f}" for f in FIELDS) + ",nextPageToken"
PAGES = 3  # the API's limit: 60 results per query
PROVIDER = "places"


class PlacesCapReached(RuntimeError):
    pass


def places_usage(conn: sqlite3.Connection, month: str | None = None) -> int:
    from datetime import datetime, timezone

    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    return conn.execute("SELECT COUNT(*) FROM api_calls WHERE provider = ? AND ts LIKE ?",
                        (PROVIDER, f"{month}%")).fetchone()[0]


def _caps() -> dict[str, Any]:
    from ..trips import load_google_caps

    return load_google_caps()


def search_box(conn: sqlite3.Connection, key: str, query: dict[str, Any], bbox: list[float], max_calls: int,
               calls: list[int], post=None) -> tuple[list[dict[str, Any]], bool]:
    """All pages of one query in one box. Returns (places, truncated). `calls` is a one-element counter shared by
    the run; every request is checked against the caps first and logged in api_calls."""
    caps = _caps()
    monthly = caps.get("places_monthly_call_cap", 0)
    s, w, n, e = bbox
    body: dict[str, Any] = {"textQuery": query["text"], "pageSize": 20,
                            "locationRestriction": {"rectangle": {"low": {"latitude": s, "longitude": w},
                                                                  "high": {"latitude": n, "longitude": e}}}}
    if query.get("type"):
        body["includedType"] = query["type"]
        body["strictTypeFiltering"] = True
    post = post or (lambda b: httpx.post(URL, json=b, timeout=30, headers={
        "X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASK, "Content-Type": "application/json"}))
    out: list[dict[str, Any]] = []
    for page in range(PAGES):
        if calls[0] >= max_calls:
            raise PlacesCapReached(f"reached this run's cap of {max_calls} Places calls")
        if places_usage(conn) >= monthly:
            raise PlacesCapReached(f"monthly cap of {monthly} Places calls reached (config/google.toml)")
        calls[0] += 1
        ok = 0
        try:
            r = post(body)
            ok = int(r.status_code == 200)
        finally:
            conn.execute("INSERT INTO api_calls (ts, provider, mode, ok, note) VALUES (?, ?, ?, ?, ?)",
                         (now_iso(), PROVIDER, "text_search", ok, f"{query['text']}|{query.get('type') or ''}|p{page + 1}"))
            conn.commit()
        if r.status_code != 200:
            raise RuntimeError(f"Places API HTTP {r.status_code}: {r.text[:400]}")
        d = r.json()
        out += [{**p, "_query": query} for p in d.get("places") or []]
        tok = d.get("nextPageToken")
        if not tok:
            return out, False
        body = {**body, "pageToken": tok}
    return out, True  # all pages full and still a next page: there may be more


def fetch(cfg: dict[str, Any], areas: list, cap_each: int, max_usd: float, conn: sqlite3.Connection,
          post=None) -> ActorRun:
    key = settings.secret("GOOGLE_MAPS_API_KEY")
    if not key and post is None:
        raise SystemExit("GOOGLE_MAPS_API_KEY is not set in .env")
    calls = [0]  # one counter for the whole run (all boxes): `max_calls` is per run
    items: dict[str, dict[str, Any]] = {}
    truncated: list[str] = []
    status = "SUCCEEDED"
    for a in areas:
        for q in cfg.get("queries") or [{"text": "apartments", "type": "apartment_complex"}]:
            try:
                found, more = search_box(conn, key, q, a.bbox, cfg.get("max_calls", 30), calls, post)
            except PlacesCapReached as e:  # keep what was paid for; the run is not complete (warned, areas partial)
                status = f"CAP_REACHED: {e}"
                break
            if more and a.id not in truncated:
                truncated.append(a.id)
            for p in found:
                if p.get("id") in items:  # the same place from another query or an overlapping box: keep one
                    items[p["id"]]["_queries"].append(q)
                else:
                    items[p["id"]] = {**p, "_box": a.id, "_queries": [q]}
        if status != "SUCCEEDED":
            break
    return ActorRun(actor="google places api", run_id="", status=status, items=list(items.values()),
                    cost_usd=0.0, charged_events=None, console_url="", truncated_areas=truncated)


def build_input(cfg: dict[str, Any], areas: list) -> dict[str, Any]:  # unused: fetch replaces the actor input
    return {}


ADDR = re.compile(r"^(?P<street>[^,]+),\s*(?P<city>[^,]+),\s*[A-Z]{2}\s*(?P<zip>\d{5})?")


def parse(item: dict[str, Any], cfg: dict[str, Any]) -> list[Parsed] | None:
    pid = item.get("id")
    if not pid:
        return None
    addr = re.sub(r",\s*USA$", "", item.get("formattedAddress") or "") or None
    m = ADDR.match(addr or "")
    loc = item.get("location") or {}
    website = clean_url(item.get("websiteUri"))[0]
    return [Parsed(
        source_id=pid, url=item.get("googleMapsUri"), price=None, price_basis=None,
        data=ObservationData(
            address=addr, city=m.group("city").strip() if m else None, zip=m.group("zip") if m else None,
            lat=loc.get("latitude"), lon=loc.get("longitude"), kind=SITE_INFO,
            property_name=(item.get("displayName") or {}).get("text"),
            extra={"website": website, "website_raw": item.get("websiteUri"), "phone": item.get("nationalPhoneNumber"),
                   "rating": item.get("rating"), "rating_count": item.get("userRatingCount"),
                   "business_status": item.get("businessStatus"), "types": item.get("types"),
                   "primary_type": item.get("primaryType"), "google_maps_uri": item.get("googleMapsUri"),
                   "queries": [f"{q.get('text')}|{q.get('type') or ''}" for q in item.get("_queries") or []],
                   "note": "Google Places: the building (name, address, website, phone); not an offer"}))]


def area_of(item: dict[str, Any], ids: list[str]) -> str | None:
    return item.get("_box") if item.get("_box") in ids else None


# batch: one fetch for every box of the run, so the per-run call cap spans all boxes
ADAPTER = Adapter(name="google_places", build_input=build_input, parse=parse, area_kind="box", batch=True,
                  area_of=area_of, required=(), skip=lambda i: i.get("businessStatus") == "CLOSED_PERMANENTLY",
                  fetch=fetch, apify=False)
