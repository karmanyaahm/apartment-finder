"""Prometheus own sites through Prometheus's own availability JSON (AGENTS.md rule 6 exception, 2026-11-25).

  aptf-intake prometheus [--site ID] [--dry-run]   # every site in the list whose platform is prometheus

One small request per site, made directly from the machine it runs on (no page scraping, no Apify):
`https://shopping.prometheusapartments-prod-west2.com/<id>/available-units?date=<today>`, a JSON list of the units
offered, each with its plan name, rent and best rent, sqft, beds, baths, unit number and ready date. `<id>` is the
community page's `__NEXT_DATA__` -> `props.pageProps.apartmentPageData.id`: read from the site's stored `direct` raw
file when there is one, else from the page fetched once; either way cached in `data/prometheus_ids.json`.

The rows take the render's place for these sites (`render` skips them): source `direct`, source_id
`<site>:r:<unit>`, area `<site>#render`, `extra.platform` 'render' with `extra.reader` 'prometheus_json', and the run
is logged like a render (`params.render`, an outcome per `<site>#render` area), so a site read here retires its
older render rows (plans no longer offered) exactly as a render does, and a refused request retires nothing.
Requests are sequential with a short delay and a normal browser UA; a refused request (HTTP 403/429, or a JSON
error such as Cloudflare's 'challenge') is recorded as `blocked` and that site is left for next time.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .. import identity, settings
from ..config import load_sites
from ..plans import match_drawing
from ..store import append_observation, finish_run, start_run
from ..store.models import ObservationData
from .direct import _script, _site_for
from .lease import _avail
from .render import _site_address, _slug, describe

API = "https://shopping.prometheusapartments-prod-west2.com/{id}/available-units?date={date}"
ORIGIN = "https://prometheusapartments.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
DELAY_S = 2.0  # between requests: sequential and polite
READER = "prometheus_json"
NEXT_DATA = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# get(url, headers) -> (HTTP status, body text); raises on a network failure
Get = Callable[[str, dict[str, str]], tuple[int, str]]


def _httpx_get(url: str, headers: dict[str, str]) -> tuple[int, str]:
    import httpx

    r = httpx.get(url, headers={"User-Agent": UA, **headers}, timeout=30, follow_redirects=True)
    return r.status_code, r.text


def prometheus_sites() -> dict[str, dict[str, Any]]:
    return {sid: s for sid, s in load_sites().items() if s.get("platform") == "prometheus"}


def _ids_file() -> Path:
    return settings.DATA_DIR / "prometheus_ids.json"


def _load_ids() -> dict[str, str]:
    try:
        return json.loads(_ids_file().read_text())
    except (OSError, ValueError):
        return {}


def _save_id(sid: str, cid: str) -> None:
    ids = _load_ids()
    ids[sid] = cid
    _ids_file().parent.mkdir(parents=True, exist_ok=True)
    _ids_file().write_text(json.dumps(ids, indent=1, sort_keys=True))


def _apd_id(next_data: str) -> str | None:
    try:
        cid = json.loads(next_data)["props"]["pageProps"]["apartmentPageData"]["id"]
    except (ValueError, KeyError, TypeError):
        return None
    return str(cid) if cid else None


def id_from_raw(conn, sid: str) -> str | None:
    """The community id from the page data of the site's stored `direct` fetch (newest raw file first)."""
    for r in conn.execute("SELECT raw_path FROM observations WHERE source = 'direct' AND source_id = ? "
                          "AND raw_path IS NOT NULL GROUP BY raw_path ORDER BY MAX(id) DESC", (f"{sid}:site",)):
        p = Path(r["raw_path"])
        for f in (p, settings.ROOT / p, settings.RAW_DIR / "direct" / p.name):
            if f.exists():
                break
        else:
            continue
        try:
            items = json.loads(f.read_text()).get("items") or []
        except (OSError, ValueError, AttributeError):
            continue
        for it in items:
            if (_site_for(it) or {}).get("id") == sid and (nd := _script(it, lambda s: s["id"] == "__NEXT_DATA__")):
                if cid := _apd_id(nd):
                    return cid
    return None


