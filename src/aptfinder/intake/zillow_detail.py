"""Zillow detail pages: the description, "what's special" and the structured facts (fees, pets, laundry, parking,
community features, utilities) that Zillow's search cards lack, one page per zpid, through
`delicious_zebu/zillow-property-details-scraper` (input `{"zpids": [...]}`).

  aptf-intake zillow-detail [--max-usd 1.0] [--batch 150] [--limit N] [--refetch] [--dry-run]

An enrichment of listings we already hold, not a source of listings: a run stores raw pages only
(`data/raw/zillow_detail/run<id>_<k>.json`) and writes no observations, so it creates no listing or place, is never
coverage of an area and never retires anything (`scope = targeted`; `zillow_detail` is not in `ADAPTERS`). The text
reading (`extract zillow`) finds a listing's newest page by its `extra.zpid`; a building's bedroom rows (`<zpid>:Nbd`)
share one page, whose beds, baths and price belong to one representative unit and are never read.

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


# Structured facts sent to the text reading as plain lines. Building-level or stated policy only: no beds, baths,
# area, price, dates or deposit (on a building's page those are one representative unit's).
RESO_LINES = (("feesAndDues", "Fees"), ("allowedPets", "Pets allowed"), ("laundryFeatures", "Laundry"),
              ("appliances", "Appliances"), ("cooling", "Cooling"), ("parkingFeatures", "Parking"),
              ("communityFeatures", "Community features"), ("associationAmenities", "Amenities"),
              ("poolFeatures", "Pool"), ("utilities", "Utilities"), ("leaseTerm", "Lease term"))
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


def detail_lines(item: dict[str, Any]) -> list[tuple[str, Any]]:
    """(label, value) pairs of a detail page for the text reading (`extract.raw_lines('zillow', ...)`)."""
    rf = dict(item.get("reso_facts") or {})
    # the security deposit is the representative unit's on a building's page; fees by type are the building's
    rf["feesAndDues"] = [f for f in rf.get("feesAndDues") or [] if not (isinstance(f, dict)
                                                                         and str(f.get("type")).lower() == "deposit")]
    out: list[tuple[str, Any]] = [("Description", (item.get("description") or "").strip()),
                                  ("What's special", item.get("whats_special"))]
    out += [(label, "; ".join(v)) for key, label in RESO_LINES if (v := _vals(rf.get(key)))]
    if rf.get("furnished") is True:  # `false` is on almost every page, a default, not a statement
        out.append(("Furnished", "yes"))
    return out


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
    from .apify import run_actor
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
    counts: dict[str, Any] = {**plan, "fetched": 0, "failed": 0, "events": {}, "apify_runs": [], "home_status": {}}
    warnings: list[str] = []
    spent = 0.0
    try:
        for k in range(0, len(todo), batch):
            chunk = todo[k:k + batch]
            room, why = spend_room(sources, max_usd - spent)
            if why:
                warnings.append(f"stopped before zpids {k + 1}-{len(todo)}: {why}")
                break
            run = run_actor(actor, {"zpids": chunk}, max_items=len(chunk), max_charge_usd=room, timeout_s=TIMEOUT_S)
            cost = event_cost(actor, run.charged_events) or (run.cost_usd or 0.0)
            spent += cost
            path = raw_dir / f"run{run_id}_{k // batch + 1:02d}.json"
            path.write_text(json.dumps({"actor": actor, "zpids": chunk, "apify_run": run.run_id, "status": run.status,
                                        "cost_usd": cost, "items": run.items}))
            got = {zpid_of(i): i for i in run.items if isinstance(i, dict) and zpid_of(i)}
            counts["fetched"] += sum(1 for z in chunk if z in got)
            counts["failed"] += sum(1 for z in chunk if z not in got)
            counts["apify_runs"].append({"run": run.run_id, "status": run.status, "zpids": len(chunk),
                                         "items": len(run.items), "cost_usd": cost})
            for ev, n in (run.charged_events or {}).items():
                counts["events"][ev] = counts["events"].get(ev, 0) + n
            for z, it in got.items():
                if it.get("home_status"):
                    counts["home_status"][z] = it["home_status"]
            if run.status != "SUCCEEDED":
                warnings.append(f"zpids {k + 1}-{k + len(chunk)}: actor run {run.status} ({run.console_url})")
            no_text = sum(1 for i in got.values() if not (i.get("description") or "").strip())
            if got and no_text > 0.5 * len(got):
                warnings.append(f"zpids {k + 1}-{k + len(chunk)}: {no_text} of {len(got)} pages have no description")
        stale = {z: s for z, s in counts["home_status"].items() if s not in RENTAL}
        counts["not_for_rent"] = len(stale)
        counts["warnings"] = warnings
        status = "error" if counts["fetched"] == 0 else "ok"
        finish_run(conn, run_id, status, counts, est_cost=round(spent, 4),
                   error=("; ".join(warnings) or "no page fetched") if status == "error" else None)
    except Exception as e:
        finish_run(conn, run_id, "error", counts, est_cost=round(spent, 4), error=repr(e)[:500])
        raise
    return {"run_id": run_id, "status": status, "cost_usd": round(spent, 4),
            **{k: v for k, v in counts.items() if k != "home_status"}, "stale": stale}


def latest_status(conn: sqlite3.Connection) -> dict[str, tuple[str, int]]:
    """zpid -> (home_status, run id) from the newest successful detail run that saw it."""
    out: dict[str, tuple[str, int]] = {}
    for r in conn.execute("SELECT id, counts FROM runs WHERE component = 'intake' AND source = ? AND status = 'ok' "
                          "ORDER BY id", (SOURCE,)):
        for z, s in (json.loads(r["counts"] or "{}").get("home_status") or {}).items():
            out[z] = (s, r["id"])
    return out
