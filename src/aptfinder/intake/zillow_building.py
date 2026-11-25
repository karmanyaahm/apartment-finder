"""Zillow building pages: every floor plan of an apartment building (beds, baths, sqft, price, units and their
availability), for the building's bedroom rows (`<zpid>:Nbd`), whose search card gives only bedrooms and a minimum
price. One page per building through `delicious_zebu/zillow-property-details-scraper` (input `{"Urls": [...]}`, a
`/b/<slug>/<lat>,<lon>_ll/` URL built from our row; the record has `page_type` 'building' and `floor_plans`).

  aptf-intake zillow-buildings --max-usd 0.5 [--batch 150] [--limit N] [--refetch] [--dry-run]

Like `zillow-detail`, an enrichment of listings already held: raw pages only (`data/raw/zillow_building/run<id>_<k>.json`),
no observations, one targeted `runs` row (source `zillow_building`, not in `ADAPTERS`, so never coverage and never
retiring anything). The text reading (`extract zillow`) applies a building's page to its bedroom rows
(`bed_facts`), so the fill comes back after every new search observation of the row.

A page counts only when it is our building's: its `zillow_id` is the row's zpid, or the zpid is one of its units
(the URL is built from coordinates, so a neighbour could answer); any other page is `mismatched` and never read.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import settings
from ..store import finish_run, start_run
from .base import load_sources, spend_room

SOURCE = "zillow_building"
ACTOR = "delicious_zebu/zillow-property-details-scraper"
BATCH = 150
TIMEOUT_S = 1800
PACIFIC = timezone(timedelta(hours=-8))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def building_url(address: str | None, lat: float, lon: float) -> str:
    """Zillow's building page by coordinates; the slug is cosmetic."""
    return f"https://www.zillow.com/b/{_slug(address or 'building')}/{lat:.6f},{lon:.6f}_ll/"


def unit_zpids(page: dict[str, Any]) -> set[str]:
    return {str(u.get("zpid")) for fp in page.get("floor_plans") or [] for u in [fp, *(fp.get("units") or [])]
            if u.get("zpid")}


def is_ours(page: dict[str, Any], zpid: str) -> bool:
    """The page is our building's: its zillow_id is our zpid, or our zpid is one of its plans' units."""
    return page.get("page_type") == "building" and (str(page.get("zillow_id")) == zpid or zpid in unit_zpids(page))


def raw_files() -> list[Path]:
    d = settings.RAW_DIR / SOURCE

    def key(p: Path) -> tuple[int, str]:
        m = re.match(r"run(\d+)_", p.name)
        return (int(m.group(1)) if m else 0, p.name)

    return sorted(d.glob("run*.json"), key=key) if d.is_dir() else []


def load_buildings() -> dict[str, dict[str, Any]]:
    """Our zpid -> its newest building page (only pages that are ours; `requested` maps URL -> zpid)."""
    out: dict[str, dict[str, Any]] = {}
    for p in raw_files():
        f = json.loads(p.read_text())
        want = f.get("requested") or {}
        fetched = f.get("fetched_at")
        for it in f.get("items") or []:
            z = want.get(it.get("original_url")) if isinstance(it, dict) else None
            if z and is_ours(it, z):
                out[z] = {**it, "_fetched_at": fetched}
    return out


def _date(ms: Any, fetched: str | None) -> str | None:
    """availableFrom: epoch ms of a local midnight (07:00 or 08:00 UTC, so the UTC date is the day), or '0' =
    available now (the page's fetch date)."""
    try:
        v = int(ms)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return (fetched or "")[:10] or None
    return datetime.fromtimestamp(v / 1000, timezone.utc).date().isoformat()


def plans(page: dict[str, Any]) -> list[dict[str, Any]]:
    """The page's floor plans, one dict each: name, beds, baths, sqft, min price, unit count, earliest available."""
    out = []
    for fp in page.get("floor_plans") or []:
        units = fp.get("units") or []
        prices = [p for p in [fp.get("minPrice"), *(u.get("price") for u in units)]
                  if isinstance(p, (int, float)) and p > 0]
        dates = [d for d in (_date(u.get("availableFrom"), page.get("_fetched_at")) for u in units or [fp]) if d]
        out.append({"name": fp.get("name") or (fp.get("videos") or [{}])[0].get("label"), "beds": fp.get("beds"),
                    "baths": fp.get("baths"), "sqft": fp.get("sqft"), "price": min(prices) if prices else None,
                    "units": len(units), "available": min(dates) if dates else None})
    return out


def bed_facts(page: dict[str, Any], beds: float | None) -> dict[str, Any]:
    """What a building's page says about its bedroom row (`<zpid>:Nbd`): `plans` (every plan with that bed count),
    and from them, only where every plan agrees, `baths` and `sqft`; `price` = the lowest plan price; `available_from`
    = the earliest unit. Plans that disagree on baths (Casa Alberta 2 bd: 2 ba and a 1.5 ba townhouse) leave baths
    unknown: the row is several plans, and one value would mix units."""
    if beds is None:
        return {}
    ps = [p for p in plans(page) if p["beds"] == beds]
    if not ps:
        return {}
    out: dict[str, Any] = {"plans": ps}
    for k in ("baths", "sqft"):
        vals = {p[k] for p in ps}
        if len(vals) == 1 and None not in vals and vals != {0}:
            out[k] = vals.pop()
    if prices := [p["price"] for p in ps if p["price"]]:
        out["price"] = min(prices)
    if dates := [p["available"] for p in ps if p["available"]]:
        out["available_from"] = min(dates)
    return out


