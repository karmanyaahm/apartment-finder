"""Dashboard: FastAPI on 127.0.0.1, JSON API plus one static page. Writes only via store.append_*."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import pipeline, settings, verify
from ..areas import in_walk_zone
from ..areas import zones as area_zones
from ..config import load_goals, load_point_sets, load_points
from ..filter import best_trips, evaluate_all, nearest_by_set
from ..plans import plan_key as _plan_key
from ..store import append_annotation, append_check, append_link, connect, init_db
from ..store.models import CHECKABLE_FIELDS

STATIC = Path(__file__).parent / "static"
AUTHOR = "user"
MODES = ["walk", "bike", "transit", "drive", "bike_train_bike", "bike_bus_bike"]
DEFAULT_ATTRIBUTES = ["overall", "status", "gym", "walk_to_caltrain", "living_room"]
LISTING_FIELDS = ["price", "price_basis", "price_month", "price_per_bed", "beds", "baths", "sqft", "kind",
                  "term", "furnished", "private_bath", "available_from", "available_to", "address", "unit",
                  "property_name", "operator", "title", "amenities", "floor_plan"]

SCRAPE_REFRESH_S = 120  # how stale the dashboard may be while an intake run is writing


def _group_listings(conn) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM listings WHERE place_key IS NOT NULL ORDER BY source, source_id"):
        out.setdefault(r["place_key"], []).append(dict(r))
    return out


def group_plans(listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Floor plans of a place: listings grouped by beds, baths and plan name across all sources. When the
    building's own site prices an offer, other sources' prices are only 'also listed' (own website wins)."""
    listings = [l for l in listings if l["fields"].get("kind") != "site_info"]  # photos and link only
    own_site = any(l["source"] == "direct" and l["active"] and l["fields"].get("price_month") for l in listings)
    listings = [l for l in listings if l["active"]] or listings  # inactive rows only if nothing is live
    groups: dict[str, list[dict[str, Any]]] = {}
    summaries = []  # beds-only rows (e.g. Zillow's per-building "3bd from $X", baths unknown)
    for l in listings:
        f = l["fields"]
        if f.get("baths") is None and f.get("beds") is not None and not f.get("unit"):
            summaries.append(l)
        else:
            groups.setdefault(_plan_key(f), []).append(l)
    for l in summaries:  # join the largest plan with the same bedroom count, else stand alone
        same = [k for k, ls in groups.items() if k.startswith("lease|")
                and ls[0]["fields"].get("beds") == l["fields"]["beds"]]
        k = max(same, key=lambda k: len(groups[k])) if same else _plan_key(l["fields"])
        groups.setdefault(k, []).append(l)
    out = []
    for key, ls in groups.items():
        # the community's own site is current when it prices the plan; other sources are "also on"
        direct = [l for l in ls if l["source"] == "direct" and l["fields"].get("price_month")]
        basis = direct or ls
        f = [l["fields"] for l in ls]
        fb = [l["fields"] for l in basis]
        prices = [x["price_month"] for x in fb if x.get("price_month")]
        sqft = [x["sqft"] for x in f if x.get("sqft")]
        avail = sorted(x["available_from"] for x in fb if x.get("available_from"))
        active = [l for l in basis if l["active"]]
        kind = key.split("|", 1)[0]  # lease | furnished | stay
        ops = sorted({x["operator"] for x in f if x.get("operator")})
        tag = f" ({', '.join(ops)})" if ops else ""
        out.append({
            "key": key, "name": next((l["fields"]["floor_plan"] for l in sorted(ls, key=lambda l: l["source"] != "direct")
                                      if l["fields"].get("floor_plan")
                                      and not re.fullmatch(r"(studio|\d+bd)", l["fields"]["floor_plan"])), None),
            "beds": f[0].get("beds"), "baths": next((x["baths"] for x in f if x.get("baths") is not None), None),
            "sqft": [min(sqft), max(sqft)] if sqft else None,
            "price_month": [min(prices), max(prices)] if prices else None,
            "price_per_bed": (min(prices) / f[0]["beds"]) if prices and f[0].get("beds") else None,
            "n_listings": len(ls), "n_active": len(active),
            "n_units": len({x.get("unit") for x in fb if x.get("unit")}),
            "offer": {"stay": "furnished stay" + tag, "furnished": "furnished lease" + tag}.get(kind, "lease"),
            "offer_kind": kind,
            "from_direct": bool(direct),
            "earliest": avail[0] if avail else None,
            "sources": sorted({l["source"] for l in ls}),
            "also_on": sorted({l["source"] for l in ls} - {"direct"}) if direct else [],
            # each other source's lowest price for this plan: "also listed at $X on <source>"
            "also_listed": [{"source": src, "price_month": min((x for x in pr if x), default=None)}
                            for src, pr in sorted(_by_source(ls).items())],
            # at a place whose own site prices offers, a plan only other sources list does not count for goals
            "not_on_own_site": own_site and not direct,
            # the community's own drawing first; hotlink-blocked hosts last
            "image": next((u for l in sorted(ls, key=lambda l: l["source"] != "direct")
                           for u in [(l["data"].get("extra") or {}).get("floorplan_image")]
                           if u and not any(h in u for h in HOTLINK_BLOCKED)), None),
            "listing_ids": [f"{l['source']}|{l['source_id']}" for l in ls],
            # only the community's own plan list, no price: the plan exists, no unit in it is known to be offered
            "plan_only": all(l["source"] == "direct" and not l["fields"].get("price") for l in ls),
        })
    return sorted(out, key=lambda p: (p["offer"] != "lease", p["offer"], p["beds"] is None, p["beds"] or 0, p["baths"] or 0,
                                      (p["price_month"] or [1e9])[0]))


