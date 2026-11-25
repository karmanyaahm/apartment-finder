"""Zillow detail pages: the description, "what's special" and the structured facts (fees, pets, laundry, parking,
community features, utilities) that Zillow's search cards lack, one page per zpid, through
`delicious_zebu/zillow-property-details-scraper` (input `{"zpids": [...]}`).

  aptf-intake zillow-detail [--max-usd 1.0] [--batch 150] [--limit N] [--refetch] [--dry-run]

An enrichment of listings we already hold, not a source of listings: a run stores raw pages only
(`data/raw/zillow_detail/run<id>_<k>.json`) and writes no observations, so it creates no listing or place, is never
coverage of an area and never retires anything (`scope = targeted`; `zillow_detail` is not in `ADAPTERS`). The text
reading (`extract zillow`) finds a listing's newest page by its `extra.zpid`; a building's bedroom rows (`<zpid>:Nbd`)
share one page, whose beds, baths and price belong to one representative unit and are never read. A single unit's
row (no ':') is that page's own unit: its empty beds, baths, sqft and price are filled from it (`unit_facts`).

A page whose `home_status` is an explicit non-rental status (FOR_SALE, SOLD, OFF_MARKET, ...) is recorded in the run's
`counts.home_status`; Verify lists active listings whose newest page says so (`verify.stale_queue`).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .. import settings
from ..store import finish_run, start_run
from .base import load_sources, spend_room

SOURCE = "zillow_detail"
ACTOR = "delicious_zebu/zillow-property-details-scraper"
RENTAL = {"FOR_RENT"}  # any other explicit status means the listing is no longer for rent here
BATCH = 150  # zpids per actor run: a timeout (run_actor aborts at timeout_s) loses little
TIMEOUT_S = 1800


def zpid_of(item: dict[str, Any]) -> str | None:
    """The page's zpid: `zillow_id` (the item's `zpid` is empty), else from its URL."""
    z = item.get("zillow_id")
    if z:
        return str(z)
    m = re.search(r"/(\d+)_zpid", str(item.get("original_url") or ""))
    return m.group(1) if m else None


def raw_files() -> list[Path]:
    """Detail raw files, oldest run first (so later pages win)."""
    d = settings.RAW_DIR / SOURCE

    def key(p: Path) -> tuple[int, str]:
        m = re.match(r"run(\d+)_", p.name)
        return (int(m.group(1)) if m else 0, p.name)

    return sorted(d.glob("run*.json"), key=key) if d.is_dir() else []


def load_pages() -> dict[str, dict[str, Any]]:
    """zpid -> its newest detail page."""
    out: dict[str, dict[str, Any]] = {}
    for p in raw_files():
        for it in json.loads(p.read_text()).get("items") or []:
            if isinstance(it, dict) and (z := zpid_of(it)):
                out[z] = it
    return out


_LISTERS: dict[Any, dict[str, list[str]]] = {}


def page_listers() -> dict[str, list[str]]:
    """zpid -> the lister names its newest detail page gives (`attribution_info` brokerName and agentName, e.g.
    'Blueground Furnished Apartments', 'Blueground US'); read from the raw pages, cached until a raw file changes.
    The filter matches them against `config/operators.toml` names (filter.listing_operators)."""
    key = tuple((str(p), p.stat().st_mtime) for p in raw_files())
    if key not in _LISTERS:
        out = {}
        for z, it in load_pages().items():
            a = it.get("attribution_info") or {}
            if names := [str(a[k]) for k in ("brokerName", "agentName") if a.get(k)]:
                out[z] = names
        _LISTERS.clear()
        _LISTERS[key] = out
    return _LISTERS[key]


# Structured facts sent to the text reading as plain lines. Building-level or stated policy only: no beds, baths,
# area, price, dates or deposit (on a building's page those are one representative unit's). On a building's page
# the UNIT_LINES and the furnished flag are that one unit's too, so a building's bedroom rows never get them.
RESO_LINES = (("feesAndDues", "Fees"), ("allowedPets", "Pets allowed"), ("laundryFeatures", "Laundry"),
              ("appliances", "Appliances"), ("cooling", "Cooling"), ("parkingFeatures", "Parking"),
              ("communityFeatures", "Community features"), ("associationAmenities", "Amenities"),
              ("poolFeatures", "Pool"), ("utilities", "Utilities"), ("leaseTerm", "Lease term"))
UNIT_LINES = {"laundryFeatures", "appliances", "cooling"}
PLACEHOLDER = re.compile(r"^(contact|call|ask|see|none|n/?a|other|unknown)\b", re.I)


def _vals(v: Any) -> list[str]:
    if isinstance(v, dict):
        v = [f"{v.get('type')}: {v.get('fee')}" if "fee" in v else ", ".join(str(x) for x in v.values() if x)]
    if not isinstance(v, list):
        v = [v]
    out = []
    for x in v:
        if isinstance(x, dict):
            x = f"{x.get('type')}: {x.get('fee')}" if "fee" in x else ", ".join(str(y) for y in x.values() if y)
        if isinstance(x, bool) or x is None:
            continue
        s = str(x).strip()
        if s and not PLACEHOLDER.match(s):
            out.append(s)
    return out


def detail_lines(item: dict[str, Any], building: bool = False) -> list[tuple[str, Any]]:
    """(label, value) pairs of a detail page for the text reading (`extract.raw_lines('zillow', ...)`). `building`:
    the page is read for a building's bedroom row (`<zpid>:Nbd`), so the unit-level flags are left out; the
    description and what's special stay (a stated 'all units furnished' is still read, with its quote)."""
    rf = dict(item.get("reso_facts") or {})
    # the security deposit is the representative unit's on a building's page; fees by type are the building's
    rf["feesAndDues"] = [f for f in rf.get("feesAndDues") or [] if not (isinstance(f, dict)
                                                                         and str(f.get("type")).lower() == "deposit")]
    out: list[tuple[str, Any]] = [("Description", (item.get("description") or "").strip()),
                                  ("What's special", item.get("whats_special"))]
    out += [(label, "; ".join(v)) for key, label in RESO_LINES
            if not (building and key in UNIT_LINES) and (v := _vals(rf.get(key)))]
    if rf.get("furnished") is True and not building:  # `false` is on almost every page, a default, not a statement
        out.append(("Furnished", "yes"))
    return out


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def unit_facts(item: dict[str, Any]) -> dict[str, float]:
    """beds, baths, sqft and price of a single unit's own page (a row whose source_id has no ':'), for the fields
    its search card left empty (an `apartmentComplex` card without `unitsGroup` gives none: 411 Forest Ave, 734
    Cowper St). Never for a building's bedroom rows: their page is one representative unit's. Baths = full + half / 2
    (as `lease.zillow_parse`), else the page's total. Price only on a FOR_RENT page and above 0 ('Price on Request'
    pages say 0: unknown); never the rent Zestimate."""
    rf = item.get("reso_facts") or {}
    out: dict[str, float] = {}
    beds = _num(item.get("bedrooms"))
    if beds is not None and 0 <= beds <= 10:
        out["beds"] = beds
    full, half = _num(rf.get("bathroomsFull")), _num(rf.get("bathroomsHalf"))
    baths = full + (half or 0) / 2 if full else _num(item.get("bathrooms"))
    if baths is not None and 0 < baths <= 10:
        out["baths"] = baths
    sqft = _num(item.get("living_area"))
    if sqft is not None and 200 <= sqft <= 20000:  # 892 Gallatin Dr, 2 bd / 2 ba: '110' (a typo): unknown
        out["sqft"] = sqft
    price = _num(item.get("listing_price"))
    if item.get("home_status") in RENTAL and price is not None and price > 0:
        out["price"] = price
    return out