def community_id(conn, sid: str, site: dict[str, Any], get: Get) -> tuple[str | None, str, dict[str, Any]]:
    """(id, where it came from: cache / raw / page, failure info when the page had to be fetched and failed)."""
    if cid := _load_ids().get(sid):
        return cid, "cache", {}
    if cid := id_from_raw(conn, sid):
        _save_id(sid, cid)
        return cid, "raw", {}
    status, body = get(site["url"], {"Accept": "text/html,application/xhtml+xml"})
    if status in (403, 429):
        return None, "page", {"outcome": "blocked", "http_status": status, "error": f"community page HTTP {status}"}
    m = NEXT_DATA.search(body or "") if status == 200 else None
    cid = _apd_id(m.group(1)) if m else None
    if not cid:
        return None, "page", {"outcome": "error", "http_status": status,
                              "error": f"community page HTTP {status}: no apartmentPageData.id"}
    _save_id(sid, cid)
    return cid, "page", {}


def units_outcome(status: int, body: str) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """(units, outcome info). A list is the answer (possibly empty); 403/429 or a JSON error ('challenge') is
    `blocked`; anything else is `error`. Only a list retires anything."""
    try:
        j = json.loads(body)
    except ValueError:
        j = None
    if status == 200 and isinstance(j, list):
        return j, {"http_status": status}
    err = (j.get("error") or {}) if isinstance(j, dict) else {}
    code = err.get("code") if isinstance(err, dict) else None
    msg = (err.get("message") if isinstance(err, dict) else None) or (body or "")[:200]
    oc = "blocked" if status in (403, 429) or code == "challenge" else "error"
    return None, {"outcome": oc, "http_status": status, "error": f"HTTP {status}: {code + ': ' if code else ''}{msg}"}


def _num(v: Any) -> float | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n else None


def unit_rows(units: list[dict[str, Any]], sid: str, site: dict[str, Any], *, address: str | None,
              drawings: dict[str, str], community: str) -> list[tuple[str, ObservationData, float | None]]:
    """[(source_id, data, monthly price)] for one site's units. The price is `bestRent` (the lowest, for
    `bestTerm` months), else `rent`; both are kept in extra."""
    out = []
    for u in units:
        num = str(u.get("unitNumber") or "").strip()
        if not num:
            continue
        bldg = str(u.get("buildingNumber") or "").strip()
        unit = f"{bldg}-{num}" if bldg else num
        plan = (u.get("floorPlanName") or "").strip() or None
        price = _num(u.get("bestRent")) or _num(u.get("rent"))
        drawing = (drawings.get(plan) or match_drawing(plan, drawings)) if plan else None
        extra = {"site": sid, "platform": "render", "reader": READER, "area": f"{sid}#render",
                 "community_id": community, "plan_id": u.get("floorPlanID"), "unit_id": u.get("unitID"),
                 "unit_label": u.get("unitLabel"), "floor": u.get("floor"), "building": bldg or None,
                 "rent": _num(u.get("rent")), "best_rent": _num(u.get("bestRent")), "best_term": u.get("bestTerm"),
                 "deposit": _num(u.get("deposit")), "ready_date": u.get("madeReadyDate"),
                 "on_hold": str(u.get("unitHoldStatus")).lower() == "true",
                 **({"floorplan_image": drawing} if drawing else {})}
        data = ObservationData(address=address, kind="apartment", term="long", property_name=site.get("name"),
                               operator="Prometheus", floor_plan=plan, unit=unit, beds=_num(u.get("bedrooms")),
                               baths=_num(u.get("bathrooms")), sqft=_num(u.get("area")),
                               available_from=_avail(u.get("madeReadyDate")), extra=extra)
        out.append((f"{sid}:r:{_slug(unit)}", data, price))
    return out


def _drawings(conn, sid: str) -> dict[str, str]:
    """The site's own plan-name -> drawing map (`extra.plan_drawings` of its `<site>:site` row)."""
    r = conn.execute("SELECT json_extract(data, '$.extra.plan_drawings') AS d FROM listings WHERE source = 'direct' "
                     "AND source_id = ?", (f"{sid}:site",)).fetchone()
    return json.loads(r["d"]) if r and r["d"] else {}


