"""Blueground's own listing JSON, read directly (AGENTS.md rule 6 exception, 2026-11-25, user's decision).

  aptf-intake blueground [--no-breakdown] [--dry-run]

Blueground's website (theblueground.com) loads its search results from its own JSON API; we call the same endpoints
from the machine this runs on, sequentially, `DELAY_S` apart, with a normal browser UA and nothing personal:

- `GET /api/sp?placeId=<market>&marketCode=SFO&ne=<lat,lon>&sw=<lat,lon>&items=18&offset=<page>` lists every unit
  inside a map box, 18 a page (`offset` is the page number; `items` above 18 is refused). The box is the union of
  the station boxes in `config/areas.toml`. Without dates `rent.amount` is 0 and `rent.minDuration` is Blueground's
  cheapest pricing window, not a minimum stay (unit 1895 is returned for a 35-day stay although its window is
  4 months 27 days), so neither is stored as a fact.
- The same search with `checkIn`/`checkOut` returns only the units bookable for exactly those dates, each with
  `rent.amount`: the monthly total for that stay (rent + utilities + insurance; `totalMonthly` changes nothing).
- `GET /api/sp/property/<id>/rent-breakdown?checkIn&checkOut` splits that total: `RENT.itemAmount` (monthly base
  rent, our price), `UTILITIES`, `CLIENT_INSURANCE` (`itemTotal` is the whole stay). One request per quoted
  (unit, stay); `--no-breakdown` skips it and keeps the total as the price (`extra.price_includes_fees`).

Stays (`[blueground] stays` in config/sources.toml): each a name, a term and exact dates; `--stay NAME` limits a run
to some. One observation per (unit, stay), source_id `<cityCode>-<code>:<stay name>` (e.g. `SFO-1895:temp`):
- quoted: price = the monthly base rent for those dates, `query_checkin`/`query_checkout` = the stay (so
  `dates_cover` passes through its existing query path), `extra.total_monthly`, `extra.fees`, `extra.stay_total`;
- a short stay Blueground does not quote for its dates still gets a row (the unit's own record, which settles its
  reposts at the `~blueground` place, filter.goal_inputs): no price, `extra.availability` 'Unavailable for <dates>'
  (filter.unavailable_label), no query dates;
- a long stay not quoted gets no row (it is only an offer).

The run is a full run of source `blueground` (it lists the whole box) and is closed ok only when every page of
every pass came back and each pass gave exactly `totalItems` distinct units; then any Blueground listing not in it
retires (view `source_last_full_run`). Anything else stops with an error and retires nothing (rule 8).
"""

from __future__ import annotations

import json
import time
from datetime import date
from typing import Any, Callable
from urllib.parse import urlencode

from .. import identity, settings
from ..config import load_areas
from ..store import append_observation, finish_run, start_run
from ..store.models import ObservationData
from .base import load_sources

SITE = "https://www.theblueground.com"
SEARCH = SITE + "/api/sp"
BREAKDOWN = SITE + "/api/sp/property/{id}/rent-breakdown"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
DELAY_S = 2.0  # between requests: sequential and polite
ITEMS = 18  # the site's page size; the API refuses more
MAX_PAGES = 60  # a safety stop per pass (60 x 18 = 1,080 units)
# the market's placeId as the site's own SF Bay Area page sends it: 'ar-' + base64url of this JSON. A placeId built
# for another city (Redwood City) is a 404; the map box (`ne`/`sw`) does the narrowing.
MARKET_PLACE = {"type": "city", "lat": 37.7749295, "lng": -122.4194155}
KINDS = {"APARTMENT": "apartment", "HOUSE": "house", "TOWNHOUSE": "townhome"}

# get(url) -> (HTTP status, parsed JSON or None, body head); raises on a network failure
Get = Callable[[str], tuple[int, Any, str]]


class FeedError(RuntimeError):
    """The feed answered with something we cannot trust (rule 8): stop, retire nothing."""


def _httpx_get(url: str) -> tuple[int, Any, str]:
    import httpx

    r = httpx.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30, follow_redirects=True)
    try:
        j = r.json()
    except ValueError:
        j = None
    return r.status_code, j, r.text[:500]


def place_id(place: dict[str, Any] = MARKET_PLACE) -> str:
    import base64

    raw = json.dumps(place, separators=(",", ":")).encode()
    return "ar-" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def feed_config() -> dict[str, Any]:
    """`[blueground]` of sources.toml: market, stays."""
    cfg = load_sources().get("blueground")
    if not cfg or not cfg.get("stays"):
        raise SystemExit("no [blueground] stays in config/sources.toml (see config.example/sources.toml)")
    return cfg