def last_event(item: dict[str, Any]) -> str | None:
    """The page's newest price-history event, e.g. '2026-11-25 Listing removed $2,300': a FOR_SALE page can still
    be listed for rent (104 Evandale Ave: listed for sale 11-11, for rent 11-23), an OTHER one is often removed."""
    hist = [h for h in item.get("price_history") or [] if isinstance(h, dict) and h.get("date")]
    if not hist:
        return None
    h = max(hist, key=lambda h: str(h["date"]))
    price = f" ${h['price']:,.0f}" if isinstance(h.get("price"), (int, float)) else ""
    return f"{h['date']} {h.get('event') or ''}{price}".strip()


def active_zpids(conn: sqlite3.Connection) -> list[str]:
    """The zpids of every active Zillow listing (a building's bedroom rows share one)."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT CAST(json_extract(lo.data, '$.extra.zpid') AS TEXT) FROM latest_obs lo "
        "JOIN listings l ON l.source = lo.source AND l.source_id = lo.source_id "
        "WHERE lo.source = 'zillow' AND l.active = 1 AND json_extract(lo.data, '$.extra.zpid') IS NOT NULL "
        "ORDER BY 1")]


def fetch(conn: sqlite3.Connection, *, max_usd: float, batch: int = BATCH, limit: int | None = None,
          refetch: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Fetch the detail page of every active Zillow listing without one yet (`refetch`: all of them), `batch` zpids
    per actor run, under the budget guard before each run. One `runs` row (component intake, source zillow_detail,
    scope targeted)."""
    from .apify import run_actor, run_record, settle
    from .base import event_cost

    zpids = active_zpids(conn)
    have = set() if refetch else set(load_pages())
    todo = [z for z in zpids if z not in have][:limit]
    plan = {"active_zpids": len(zpids), "have_page": len(set(zpids) & have), "to_fetch": len(todo),
            "actor_runs": -(-len(todo) // batch)}
    if dry_run or not todo:
        return plan
    sources = load_sources()
    actor = sources.get(SOURCE, {}).get("actor", ACTOR)
    run_id = start_run(conn, "intake", SOURCE, {"actor": actor, "scope": "targeted", "zpids": len(todo),
                                                 "batch": batch, "max_usd": max_usd})
    raw_dir = settings.RAW_DIR / SOURCE
    raw_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, Any] = {**plan, "fetched": 0, "failed": 0, "events": {}, "apify_runs": [], "home_status": {},
                              "apify_usd": 0.0}
    warnings: list[str] = []
    spent, est = 0.0, 0.0
    try:
        for k in range(0, len(todo), batch):
            chunk = todo[k:k + batch]
            room, why = spend_room(sources, max_usd - spent)
            if why:
                warnings.append(f"stopped before zpids {k + 1}-{len(todo)}: {why}")
                break
            run = run_actor(actor, {"zpids": chunk}, max_items=len(chunk), max_charge_usd=room, timeout_s=TIMEOUT_S)
            cost = event_cost(actor, run.charged_events) or (run.cost_usd or 0.0)
            est += cost
            spent += max(cost, run.usage_usd or 0.0)  # the cap counts the larger of real and estimate
            counts["apify_usd"] = round(counts["apify_usd"] + (run.usage_usd or 0.0), 4)
            path = raw_dir / f"run{run_id}_{k // batch + 1:02d}.json"
            path.write_text(json.dumps({"actor": actor, "zpids": chunk, "apify_run": run.run_id, "status": run.status,
                                        "cost_usd": cost, "items": run.items}))
            got = {zpid_of(i): i for i in run.items if isinstance(i, dict) and zpid_of(i)}
            counts["fetched"] += sum(1 for z in chunk if z in got)
            counts["failed"] += sum(1 for z in chunk if z not in got)
            counts["apify_runs"].append({**run_record(run), "zpids": len(chunk), "items": len(run.items),
                                         "cost_usd": cost})
            for ev, n in (run.charged_events or {}).items():
                counts["events"][ev] = counts["events"].get(ev, 0) + n
            for z, it in got.items():
                if it.get("home_status"):
                    counts["home_status"][z] = it["home_status"]
                if it.get("home_status") and it["home_status"] not in RENTAL:
                    counts.setdefault("last_event", {})[z] = last_event(it)
            if run.status != "SUCCEEDED":
                warnings.append(f"zpids {k + 1}-{k + len(chunk)}: actor run {run.status} ({run.console_url})")
            no_text = sum(1 for i in got.values() if not (i.get("description") or "").strip())
            if got and no_text > 0.5 * len(got):
                warnings.append(f"zpids {k + 1}-{k + len(chunk)}: {no_text} of {len(got)} pages have no description")
        stale = {z: f"{s} ({counts['last_event'].get(z)})" for z, s in counts["home_status"].items()
                 if s not in RENTAL}
        counts["not_for_rent"] = len(stale)
        counts["warnings"] = warnings
        counts["apify_usd"] = settle(counts["apify_runs"])
        status = "error" if counts["fetched"] == 0 else "ok"
        finish_run(conn, run_id, status, counts, est_cost=round(est, 4),
                   error=("; ".join(warnings) or "no page fetched") if status == "error" else None)
    except Exception as e:
        finish_run(conn, run_id, "error", counts, est_cost=round(est, 4), error=repr(e)[:500])
        raise
    return {"run_id": run_id, "status": status, "cost_usd": round(spent, 4),
            **{k: v for k, v in counts.items() if k != "home_status"}, "stale": stale}


def latest_status(conn: sqlite3.Connection) -> dict[str, tuple[str, str | None]]:
    """zpid -> (home_status, newest price-history event if not for rent) from the newest successful detail run
    that saw it."""
    out: dict[str, tuple[str, str | None]] = {}
    for r in conn.execute("SELECT counts FROM runs WHERE component = 'intake' AND source = ? AND status = 'ok' "
                          "ORDER BY id", (SOURCE,)):
        c = json.loads(r["counts"] or "{}")
        for z, s in (c.get("home_status") or {}).items():
            out[z] = (s, (c.get("last_event") or {}).get(z))
    return out
