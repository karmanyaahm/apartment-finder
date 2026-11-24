"""Address parts read by Claude Haiku, cached per address string, for building identity.

  aptf-identity normalize [--limit N] [--dry-run]

Sources write one building's address many ways ('234 Escuela Avenue Mountain View, CA' without a comma, a unit in
its own comma slot, 'El Camino Real', 'W.' vs 'West'). The hand-written parser (`store.keys.parse_address`) misses
some of them; Haiku reads each distinct address string into parts (number, direction, street name, street type,
unit, city, ZIP) and the parts are checked against the original before use:

- the street number must appear in the original;
- every street-name word must appear in the original (abbreviations expanded on both sides);
- the city is kept only if it is written in the address.

A failed check keeps the parser's reading. Answers are cached by address string in `data/address_cache.json`, so a
given string always gets the same key and only new strings cost anything. The place key is built with the same
formula as before (normalized 'number direction name type' plus city), so clean addresses keep their existing keys.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Any

from .. import settings
from ..store.keys import _h, _norm_tokens

CACHE = "address_cache.json"
SECRET_ENV = ("APIFY_TOKEN", "GOOGLE_MAPS_API_KEY")
SYSTEM = ("You split US street addresses into their parts, exactly as written. Do not correct, complete or guess: "
          "if a part is not in the address, answer null. 'El Camino Real' is a street name. Answer in the JSON.")
_S = {"type": ["string", "null"]}
SCHEMA = {"type": "object", "required": ["results"], "properties": {"results": {"type": "array", "items": {
    "type": "object", "required": ["key"],
    "properties": {"key": {"type": "string"}, "number": _S, "predirectional": _S, "street_name": _S,
                   "street_type": _S, "postdirectional": _S, "unit": _S, "city": _S, "zip": _S}}}}}


def cache_path():
    return settings.DATA_DIR / CACHE


def load_cache() -> dict[str, dict[str, Any]]:
    p = cache_path()
    return json.loads(p.read_text()) if p.exists() else {}


def _toks(s: str) -> list[str]:
    return _norm_tokens(s or "").split()


def check_parts(parts: dict[str, Any], address: str) -> dict[str, Any] | None:
    """The parts if the original supports them, else None (keep the parser's reading)."""
    orig = set(_toks(address))
    num = (parts.get("number") or "").strip()
    name = (parts.get("street_name") or "").strip()
    if not num or not name or not re.match(r"^\d", num) or num.lower() not in address.lower():
        return None
    words = [*_toks(parts.get("predirectional") or ""), *_toks(name), *_toks(parts.get("street_type") or ""),
             *_toks(parts.get("postdirectional") or "")]
    if not words or any(w not in orig for w in words):
        return None
    city = (parts.get("city") or "").strip()
    city_ok = city and all(w in orig for w in _toks(city))
    return {"street": " ".join([*_toks(num), *words]), "city": " ".join(_toks(city)) if city_ok else None,
            "unit": (parts.get("unit") or "").strip().lstrip("#").strip().lower() or None}


def place_key_from(parts: dict[str, Any], city_fallback: str | None) -> str | None:
    """Same formula as `store.keys.place_key` for an address: 'p_' + hash('street|city')."""
    city = parts.get("city") or (_norm_tokens(city_fallback) if city_fallback else None)
    # "881-881 E El Camino Real": a one-number range is that number, as in the parser ("4180-4190" stays)
    street = re.sub(r"^(\d+)-\1\b", r"\1", parts.get("street") or "")
    return "p_" + _h(f"{street}|{city or ''}") if street else None


def argv(model: str) -> list[str]:
    return ["claude", "-p", "--system-prompt", SYSTEM, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--disable-slash-commands", "--model", model, "--no-session-persistence", "--output-format", "json",
            "--json-schema", json.dumps(SCHEMA), "--max-budget-usd", "0.30"]


def ask(batch: list[str], model: str) -> tuple[list[dict[str, Any]], float]:
    prompt = "Split each address into parts. One result per key.\n" + "\n".join(
        f"- key {i}: {a}" for i, a in enumerate(batch))
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(argv(model) + [prompt], capture_output=True, text=True, cwd=cwd, env=env, timeout=600)
    out = json.loads(proc.stdout or "{}")
    if out.get("is_error") or out.get("subtype") != "success" or not out.get("structured_output"):
        raise RuntimeError(f"claude call failed: {out.get('subtype')} {str(out.get('result'))[:200]}")
    return out["structured_output"].get("results") or [], float(out.get("total_cost_usd") or 0)


def normalize(conn, *, limit: int = 5000, batch: int = 50, model: str = "haiku", max_usd: float = 0.5,
              dry_run: bool = False) -> dict[str, Any]:
    cache = load_cache()
    todo = [r[0] for r in conn.execute(
        "SELECT DISTINCT address FROM listings WHERE address IS NOT NULL AND address != '' ORDER BY address")
            if r[0] not in cache][:limit]
    if dry_run:
        return {"new_addresses": len(todo), "cached": len(cache), "calls": -(-len(todo) // batch)}
    spent, kept, fell_back = 0.0, 0, []
    for i in range(0, len(todo), batch):
        if spent >= max_usd:
            break
        group = todo[i:i + batch]
        answers, cost = ask(group, model)
        spent += cost
        by_key = {a.get("key"): a for a in answers}
        for j, addr in enumerate(group):
            parts = check_parts(by_key.get(str(j)) or {}, addr)
            cache[addr] = parts or {"fallback": True}
            if parts:
                kept += 1
            else:
                fell_back.append(addr)
        cache_path().write_text(json.dumps(cache, indent=0, sort_keys=True))
    return {"asked": len(todo), "kept": kept, "fallback": len(fell_back), "fallback_examples": fell_back[:10],
            "claude_usd": round(spent, 4)}


def suspected_duplicates(conn, min_similarity: float = 0.85, max_m: float = 150.0) -> list[dict[str, Any]]:
    """Address-keyed buildings (third-party stay places excluded: separate on purpose) with the same street number,
    street names at least `min_similarity` alike and within `max_m` metres, that still have different keys."""
    import difflib

    from ..commute.geo import haversine_m

    rows = [dict(r) for r in conn.execute(
        "SELECT place_key, address, property_name, lat, lon, n_listings FROM places WHERE place_key LIKE 'p_%' "
        "AND place_key NOT LIKE '%~%' AND address IS NOT NULL AND lat IS NOT NULL")]
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


# ------------------------------------------------------------------ Google geocoder: the authoritative reading
GEO_CACHE = "geocode_cache.json"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
PRECISE = {"street_address", "premise", "subpremise"}


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
    one written in the address (a vague or different match keeps the other readings)."""
    if not PRECISE & set(result.get("types") or []):
        return None
    comp = {t: c for c in result.get("address_components") or [] for t in c.get("types") or []}
    num = (comp.get("street_number") or {}).get("long_name")
    route = (comp.get("route") or {}).get("long_name")
    city = (comp.get("locality") or comp.get("sublocality") or {}).get("long_name")
    if not num or not route or num != _lead_number(address):
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