def box() -> list[float]:
    """[south, west, north, east]: the union of the station boxes (config/areas.toml)."""
    bbs = [a.bbox for a in load_areas() if a.kind == "box" and a.bbox]
    if not bbs:
        raise SystemExit("no [[box]] areas in config/areas.toml")
    return [min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs)]


def _num(v: Any) -> float | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n and n > 0 else None


class Client:
    """Sequential requests, `delay` apart, each logged in `log` (the raw file)."""

    def __init__(self, get: Get, delay: float):
        self.get, self.delay, self.n, self.log = get, delay, 0, []

    def __call__(self, url: str) -> Any:
        if self.n:
            time.sleep(self.delay)
        self.n += 1
        status, j, head = self.get(url)
        self.log.append({"url": url, "status": status, **({"body": j} if j is not None else {"head": head})})
        if status in (403, 429):
            raise FeedError(f"blocked: HTTP {status} for {url}: {head[:200]}")
        if status != 200 or j is None:
            raise FeedError(f"HTTP {status} for {url}: {head[:200]}")
        return j


def key_of(u: dict[str, Any]) -> str:
    return f"{u['cityCode']}-{u['code']}"


def search(client: Client, base: dict[str, Any], stay: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every unit of one pass (all pages). Checks that the pages add up to `totalItems` distinct units."""
    q = {**base, **({"checkIn": stay["check_in"], "checkOut": stay["check_out"]} if stay else {})}
    got: dict[str, dict[str, Any]] = {}
    total = None
    for page in range(MAX_PAGES):
        j = client(SEARCH + "?" + urlencode({**q, "items": ITEMS, "offset": page}))
        props = j.get("properties") if isinstance(j, dict) else None
        main = props.get("main") if isinstance(props, dict) else None
        if not isinstance(main, list) or not isinstance(j.get("totalItems"), int):
            raise FeedError(f"search page {page}: no properties.main / totalItems: format changed?")
        total = j["totalItems"]
        for u in main:
            got[key_of(u)] = u
        if len(main) < ITEMS or len(got) >= total:
            break
    else:
        raise FeedError(f"search did not end within {MAX_PAGES} pages")
    if len(got) != total:
        raise FeedError(f"search {'dated ' + stay['name'] if stay else 'undated'}: {len(got)} distinct units, "
                        f"totalItems {total}")
    return list(got.values())


def breakdown(client: Client, pid: Any, stay: dict[str, Any]) -> dict[str, Any]:
    j = client(BREAKDOWN.format(id=pid) + "?" + urlencode({"checkIn": stay["check_in"], "checkOut": stay["check_out"],
                                                           "currency": "USD"}))
    if not isinstance(j, dict) or not isinstance(j.get("lineItems"), list):
        raise FeedError(f"rent-breakdown {pid}: no lineItems: format changed?")
    return j


def split(bd: dict[str, Any]) -> dict[str, Any]:
    """{rent, fees {name: monthly}, total_monthly, stay_total} of one rent breakdown."""
    items = {i.get("itemName"): i for i in bd["lineItems"]}
    rent = _num((items.get("RENT") or {}).get("itemAmount"))
    fees = {str(n).lower(): i.get("itemAmount") for n, i in items.items() if n != "RENT"}
    stay_total = sum(float(i.get("itemTotal") or 0) for i in bd["lineItems"])
    return {"rent": rent, "fees": fees, "total_monthly": _num(bd.get("totalAmount")),
            "stay_total": round(stay_total, 2) or None}


def street_and_name(name: str) -> tuple[str | None, str | None]:
    """('837 Cowper St', 'Cowper Apartments') from 'Cowper Apartments, 837 Cowper St'; a street without a house
    number ('Seale Ave', a partner home) gives no street address."""
    parts = [p.strip() for p in (name or "").split(",") if p.strip()]
    if not parts:
        return None, None
    street = parts[-1]
    prop = ", ".join(parts[:-1]) or None
    return (street if street[:1].isdigit() else None), prop


def unit_data(u: dict[str, Any], stay: dict[str, Any]) -> ObservationData:
    a = u.get("address") or {}
    street, prop = street_and_name(u.get("name") or "")
    code = str(u.get("code") or "")
    city = a.get("city")
    kind = KINDS.get(str(u.get("type") or "").upper())
    extra: dict[str, Any] = {
        "blueground_id": u.get("id"), "code": key_of(u), "stay": stay["name"],
        "street": street or (u.get("name") or None), "neighborhood": a.get("level2"),
        "business_model": u.get("businessModel"), "partner": u.get("partnerSlug"), "list_source": u.get("source"),
        "pricing_window": (u.get("rent") or {}).get("minDuration"),
        "utilities": [x.get("name") for x in u.get("utilities") or []],
        "utilities_included": u.get("utilitiesIncludedInRent"), "junior_bedroom": u.get("hasJuniorBedroom"),
        "images": [p["url"] for p in u.get("photos") or [] if p.get("url")][:24]}
    if kind != "apartment" and _num(u.get("lotSize")):
        extra["lot_size"] = _num(u.get("lotSize"))  # a house's lotSize may be the lot, not the floor area
    lat, lon = a.get("lat"), a.get("lng")
    return ObservationData(
        address=f"{street} #{code}, {city}, CA" if street and city else None, unit=code or None, city=city,
        lat=float(lat) if isinstance(lat, (int, float)) else None,
        lon=float(lon) if isinstance(lon, (int, float)) else None,
        beds=float(u["bedrooms"]) if isinstance(u.get("bedrooms"), (int, float)) else None,
        baths=_num(u.get("bathrooms")), sqft=_num(u.get("lotSize")) if kind == "apartment" else None,
        kind=kind or "entire_place", term=stay["term"], furnished=True, private_bath=True,
        available_from=u.get("availableFrom") or None, property_name=prop, operator="Blueground",
        title=u.get("name"), extra=extra)


def rows(units: list[dict[str, Any]], stays: list[dict[str, Any]], quotes: dict[str, dict[str, dict[str, Any]]],
         splits: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    """[{source_id, data, price, query_checkin, query_checkout, url}] for every (unit, stay) as described above.
    `quotes[stay][key]` is the unit as the dated search returned it; `splits[(key, stay)]` its rent breakdown."""
    out = []
    for u in units:
        key = key_of(u)
        url = f"{SITE}/{u['path']}" if u.get("path") else None
        for s in stays:
            q = quotes.get(s["name"], {}).get(key)
            if q is None and s["term"] != "short":
                continue
            d = unit_data(u, s)
            ex = dict(d.extra or {})
            ex["list_price"] = _num(((q or u).get("baseRent") or {}).get("amount"))
            price = ci = co = None
            if q is None:
                ex["availability"] = f"Unavailable for {s['check_in']} to {s['check_out']}"
            else:
                total = _num((q.get("rent") or {}).get("amount"))
                sp = splits.get((key, s["name"]))
                ci, co = s["check_in"], s["check_out"]
                ex["quote"] = {"check_in": ci, "check_out": co, "total_monthly": total}
                ex["total_monthly"] = total
                if sp and sp["rent"]:
                    price = sp["rent"]
                    ex.update(fees=sp["fees"], stay_total=sp["stay_total"])
                    if sp["total_monthly"] and total and abs(sp["total_monthly"] - total) > 1:
                        ex["breakdown_total"] = sp["total_monthly"]  # the two endpoints disagree: keep both
                else:
                    price = total
                    ex["price_includes_fees"] = True
            d.extra = ex
            out.append({"source_id": f"{key}:{s['name']}", "data": d, "price": price, "query_checkin": ci,
                        "query_checkout": co, "url": url})
    return out


def _count(xs) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[x or "?"] = out.get(x or "?", 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def fetch(get: Get | None = None, *, delay: float = DELAY_S, with_breakdown: bool = True,
          stays: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Every request of one read, no database: {stays, box, units, quotes, splits, log}. Raises FeedError."""
    cfg = feed_config()
    stays = stays if stays is not None else [dict(s) for s in cfg["stays"]]
    s_, w, n, e = box()
    base = {"placeId": place_id(), "marketCode": cfg.get("market", "SFO"), "currency": "USD", "language": "en",
            "ne": f"{n},{e}", "sw": f"{s_},{w}"}
    client = Client(get or _httpx_get, delay)
    out: dict[str, Any] = {"stays": stays, "box": [s_, w, n, e], "log": client.log}
    units = search(client, base)
    if not units:
        raise FeedError("the undated search found no unit in the box (rule 8: stop and ask)")
    keys = {key_of(u) for u in units}
    quotes: dict[str, dict[str, dict[str, Any]]] = {}
    for s in stays:
        quotes[s["name"]] = {key_of(u): u for u in search(client, base, s)}
        for k in sorted(set(quotes[s["name"]]) - keys):  # bookable but not listed undated: list it anyway
            units.append(quotes[s["name"]][k])
            keys.add(k)
    splits: dict[tuple[str, str], dict[str, Any]] = {}
    if with_breakdown:
        for s in stays:
            for k, u in quotes[s["name"]].items():
                splits[(k, s["name"])] = split(breakdown(client, u["id"], s))
    out.update(units=units, quotes=quotes, splits=splits, requests=client.n)
    return out


def store(conn, got: dict[str, Any], *, raw_path=None, run: int | None = None) -> dict[str, Any]:
    """Append one full run from a fetch (or a saved raw file's fetch) and link it."""
    stays = got["stays"]
    if run is None:
        run = start_run(conn, "intake", "blueground", {"scope": "full", "endpoint": SEARCH, "box": got["box"],
                                                       "stays": stays, "breakdown": bool(got["splits"]),
                                                       "date": date.today().isoformat()})
    out = rows(got["units"], stays, got["quotes"], got["splits"])
    for r in out:
        append_observation(conn, run, "blueground", r["source_id"], r["data"], url=r["url"], price=r["price"],
                           price_basis="month" if r["price"] else None, query_checkin=r["query_checkin"],
                           query_checkout=r["query_checkout"], raw_path=str(raw_path) if raw_path else None,
                           commit=False)
    conn.commit()
    counts = {"units": len(got["units"]), "observations": len(out), "requests": got.get("requests"),
              "quoted": {s["name"]: len(got["quotes"][s["name"]]) for s in stays},
              "cities": _count((u.get("address") or {}).get("city") for u in got["units"])}
    finish_run(conn, run, "ok", counts, est_cost=0.0)
    identity.run(conn)
    return {"run_id": run, **counts}


def save_raw(got: dict[str, Any], path) -> None:
    """The whole fetch, so `store` can be replayed from it (`load_raw`) without a request."""
    path.write_text(json.dumps({**{k: got[k] for k in ("stays", "box", "units", "quotes", "requests", "log")
                                   if k in got},
                                "splits": [[k, s, v] for (k, s), v in (got.get("splits") or {}).items()]}))


def load_raw(path) -> dict[str, Any]:
    j = json.loads(path.read_text())
    j["splits"] = {(k, s): v for k, s, v in j.get("splits") or []}
    return j


def read(conn, *, get: Get | None = None, delay: float = DELAY_S, with_breakdown: bool = True,
         dry_run: bool = False, only: list[str] | None = None, from_raw: str | None = None) -> dict[str, Any]:
    """`aptf-intake blueground`: fetch, save the raw file, store one full run. `only`: stay names to keep (a run
    with fewer stays is still full: every unit gets its short-stay row). `from_raw`: replay a saved fetch."""
    from pathlib import Path

    cfg = feed_config()
    stays = [dict(s) for s in cfg["stays"]]
    for s in stays:
        if s.get("term") not in ("short", "long") or not s.get("name") or s["check_in"] >= s["check_out"]:
            raise SystemExit(f"bad [blueground] stay: {s}")
    if only:
        stays = [s for s in stays if s["name"] in only]
        if not any(s["term"] == "short" for s in stays):
            raise SystemExit("a run needs a short stay (it carries every unit's row)")
    if dry_run:
        return {"box": box(), "stays": stays}
    raw_dir = settings.RAW_DIR / "blueground"
    raw_dir.mkdir(parents=True, exist_ok=True)
    if from_raw:
        got = load_raw(Path(from_raw))
        names = {s["name"] for s in stays}
        got["stays"] = [s for s in got["stays"] if s["name"] in names]
        got["quotes"] = {n: q for n, q in got["quotes"].items() if n in names}
        got["splits"] = {k: v for k, v in got["splits"].items() if k[1] in names}
        return {"raw": from_raw, **store(conn, got, raw_path=from_raw)}
    run = start_run(conn, "intake", "blueground", {"scope": "full", "endpoint": SEARCH, "box": box(),
                                                   "stays": stays, "breakdown": with_breakdown,
                                                   "date": date.today().isoformat()})
    raw_path = raw_dir / f"run{run}.json"
    try:
        got = fetch(get, delay=delay, with_breakdown=with_breakdown, stays=stays)
    except BaseException as ex:
        conn.commit()
        finish_run(conn, run, "error", {}, est_cost=0.0, error=repr(ex)[:500])
        raise
    save_raw(got, raw_path)
    return {"raw": str(raw_path), **store(conn, got, raw_path=raw_path, run=run)}
