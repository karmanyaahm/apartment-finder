"""Building identity from addresses: Google's geocoder reads each address string, the parser is the fallback.

  aptf-identity geocode [--limit N] [--dry-run]
  aptf-identity duplicates

Sources write one building's address many ways ('234 Escuela Avenue Mountain View, CA' without a comma, 'Infinity Wy',
a unit in its own comma slot, a city that is really the search area). Google's Geocoding API reads each distinct
address string once; its answer counts only if it is street-level and its house number is the one written in the
address, and then the place key uses Google's street and city with the same formula as the parser
(`store.keys.place_key`: 'street|city'), so clean addresses keep their keys. Answers are cached by address string in
`data/geocode_cache.json`; every call is metered in `api_calls` and capped (config/google.toml). Anything Google
cannot place precisely keeps the parser's reading. `suspected_duplicates` is the standing check on the result.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .. import settings
from ..store.keys import _h, _norm_tokens, house_numbers

GEO_CACHE = "geocode_cache.json"


GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


PRECISE = {"street_address", "premise", "subpremise"}



def _toks(s: str) -> list[str]:
    return _norm_tokens(s or "").split()



def place_key_from(parts: dict[str, Any], city_fallback: str | None) -> str | None:
    """Same formula as `store.keys.place_key` for an address: 'p_' + hash('street|city')."""
    city = parts.get("city") or (_norm_tokens(city_fallback) if city_fallback else None)
    # "881-881 E El Camino Real": a one-number range is that number, as in the parser ("4180-4190" stays)
    street = re.sub(r"^(\d+)-\1\b", r"\1", parts.get("street") or "")
    return "p_" + _h(f"{street}|{city or ''}") if street else None



def geo_cache_path():
    return settings.DATA_DIR / GEO_CACHE



def load_geocodes() -> dict[str, dict[str, Any]]:
    p = geo_cache_path()
    return json.loads(p.read_text()) if p.exists() else {}



def _lead_number(address: str) -> str | None:
    m = re.match(r"\s*(\d+)(?:-(\d+))?", address or "")
    if not m:
        return None
    return m.group(1) if not m.group(2) or m.group(2) == m.group(1) else f"{m.group(1)}-{m.group(2)}"



def geocode_parts(result: dict[str, Any], address: str) -> dict[str, Any] | None:
    """Google's canonical street and city for an address, only for a street-level match whose house number is the
    one written in the address, or one of a number list's (a vague or different match keeps the other readings)."""
    if not PRECISE & set(result.get("types") or []):
        return None
    comp = {t: c for c in result.get("address_components") or [] for t in c.get("types") or []}
    num = (comp.get("street_number") or {}).get("long_name")
    route = (comp.get("route") or {}).get("long_name")
    city = (comp.get("locality") or comp.get("sublocality") or {}).get("long_name")
    # a number-list address ('1024, 1026, 1028 Middle Avenue') takes Google's pick among its numbers (1028)
    if not num or not route or (num != _lead_number(address) and num.lower() not in house_numbers(address)):
        return None
    return {"street": " ".join(_toks(f"{num} {route}")), "city": " ".join(_toks(city)) if city else None,
            "zip": (comp.get("postal_code") or {}).get("long_name"), "place_id": result.get("place_id"),
            "formatted": result.get("formatted_address")}