def _by_source(ls: list[dict[str, Any]]) -> dict[str, list[float | None]]:
    out: dict[str, list[float | None]] = {}
    for l in ls:
        if l["source"] != "direct":
            out.setdefault(l["source"], []).append(l["fields"].get("price_month"))
    return out


# Image hosts that refuse hotlinked requests (Chrome: ERR_BLOCKED_BY_ORB on every images1.apartments.com URL,
# 2026-11-24). Their photos are listed last; showing them needs mirroring through Apify (not done).
HOTLINK_BLOCKED = ("images1.apartments.com",)


def gather_photos(listings: list[dict[str, Any]], limit: int = 30) -> list[dict[str, str]]:
    """Photo URLs from every listing (not mirrored: the browser loads them from the listing site's CDN).
    The community's own website comes first; hosts known to block hotlinking go last, so loadable photos fill
    the limit first."""
    seen, out = set(), []
    listings = sorted(listings, key=lambda l: (any(
        h in u for u in (l["data"].get("extra") or {}).get("images") or [] for h in HOTLINK_BLOCKED),
        l["source"] != "direct"))
    for l in listings:
        for u in (l["data"].get("extra") or {}).get("images") or []:
            if u and u not in seen:
                seen.add(u)
                # `page`: the listing or site page the photo was found on; `site`: that page's host, for the overlay
                out.append({"url": u, "source": l["source"], "page": l.get("url"), "site": site_name(l.get("url"))})
    return out[:limit]


def place_label(p: dict[str, Any]) -> str | None:
    """A building's name or address; a third-party stay place says whose stays and where."""
    if "~" in (p.get("place_key") or ""):
        who = p.get("operator") or p["place_key"].split("~", 1)[1]
        return f"{who} furnished stays · {p.get('address') or ''}".strip(" ·")
    return p.get("property_name") or p.get("address")


def site_name(url: str | None) -> str | None:
    if not url:
        return None
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.lower().removeprefix("www.") or None