def targets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One per building of the active Zillow bedroom rows: zpid, address, lat, lon."""
    rows = conn.execute(
        "SELECT CAST(json_extract(lo.data, '$.extra.zpid') AS TEXT) z, json_extract(lo.data, '$.address') a, "
        "json_extract(lo.data, '$.lat') lat, json_extract(lo.data, '$.lon') lon FROM latest_obs lo "
        "JOIN listings l ON l.source = lo.source AND l.source_id = lo.source_id "
        "WHERE lo.source = 'zillow' AND l.active = 1 AND lo.source_id LIKE '%:%' "
        "AND json_extract(lo.data, '$.lat') IS NOT NULL ORDER BY 1").fetchall()
    out: dict[str, dict[str, Any]] = {}
    for z, a, lat, lon in rows:
        if z and z not in out:
            out[z] = {"zpid": z, "address": a, "lat": lat, "lon": lon}
    return list(out.values())


def fetch(conn: sqlite3.Connection, *, max_usd: float, batch: int = BATCH, limit: int | None = None,
          refetch: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Fetch the building page of every building with active Zillow bedroom rows and no page yet (`refetch`: all),
    `batch` pages per actor run, under the budget guard before each run."""
    from .apify import run_actor, run_record, settle
    from .base import event_cost

    ts = targets(conn)
    have = set() if refetch else set(load_buildings())
    todo = [t for t in ts if t["zpid"] not in have][:limit]
    plan = {"buildings": len(ts), "have_page": len({t["zpid"] for t in ts} & have), "to_fetch": len(todo),
            "actor_runs": -(-len(todo) // batch)}
    if dry_run or not todo:
        return plan
    sources = load_sources()
    actor = sources.get(SOURCE, {}).get("actor", ACTOR)
    run_id = start_run(conn, "intake", SOURCE, {"actor": actor, "scope": "targeted", "buildings": len(todo),
                                                 "batch": batch, "max_usd": max_usd})
    raw_dir = settings.RAW_DIR / SOURCE
    raw_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, Any] = {**plan, "fetched": 0, "failed": 0, "mismatched": [], "events": {}, "apify_runs": [],
                              "apify_usd": 0.0}
    warnings: list[str] = []
    spent, est = 0.0, 0.0
    try:
        for k in range(0, len(todo), batch):
            chunk = todo[k:k + batch]
            room, why = spend_room(sources, max_usd - spent)
            if why:
                warnings.append(f"stopped before buildings {k + 1}-{len(todo)}: {why}")
                break
            want = {building_url(t["address"], t["lat"], t["lon"]): t["zpid"] for t in chunk}
            run = run_actor(actor, {"Urls": list(want)}, max_items=len(chunk), max_charge_usd=room,
                            timeout_s=TIMEOUT_S)
            cost = event_cost(actor, run.charged_events) or (run.cost_usd or 0.0)
            est += cost
            spent += max(cost, run.usage_usd or 0.0)
            path = raw_dir / f"run{run_id}_{k // batch + 1:02d}.json"
            path.write_text(json.dumps({"actor": actor, "requested": want, "apify_run": run.run_id,
                                        "status": run.status, "cost_usd": cost,
                                        "fetched_at": datetime.now(PACIFIC).date().isoformat(),
                                        "items": run.items}))
            got = {want[i["original_url"]]: i for i in run.items
                   if isinstance(i, dict) and i.get("original_url") in want}
            ours = {z for z, i in got.items() if is_ours(i, z)}
            counts["fetched"] += len(ours)
            counts["failed"] += sum(1 for z in want.values() if z not in got)
            counts["mismatched"] += [{"zpid": z, "page_zillow_id": i.get("zillow_id"), "page_type": i.get("page_type"),
                                      "name": i.get("building_name")} for z, i in got.items() if z not in ours]
            counts["apify_runs"].append({**run_record(run), "buildings": len(chunk), "items": len(run.items),
                                         "cost_usd": cost})
            for ev, n in (run.charged_events or {}).items():
                counts["events"][ev] = counts["events"].get(ev, 0) + n
            if run.status != "SUCCEEDED":
                warnings.append(f"buildings {k + 1}-{k + len(chunk)}: actor run {run.status} ({run.console_url})")
            no_plans = sum(1 for z in ours if not got[z].get("floor_plans"))
            if ours and no_plans > 0.5 * len(ours):
                warnings.append(f"buildings {k + 1}-{k + len(chunk)}: {no_plans} of {len(ours)} pages have no "
                                "floor plans")
        if counts["mismatched"]:
            warnings.append(f"{len(counts['mismatched'])} pages were another building's (never read)")
        counts["warnings"] = warnings
        counts["apify_usd"] = settle(counts["apify_runs"])
        status = "error" if counts["fetched"] == 0 else "ok"
        finish_run(conn, run_id, status, counts, est_cost=round(est, 4),
                   error=("; ".join(warnings) or "no page fetched") if status == "error" else None)
    except Exception as e:
        finish_run(conn, run_id, "error", counts, est_cost=round(est, 4), error=repr(e)[:500])
        raise
    return {"run_id": run_id, "status": status, "cost_usd": round(spent, 4), **counts}