def geocode(conn, *, limit: int = 5000, run_cap: int = 2000, dry_run: bool = False) -> dict[str, Any]:
    """Geocode each address string not geocoded yet. Every call is logged in `api_calls` (provider 'geocoding')
    and checked against `geocoding_monthly_call_cap` in config/google.toml (default 4,900, under the 10,000 free
    calls a month) and this run's cap."""
    import httpx

    from ..store import now_iso
    from ..trips import load_google_caps

    cache = load_geocodes()
    todo = [r[0] for r in conn.execute(
        "SELECT DISTINCT address FROM listings WHERE address IS NOT NULL AND address != '' ORDER BY address")
            if r[0] not in cache and _lead_number(r[0])][:limit]
    month = datetime_month()
    used = conn.execute("SELECT COUNT(*) FROM api_calls WHERE provider = 'geocoding' AND ts LIKE ?",
                        (f"{month}%",)).fetchone()[0]
    cap = int(load_google_caps().get("geocoding_monthly_call_cap", 4900))
    if dry_run:
        return {"to_geocode": len(todo), "cached": len(cache), "used_this_month": used, "monthly_cap": cap}
    key = settings.secret("GOOGLE_MAPS_API_KEY")
    if not key:
        raise SystemExit("GOOGLE_MAPS_API_KEY is not set in .env")
    calls, kept, vague = 0, 0, []
    with httpx.Client(timeout=30) as http:
        for addr in todo:
            if calls >= run_cap or used + calls >= cap:
                break
            ok = 0
            try:
                r = http.get(GEOCODE_URL, params={"address": addr, "components": "country:US", "key": key})
                body = r.json()
                status = body.get("status")
                if status in ("REQUEST_DENIED", "OVER_DAILY_LIMIT", "OVER_QUERY_LIMIT"):
                    raise SystemExit(f"BLOCKED: Geocoding API answered {status}: {body.get('error_message', '')}")
                ok = 1
            finally:
                conn.execute("INSERT INTO api_calls (ts, provider, mode, ok) VALUES (?, 'geocoding', 'geocode', ?)",
                             (now_iso(), ok))
                conn.commit()
                calls += 1
            parts = next((p for res in body.get("results") or [] if (p := geocode_parts(res, addr))), None)
            cache[addr] = parts or {"fallback": True, "status": status,
                                    "first": ((body.get("results") or [{}])[0]).get("formatted_address")}
            if parts:
                kept += 1
            else:
                vague.append(addr)
            if calls % 50 == 0:
                geo_cache_path().write_text(json.dumps(cache, indent=0, sort_keys=True))
    geo_cache_path().write_text(json.dumps(cache, indent=0, sort_keys=True))
    return {"calls": calls, "kept": kept, "kept_other_reading": len(vague), "examples_not_kept": vague[:10],
            "used_this_month": used + calls, "monthly_cap": cap}



def datetime_month() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m")



def suspected_duplicates(conn, min_similarity: float = 0.85, max_m: float = 150.0,
                         places: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Address-keyed buildings (third-party stay places excluded: separate on purpose) with the same street number,
    street names at least `min_similarity` alike and within `max_m` metres, that still have different keys.
    `places`: rows of the places view already read (the view is computed over everything)."""
    import difflib

    from ..commute.geo import haversine_m

    if places is None:
        places = [dict(r) for r in conn.execute("SELECT place_key, address, property_name, lat, lon, n_listings "
                                                "FROM places")]
    rows = [dict(r) for r in places if r["place_key"].startswith("p_") and "~" not in r["place_key"]
            and r.get("address") is not None and r.get("lat") is not None]
    by_num: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        m = re.match(r"(\d+)\s+(.*)", " ".join(_toks(r["address"].split(",")[0])))
        if m:
            r["street"] = m.group(2)
            by_num.setdefault(m.group(1), []).append(r)
    out = []
    for rs in by_num.values():
        for i, a in enumerate(rs):
            for b in rs[i + 1:]:
                sim = difflib.SequenceMatcher(None, a["street"], b["street"]).ratio()
                d = haversine_m((a["lat"], a["lon"]), (b["lat"], b["lon"]))
                if sim >= min_similarity and d <= max_m:
                    out.append({"similarity": round(sim, 2), "metres": round(d), "a": a["address"], "b": b["address"],
                                "a_key": a["place_key"], "b_key": b["place_key"],
                                "listings": [a["n_listings"], b["n_listings"]]})
    return sorted(out, key=lambda p: (-p["similarity"], p["metres"]))