def _summary(rows: list[tuple[str, ObservationData, float | None]]) -> dict[str, Any]:
    prices = [p for _, _, p in rows if p]
    return {"units": len(rows), "plans": len({d.floor_plan for _, d, _ in rows}),
            "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None}


def read(conn, *, sites: list[str] | None = None, dry_run: bool = False, get: Get | None = None,
         delay: float = DELAY_S, today: date | None = None) -> dict[str, Any]:
    get = get or _httpx_get
    all_p = prometheus_sites()
    unknown = sorted(set(sites or []) - set(all_p))
    if unknown:
        raise SystemExit(f"not Prometheus sites in the list: {', '.join(unknown)}")
    pick = {sid: s for sid, s in all_p.items() if not sites or sid in sites}
    if dry_run or not pick:
        ids = _load_ids()
        return {"sites": [(sid, s["url"], ids.get(sid) or id_from_raw(conn, sid) or "id from the page")
                          for sid, s in pick.items()], "requests": len(pick)}
    day = (today or date.today()).isoformat()
    run = start_run(conn, "intake", "direct", {"areas": [f"{sid}#render" for sid in pick], "render": True,
                                               "reader": READER, "scope": "partial", "endpoint": API,
                                               "date": day})
    raw_dir = settings.RAW_DIR / "direct"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"run{run}_prometheus_json.json"
    raw: dict[str, Any] = {"reader": READER, "endpoint": API, "date": day, "sites": {}}
    per_area: dict[str, Any] = {}
    report: dict[str, Any] = {}
    n_obs = n_req = 0
    answers: dict[str, Any] = {}  # community id -> (units, failure) of this run
    try:
        for sid, s in pick.items():
            if n_req:
                time.sleep(delay)
            rec: dict[str, Any] = {"url": s["url"]}
            raw["sites"][sid] = rec
            try:
                cid, src, fail = community_id(conn, sid, s, get)
                n_req += src == "page"
                rec.update(community_id=cid, id_from=src)
                units = None
                if cid in answers:  # another list entry for the same community: asked once per run
                    units, fail = answers[cid]
                    rec.update(same_as=cid)
                elif cid:
                    if src == "page":
                        time.sleep(delay)
                    url = API.format(id=cid, date=day)
                    status, body = get(url, {"Accept": "application/json", "Origin": ORIGIN,
                                             "Referer": s["url"]})
                    n_req += 1
                    units, fail = units_outcome(status, body)
                    answers[cid] = (units, fail)
                    rec.update(request=url, status=status,
                               **({"units": units} if units is not None else {"body": (body or "")[:2000]}))
            except Exception as e:  # network failure: this site only
                fail = {"outcome": "timeout" if "timeout" in type(e).__name__.lower() else "error",
                        "http_status": None, "error": f"{type(e).__name__}: {str(e)[:200]}"}
                units = None
                rec["exception"] = fail["error"]
            if units is None:
                per_area[f"{sid}#render"] = {"state": "failed", "items": 0, **fail}
                report[sid] = describe({**fail, "at": None}) + f": {fail.get('error')}"
                continue
            addr, _ = _site_address(conn, sid, s)
            rows = unit_rows(units, sid, s, address=addr, drawings=_drawings(conn, sid), community=cid)
            if units and not rows:  # units we cannot read: a changed format, never 'nothing offered' (rule 8)
                fail = {"outcome": "error", "http_status": 200,
                        "error": f"{len(units)} units, none with a unit number: format changed?"}
                per_area[f"{sid}#render"] = {"state": "failed", "items": 0, **fail}
                report[sid] = describe({**fail, "at": None}) + f": {fail['error']}"
                continue
            for source_id, data, price in rows:
                append_observation(conn, run, "direct", source_id, data, url=s["url"], price=price,
                                   price_basis="month" if price else None, raw_path=str(raw_path), commit=False)
            conn.commit()
            n_obs += len(rows)
            oc = "ok" if rows else "no_plans"
            summ = _summary(rows)
            per_area[f"{sid}#render"] = {"state": "complete" if rows else "empty", "items": len(rows),
                                         "outcome": oc, "http_status": 200, "error": None, **summ}
            report[sid] = summ
    except BaseException as e:
        raw_path.write_text(json.dumps(raw))
        conn.commit()
        finish_run(conn, run, "error", {"areas": per_area, "observations": n_obs, "requests": n_req},
                   est_cost=0.0, error=repr(e)[:500])
        raise
    raw_path.write_text(json.dumps(raw))
    finish_run(conn, run, "ok", {"areas": per_area, "observations": n_obs, "requests": n_req}, est_cost=0.0)
    if n_obs:
        identity.run(conn)
    return {"run_id": run, "requests": n_req, "observations": n_obs, "raw": str(raw_path), "sites": report}