def websites(listings: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The building's own website(s): community-site rows first, then a website an aggregator gave."""
    seen, out = set(), []
    for l in sorted(listings, key=lambda l: l["source"] != "direct"):
        url = l.get("url") if l["source"] == "direct" else (l["data"].get("extra") or {}).get("website")
        if url and site_name(url) not in seen:
            seen.add(site_name(url))
            out.append({"url": url, "site": site_name(url)})
    return out


class AnnotationIn(BaseModel):
    entity_type: str = "place"
    entity_key: str
    attribute: str
    goal_id: str | None = None
    rating: int | None = None
    comment: str | None = None


class CheckIn(BaseModel):
    queue: str = "adhoc"
    observation_id: int | None = None
    trip_id: int | None = None
    field: str
    verdict: str
    true_value: Any = None
    note: str | None = None


class LinkIn(BaseModel):
    source: str
    source_id: str
    place_key: str | None = None  # None = split this listing into its own place


def _parse_value(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    try:
        return float(s.replace(",", "").replace("$", ""))
    except ValueError:
        return s


def create_app(db_path: str | Path | None = None, redact: dict[str, str] | None = None) -> FastAPI:
    """`redact` (real -> shown) makes a read-only view for screenshots: every API response has the strings
    swapped, longest first, and every write is refused."""
    db_path = str(db_path or settings.DB_PATH)
    conn = connect(db_path)
    init_db(conn)
    lock = threading.RLock()  # re-entrant: cached() takes it inside endpoints that already hold it
    app = FastAPI(title="apartment finder", openapi_url=None, docs_url=None, redoc_url=None)

    if redact is not None:
        pairs = sorted(redact.items(), key=lambda kv: -len(kv[0]))

        @app.middleware("http")
        async def redacted(request: Request, call_next):  # type: ignore[no-untyped-def]
            if request.method != "GET":
                return JSONResponse({"detail": "read-only (redacted view)"}, status_code=403)
            resp = await call_next(request)
            if not request.url.path.startswith("/api/"):
                return resp
            body = b"".join([chunk async for chunk in resp.body_iterator]).decode()
            for real, shown in pairs:
                body = body.replace(real, shown)
            headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
            return Response(body, status_code=resp.status_code, headers=headers, media_type=resp.media_type)

    memo: dict[str, tuple[Any, ...]] = {}

    def data_version() -> tuple[tuple, tuple]:
        """(edits, scrapes): what the pages show changes with a new row in any log or an edited config file.
        Edits (links, checks, annotations, config) are the user's own and must show at once; scrapes (observations,
        trips, runs) arrive in bursts while intake runs. Cheap (max rowid per table, file mtimes)."""
        row = conn.execute(
            "SELECT (SELECT MAX(id) FROM links), (SELECT MAX(id) FROM checks), (SELECT MAX(id) FROM annotations), "
            "(SELECT MAX(id) FROM observations), (SELECT MAX(id) FROM trips), (SELECT MAX(id) FROM runs), "
            "(SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1)").fetchone()
        cfg = tuple(f.stat().st_mtime_ns for f in sorted(settings.CONFIG_DIR.rglob("*")) if f.is_file())
        return tuple(row[:3]) + cfg, tuple(row[3:])

    def cached(name: str, fn):
        """fn() under the lock, reused while nothing changed. A user edit recomputes at once; new scraped rows
        recompute at most every SCRAPE_REFRESH_S seconds, so a running intake (a write every few seconds) does not
        make every request rebuild every whole-database result."""
        with lock:
            edits, scrapes = data_version()
            hit = memo.get(name)
            if hit and hit[0] == edits and (hit[1] == scrapes or time.monotonic() - hit[2] < SCRAPE_REFRESH_S):
                return hit[3]
            val = fn()
            memo[name] = (edits, scrapes, time.monotonic(), val)
            return val

    def all_places() -> dict[str, dict[str, Any]]:
        """The places view by key, read once per data version (the view is computed over everything)."""
        return cached("places_by_key", lambda: {r["place_key"]: dict(r) for r in conn.execute("SELECT * FROM places")})

    def q(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with lock:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def latest_annotations(entity_type: str, key: str | None = None) -> dict[str, dict[str, dict[str, Any]]]:
        sql = "SELECT * FROM annotations WHERE entity_type = ?" + (" AND entity_key = ?" if key else "")
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for r in q(sql + " ORDER BY id", (entity_type, key) if key else (entity_type,)):
            out.setdefault(r["entity_key"], {})[r["attribute"]] = r
        return out

    # ------------------------------------------------------------------ read API
    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        goals = load_goals()
        points = load_points()
        return {
            "db": Path(db_path).name,
            "goals": [{"id": g, "label": v.label} for g, v in goals.goals.items()],
            "targets": [p.id for p in points.get("commute_targets", [])],
            "default_target": next((r.get("target") for g in goals.goals.values() for r in g.rules
                                    if r.get("type") == "commute_any"), None),
            "modes": MODES,
            "point_sets": {k: {"label": v.label or k} for k, v in load_point_sets().items()},
            "attributes": DEFAULT_ATTRIBUTES,
            "google": bool(settings.secret("GOOGLE_MAPS_API_KEY")),
            "areas": area_zones(),
        }

    @app.get("/api/points")
    def points() -> dict[str, list[dict[str, Any]]]:
        return {k: [{"name": p.name, "lat": p.lat, "lon": p.lon, **p.extra} for p in v]
                for k, v in load_points().items()}

    @app.get("/api/places")
    def places() -> list[dict[str, Any]]:
        res = cached("evaluate_all", lambda: evaluate_all(conn))
        trips = cached("best_trips", lambda: best_trips(conn))
        listings = q("SELECT * FROM listings WHERE place_key IS NOT NULL")
        by_place: dict[str, list[dict[str, Any]]] = {}
        for row in listings:
            by_place.setdefault(row["place_key"], []).append(row)
        anns = latest_annotations("place")
        out = []
        for p in q("SELECT * FROM places"):
            pk = p["place_key"]
            rows = by_place.get(pk, [])
            goals = {}
            for gid, pres in res["places"].items():
                r = pres.get(pk)
                if r:
                    goals[gid] = {"status": r["status"], "nice_score": r["nice_score"],
                                  "reasons": [t for s, t, *_ in r["reasons"] if s != "pass"],
                                  "unit_key": r["unit_key"], "source": r["source"],
                                  "source_id": r["source_id"]}
            title = next((x["title"] for x in rows if x.get("title")), None)
            cover = gather_photos([{**x, "data": json.loads(x["data"])} for x in rows], limit=1)
            a = anns.get(pk, {})
            out.append({
                **p,
                "in_zone": in_walk_zone(p["lat"], p["lon"]),
                "label": place_label(p) or title or pk,
                "title": title,
                "photo": cover[0]["url"] if cover else None,  # map hover
                "beds": sorted({x["beds"] for x in rows if x["beds"] is not None}),
                "baths": sorted({x["baths"] for x in rows if x["baths"] is not None}),
                "goals": goals,
                "minutes": {f"{pt}|{m}": {"minutes": t["minutes"], "provider": t["provider"]}
                            for (pt, m), t in trips.get(pk, {}).items()},
                "rating": (a.get("overall") or {}).get("rating"),
                "user_status": (a.get("status") or {}).get("comment"),
            })
        return out

    @app.get("/api/raw")
    def raw_item(source: str, source_id: str) -> dict[str, Any]:
        """The item the source sent for one listing, read from its raw file only when asked (the drawer's
        "raw" button), instead of shipping raw files with every drawer."""
        rows = q("SELECT raw_path FROM latest_obs WHERE source = ? AND source_id = ?", (source, source_id))
        if not rows or not rows[0]["raw_path"] or not Path(rows[0]["raw_path"]).exists():
            raise HTTPException(404, "no raw data for this listing")
        blob = json.loads(Path(rows[0]["raw_path"]).read_text())
        items = blob.get("items", blob) if isinstance(blob, dict) else blob
        root = source_id.split(":", 1)[0]
        item = next((i for i in items if f'"{root}"' in json.dumps(i)), None) or \
            next((i for i in items if root in json.dumps(i)), None)
        if item is None:
            raise HTTPException(404, "listing not found in its raw file")
        return {"raw_path": rows[0]["raw_path"], "item": item}

    @app.get("/api/places/{place_key}")
    def place_detail(place_key: str) -> dict[str, Any]:
        # the places and listings views are computed over everything; read them once per data version
        places_by_key = all_places()
        if place_key not in places_by_key:
            raise HTTPException(404, "no such place")
        place = dict(places_by_key[place_key])
        place["label"] = place_label(place)
        by_place = cached("listings_by_place", lambda: _group_listings(conn))
        listings = by_place.get(place_key, [])
        res = cached("evaluate_all", lambda: evaluate_all(conn))
        trips = cached("best_trips", lambda: best_trips(conn)).get(place_key, {})
        out_listings = []
        for l in listings:
            checks = {c["field"]: c["verdict"] for c in q(
                "SELECT field, verdict FROM current_checks WHERE source = ? AND source_id = ?",
                (l["source"], l["source_id"]))}
            history = q("SELECT observed_at, price, price_basis, query_checkin, query_checkout, "
                        "json_extract(data, '$.available_from') AS available_from, content_hash "
                        "FROM observations WHERE source = ? AND source_id = ? ORDER BY observed_at, id",
                        (l["source"], l["source_id"]))
            goal_results = {gid: res["listings"][gid].get((l["source"], l["source_id"])) for gid in res["listings"]}
            out_listings.append({
                "source": l["source"], "source_id": l["source_id"], "url": l["url"], "obs_id": l["obs_id"],
                "unit_key": l["unit_key"], "first_seen": l["first_seen"], "last_seen": l["last_seen"],
                "active": l["active"], "n_obs": l["n_obs"],
                "fields": {f: l.get(f) for f in LISTING_FIELDS},
                "overridden": (l["overridden"] or "").split(",") if l["overridden"] else [],
                "checks": checks,
                "media": json.loads(l["media"]) if l.get("media") else [],
                "history": history, "data": json.loads(l["data"]), "has_raw": bool(l.get("raw_path")),
                "goals": goal_results,
            })
        nearest = {}
        for (set_name, mode), t in nearest_by_set(trips).items():
            if set_name == "commute_targets":
                continue
            ranked = sorted([dict(v, point=pt.split(":", 1)[1]) for (pt, m), v in trips.items()
                             if m == mode and pt.startswith(set_name + ":") and v["minutes"] is not None],
                            key=lambda x: x["minutes"])
            nearest[f"{set_name}|{mode}"] = ranked[:3]
        matrix = {f"{pt}|{m}": {k: v[k] for k in ("id", "minutes", "provider", "legs", "distance_m")}
                  for (pt, m), v in trips.items() if pt.startswith("commute_targets:")}
        for v in matrix.values():
            v["legs"] = json.loads(v["legs"]) if isinstance(v["legs"], str) else v["legs"]
        anns = q("SELECT * FROM annotations WHERE entity_type = 'place' AND entity_key = ? ORDER BY id",
                 (place_key,))
        web = cached("website_status", lambda: pipeline.website_status(conn))
        with lock:
            pstatus = pipeline.place_status(conn, place_key, web=web)
        return {
            "pipeline": pstatus,
            "place": place,
            "goals": {gid: res["places"][gid].get(place_key) for gid in res["places"]},
            "plans": group_plans(out_listings),
            "photos": gather_photos(out_listings),
            "websites": websites(out_listings),
            "listings": out_listings,
            "matrix": matrix,
            "nearest": nearest,
            "annotations": anns,
            # the building and its third-party stay places (`<building>~<operator>`), linked both ways
            "related": [dict(r, label=place_label(r)) for r in q(
                "SELECT place_key, property_name, operator, address, n_active, min_lease_month, min_stay_month "
                "FROM places WHERE (place_key = ? OR place_key LIKE ?) AND place_key <> ?",
                (place_key.split("~", 1)[0], place_key.split("~", 1)[0] + "~%", place_key))],
        }

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        runs = q("""SELECT r.* FROM runs r JOIN (SELECT component, source, MAX(id) AS id FROM runs
                    GROUP BY component, source) m ON m.id = r.id ORDER BY r.component, r.source""")
        for r in runs:
            r["counts"] = json.loads(r["counts"]) if r["counts"] else {}
        spend = q("SELECT COALESCE(SUM(est_cost), 0) AS total FROM runs")[0]["total"]
        errors = q("SELECT id, started_at, component, source, error FROM runs WHERE status = 'error' "
                   "ORDER BY id DESC LIMIT 5")
        with lock:
            acc = verify.accuracy(conn)
            agree = cached("agreement", lambda: verify.agreement(conn))
            conflicts = cached("all_conflicts", lambda: verify.all_conflicts(conn))
            n_pending = sum(c["pending_render"] for c in conflicts)
            n_sample = cached("n_sample", lambda: len(verify.sample_queue(conn)))
            n_own = len(cached("own_site_queue", lambda: verify.own_site_queue(conn)))
        from ..trips import google_usage

        with lock:
            google = google_usage(conn)
        return {
            "runs": runs, "spend": spend, "errors": errors, "google": google,
            "accuracy": [{"source": s, "field": f, **a} for (s, f), a in sorted(acc.items())],
            "agreement": agree, "queues": {"conflict": len(conflicts) - n_pending, "conflict_pending": n_pending,
                                           "sample": n_sample,
                                           "own_site": n_own},
            "counts": cached("counts", lambda: q("SELECT (SELECT COUNT(*) FROM places) AS places, "
                                                 "(SELECT COUNT(*) FROM listings) AS listings, "
                                                 "(SELECT COUNT(*) FROM listings WHERE active = 1) AS active")[0]),
        }

    @app.get("/api/pipeline")
    def pipeline_overview() -> dict[str, Any]:
        """Each pipeline stage's state from the logs: done / partial / todo / stale / blocked, and the commands
        that would run the rest (aptfinder.pipeline)."""
        return cached("pipeline", lambda: pipeline.overview(conn))

    @app.get("/api/areas")
    def areas_map() -> dict[str, Any]:
        """Every intake area with the sources that search its kind and its shape (aptfinder.area_map)."""
        from ..area_map import area_map

        return cached("area_map", lambda: area_map(conn))

    # ------------------------------------------------------------------ verify
    def _obs_fields(obs_id: int) -> dict[str, Any]:
        r = q("SELECT * FROM observations WHERE id = ?", (obs_id,))[0]
        data = json.loads(r["data"])
        fields = list(dict.fromkeys([*load_goals().verify.sample_fields, *CHECKABLE_FIELDS]))
        out = {f: (r["price"] if f == "price" else data.get(f)) for f in fields}
        return {"obs_id": obs_id, "source": r["source"], "source_id": r["source_id"], "url": r["url"],
                "observed_at": r["observed_at"], "fields": out, "lat": data.get("lat"), "lon": data.get("lon")}

    def _listing_detail(item: dict[str, Any]) -> dict[str, Any]:
        """A conflict's or sample listing's observations, the fields to check and the place."""
        ids = [x["obs_id"] for x in item["listings"]] if item["kind"] == "conflict" else [item["obs_id"]]
        out: dict[str, Any] = {"observations": [_obs_fields(i) for i in ids]}
        if item["kind"] == "conflict":
            # the queue's price_month conflicts are checked on the stored price
            out["fields"] = sorted({verify.CHECK_FIELD.get(f, f) for f in item["fields"]})
        else:
            out["fields"] = load_goals().verify.sample_fields
        p = all_places().get(item["place_key"])
        out["place"] = {"address": p["address"], "property_name": p["property_name"]} if p else None
        out["label"] = place_label(p) if p else None
        return out

    @app.get("/api/verify/batch")
    def verify_batch(kind: str = "conflict", skip: int = 0, n: int = 10) -> dict[str, Any]:
        """Up to n conflicts from `skip` (actionable first, those waiting for an own-site render last), each with
        its observations, disagreeing fields and place label, plus the totals."""
        if kind != "conflict":
            raise HTTPException(400, "batch is for conflicts; use /api/verify/next for the rest")
        with lock:
            items = cached("all_conflicts", lambda: verify.all_conflicts(conn))
            checked = verify.current_checks(conn)
        n = max(1, min(n, 50))
        pending = sum(c["pending_render"] for c in items)
        page = [{"item": it, **_listing_detail(it)} for it in items[max(0, skip):max(0, skip) + n]]
        for e in page:  # verdicts already given on a partly checked conflict, so the screen shows them
            for o in e["observations"]:
                o["checked"] = checked.get((o["source"], o["source_id"]), {})
        return {"kind": kind, "skip": skip, "n": n, "total": len(items), "actionable": len(items) - pending,
                "pending": pending, "items": page}

    @app.get("/api/verify/own_sites")
    def verify_own_sites(skip: int = 0, n: int = 20) -> dict[str, Any]:
        """Named buildings whose own site gave no usable offer, n at a time, each with its reason and every URL
        tried (verify.own_site_queue), plus the count per reason."""
        with lock:
            items = cached("own_site_queue", lambda: verify.own_site_queue(conn))
        from collections import Counter

        return {"total": len(items), "skip": skip, "n": n, "items": items[skip:skip + n],
                "by_reason": dict(Counter(i["reason"] for i in items)), "labels": verify.OWN_SITE_REASONS}

    @app.get("/api/verify/next")
    def verify_next(seed: int = 0, skip: int = 0, conflicts: bool = True) -> dict[str, Any]:
        """One item at a time: conflicts (unless conflicts=false: the dashboard pages them with /batch), then the
        sample. Buildings whose own site gave nothing usable are a list of their own (/api/verify/own_sites)."""
        with lock:
            items = ((cached("all_conflicts", lambda: verify.all_conflicts(conn)) if conflicts else [])
                     + verify.sample_queue(conn, seed))
        if skip >= len(items):
            return {"done": True, "remaining": 0}
        item = items[skip]
        out: dict[str, Any] = {"done": False, "remaining": len(items), "item": item}
        if item["kind"] in ("conflict", "listing"):
            out.update(_listing_detail(item))
        else:
            t = q("SELECT * FROM trips WHERE id = ?", (item["trip_id"],))[0]
            p = q("SELECT lat, lon, address FROM places WHERE place_key = ?", (t["place_key"],))
            set_name, name = t["point_id"].split(":", 1)
            pt = next((x for x in load_points().get(set_name, []) if x.name == name), None)
            out["trip"] = {**t, "legs": json.loads(t["legs"]),
                           "origin": p[0] if p else None,
                           "dest": {"name": name, "lat": pt.lat, "lon": pt.lon} if pt else None}
        return out

    @app.post("/api/checks")
    def post_check(c: CheckIn) -> dict[str, Any]:
        if c.observation_id is None and c.trip_id is None:
            raise HTTPException(400, "observation_id or trip_id required")
        if c.observation_id is not None:
            if c.field not in CHECKABLE_FIELDS:
                raise HTTPException(400, f"field {c.field!r} is not checkable")
            rows = q("SELECT * FROM observations WHERE id = ?", (c.observation_id,))
            if not rows:
                raise HTTPException(404, "no such observation")
            stored = rows[0]["price"] if c.field == "price" else json.loads(rows[0]["data"]).get(c.field)
        else:
            rows = q("SELECT * FROM trips WHERE id = ?", (c.trip_id,))
            if not rows:
                raise HTTPException(404, "no such trip")
            stored = rows[0]["minutes"]
        tv = _parse_value(c.true_value) if c.verdict == "wrong" else None
        if c.verdict == "wrong" and tv in (None, ""):
            raise HTTPException(400, "a 'wrong' verdict needs the true value")
        with lock:
            cid = append_check(conn, AUTHOR, c.queue, c.field, c.verdict, observation_id=c.observation_id,
                               trip_id=c.trip_id, stored_value=stored, true_value=tv, note=c.note)
        return {"id": cid}

    # ------------------------------------------------------------------ other writes
    @app.post("/api/annotations")
    def post_annotation(a: AnnotationIn) -> dict[str, Any]:
        if a.rating is not None and not 1 <= a.rating <= 5:
            raise HTTPException(400, "rating must be 1 to 5")
        if a.rating is None and not (a.comment or "").strip():
            raise HTTPException(400, "rating or comment required")
        with lock:
            aid = append_annotation(conn, AUTHOR, a.entity_type, a.entity_key, a.attribute.strip().lower(),
                                    goal_id=a.goal_id, rating=a.rating, comment=a.comment)
        return {"id": aid}

    @app.post("/api/links")
    def post_link(link: LinkIn) -> dict[str, Any]:
        if not q("SELECT 1 FROM listings WHERE source = ? AND source_id = ?", (link.source, link.source_id)):
            raise HTTPException(404, "no such listing")
        if link.place_key:
            if not q("SELECT 1 FROM places WHERE place_key = ?", (link.place_key,)):
                raise HTTPException(404, "no such place")
            pk, reason = link.place_key, "manual merge"
        else:
            pk = "u_" + hashlib.sha256(f"{link.source}:{link.source_id}".encode()).hexdigest()[:16]
            reason = "manual split"
        with lock:
            lid = append_link(conn, AUTHOR, link.source, link.source_id, pk, pk, reason)
        return {"id": lid, "place_key": pk}

    @app.post("/api/trips/{place_key}")
    def route_place(place_key: str, provider: str | None = None) -> dict[str, Any]:
        from ..trips import SpendCapReached, build_commute, compute_for_place, make_source

        rows = q("SELECT place_key, lat, lon FROM places WHERE place_key = ?", (place_key,))
        if not rows:
            raise HTTPException(404, "no such place")
        provider = provider or ("google" if settings.secret("GOOGLE_MAPS_API_KEY") else "estimate")
        source = make_source(provider, max_calls=60)
        commute = build_commute(db_path, source)
        try:
            with lock:
                n = compute_for_place(conn, commute, rows[0], on_demand=True)
        except SpendCapReached as e:
            raise HTTPException(429, str(e))
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        return {"new_trips": n, "provider": provider, "api_calls": getattr(source, "calls", 0)}

    # ------------------------------------------------------------------ static
    settings.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=settings.MEDIA_DIR), name="media")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index() -> Response:
        # Asset URLs carry a hash of the file, so a cache in front (Cloudflare keeps .js and .css for hours)
        # never serves an old version after a deploy.
        html = (STATIC / "index.html").read_text()
        for name in ("app.js", "style.css"):
            v = hashlib.sha1((STATIC / name).read_bytes()).hexdigest()[:10]
            html = html.replace(f"static/{name}\"", f"static/{name}?v={v}\"")
        return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js")
    def sw() -> FileResponse:
        # served from the root so the service worker's scope covers the whole app
        return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    return app
