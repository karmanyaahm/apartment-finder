"""Dashboard: FastAPI on 127.0.0.1, JSON API plus one static page. Writes only via store.append_*."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import settings, verify
from ..areas import chunk_of, is_active
from ..areas import zones as area_zones
from ..config import load_goals, load_point_sets, load_points
from ..filter import best_trips, evaluate_all, nearest_by_set
from ..store import append_annotation, append_check, append_link, connect, init_db
from ..store.models import CHECKABLE_FIELDS

STATIC = Path(__file__).parent / "static"
AUTHOR = "user"
MODES = ["walk", "bike", "transit", "drive", "bike_train_bike", "bike_bus_bike"]
DEFAULT_ATTRIBUTES = ["overall", "status", "gym", "walk_to_caltrain", "living_room"]
LISTING_FIELDS = ["price", "price_basis", "price_month", "price_per_bed", "beds", "baths", "sqft", "kind",
                  "term", "furnished", "private_bath", "available_from", "available_to", "address", "unit",
                  "property_name", "operator", "title", "amenities", "floor_plan"]


def _plan_key(f: dict[str, Any]) -> str:
    """Same plan across sources = same beds, baths and size (to 10 sqft); the name only when size is unknown
    (sources name plans differently: Avalon 'C1', Apartments.com none)."""
    beds, baths = f.get("beds"), f.get("baths")
    size = f"{'studio' if beds == 0 else (f'{beds:g}bd' if beds is not None else '?bd')}/" \
           f"{f'{baths:g}' if baths is not None else '?'}ba"
    if f.get("sqft"):
        return f"{size}|{round(f['sqft'] / 10) * 10:.0f}sqft"
    return f"{size}|{(f.get('floor_plan') or '').strip().lower()}"


def group_plans(listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Floor plans of a place: listings grouped by beds, baths and plan name across all sources."""
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
        same = [k for k, ls in groups.items() if ls[0]["fields"].get("beds") == l["fields"]["beds"]]
        k = max(same, key=lambda k: len(groups[k])) if same else _plan_key(l["fields"])
        groups.setdefault(k, []).append(l)
    out = []
    for key, ls in groups.items():
        f = [l["fields"] for l in ls]
        prices = [x["price_month"] for x in f if x.get("price_month")]
        sqft = [x["sqft"] for x in f if x.get("sqft")]
        avail = sorted(x["available_from"] for x in f if x.get("available_from"))
        active = [l for l in ls if l["active"]]
        out.append({
            "key": key, "name": next((l["fields"]["floor_plan"] for l in sorted(ls, key=lambda l: l["source"] != "direct")
                                      if l["fields"].get("floor_plan")
                                      and not re.fullmatch(r"(studio|\d+bd)", l["fields"]["floor_plan"])), None),
            "beds": f[0].get("beds"), "baths": next((x["baths"] for x in f if x.get("baths") is not None), None),
            "sqft": [min(sqft), max(sqft)] if sqft else None,
            "price_month": [min(prices), max(prices)] if prices else None,
            "price_per_bed": (min(prices) / f[0]["beds"]) if prices and f[0].get("beds") else None,
            "n_listings": len(ls), "n_active": len(active),
            "n_units": len({x.get("unit") for x in f if x.get("unit")}),
            "earliest": avail[0] if avail else None,
            "sources": sorted({l["source"] for l in ls}),
            "image": next((l["data"].get("extra", {}).get("floorplan_image") for l in ls
                           if l["data"].get("extra", {}).get("floorplan_image")), None),
            "listing_ids": [f"{l['source']}|{l['source_id']}" for l in ls],
        })
    return sorted(out, key=lambda p: (p["beds"] is None, p["beds"] or 0, p["baths"] or 0,
                                      (p["price_month"] or [1e9])[0]))


# Image hosts that refuse hotlinked requests (Chrome: ERR_BLOCKED_BY_ORB on every images1.apartments.com URL,
# 2026-11-24). Their photos are listed last; showing them needs mirroring through Apify (not done).
HOTLINK_BLOCKED = ("images1.apartments.com",)


def gather_photos(listings: list[dict[str, Any]], limit: int = 30) -> list[dict[str, str]]:
    """Photo URLs from every listing (not mirrored: the browser loads them from the listing site's CDN).
    Hosts known to block hotlinking go last, so loadable photos fill the limit first."""
    seen, out = set(), []
    listings = sorted(listings, key=lambda l: any(
        h in u for u in (l["data"].get("extra") or {}).get("images") or [] for h in HOTLINK_BLOCKED))
    for l in listings:
        for u in (l["data"].get("extra") or {}).get("images") or []:
            if u and u not in seen:
                seen.add(u)
                out.append({"url": u, "source": l["source"]})
    return out[:limit]


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


def create_app(db_path: str | Path | None = None) -> FastAPI:
    db_path = str(db_path or settings.DB_PATH)
    conn = connect(db_path)
    init_db(conn)
    lock = threading.Lock()
    app = FastAPI(title="apartment finder")

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
        with lock:
            res = evaluate_all(conn)
            trips = best_trips(conn)
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
            a = anns.get(pk, {})
            chunk = chunk_of(p["lat"], p["lon"])
            out.append({
                **p,
                "chunk": chunk, "todo": not is_active(chunk),
                "label": p["property_name"] or p["address"] or title or pk,
                "title": title,
                "beds": sorted({x["beds"] for x in rows if x["beds"] is not None}),
                "baths": sorted({x["baths"] for x in rows if x["baths"] is not None}),
                "goals": goals,
                "minutes": {f"{pt}|{m}": {"minutes": t["minutes"], "provider": t["provider"]}
                            for (pt, m), t in trips.get(pk, {}).items()},
                "rating": (a.get("overall") or {}).get("rating"),
                "user_status": (a.get("status") or {}).get("comment"),
            })
        return out

    @app.get("/api/places/{place_key}")
    def place_detail(place_key: str) -> dict[str, Any]:
        rows = q("SELECT * FROM places WHERE place_key = ?", (place_key,))
        if not rows:
            raise HTTPException(404, "no such place")
        place = rows[0]
        listings = q("SELECT * FROM listings WHERE place_key = ? ORDER BY source, source_id", (place_key,))
        with lock:
            res = evaluate_all(conn)
            trips = best_trips(conn).get(place_key, {})
        out_listings = []
        for l in listings:
            checks = {c["field"]: c["verdict"] for c in q(
                "SELECT field, verdict FROM current_checks WHERE source = ? AND source_id = ?",
                (l["source"], l["source_id"]))}
            history = q("SELECT observed_at, price, price_basis, query_checkin, query_checkout, "
                        "json_extract(data, '$.available_from') AS available_from, content_hash "
                        "FROM observations WHERE source = ? AND source_id = ? ORDER BY observed_at, id",
                        (l["source"], l["source_id"]))
            raw = None
            if l.get("raw_path") and Path(l["raw_path"]).exists():
                raw = Path(l["raw_path"]).read_text()[:200_000]
            goal_results = {gid: res["listings"][gid].get((l["source"], l["source_id"])) for gid in res["listings"]}
            out_listings.append({
                "source": l["source"], "source_id": l["source_id"], "url": l["url"], "obs_id": l["obs_id"],
                "unit_key": l["unit_key"], "first_seen": l["first_seen"], "last_seen": l["last_seen"],
                "active": l["active"], "n_obs": l["n_obs"],
                "fields": {f: l.get(f) for f in LISTING_FIELDS},
                "overridden": (l["overridden"] or "").split(",") if l["overridden"] else [],
                "checks": checks,
                "media": json.loads(l["media"]) if l.get("media") else [],
                "history": history, "data": json.loads(l["data"]), "raw": raw,
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
        return {
            "place": place,
            "goals": {gid: res["places"][gid].get(place_key) for gid in res["places"]},
            "plans": group_plans(out_listings),
            "photos": gather_photos(out_listings),
            "listings": out_listings,
            "matrix": matrix,
            "nearest": nearest,
            "annotations": anns,
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
            agree = verify.agreement(conn)
            n_conflict = len(verify.conflict_queue(conn))
            n_sample = len(verify.sample_queue(conn))
        from ..trips import google_usage

        with lock:
            google = google_usage(conn)
        return {
            "runs": runs, "spend": spend, "errors": errors, "google": google,
            "accuracy": [{"source": s, "field": f, **a} for (s, f), a in sorted(acc.items())],
            "agreement": agree, "queues": {"conflict": n_conflict, "sample": n_sample},
            "counts": q("SELECT (SELECT COUNT(*) FROM places) AS places, "
                        "(SELECT COUNT(*) FROM listings) AS listings, "
                        "(SELECT COUNT(*) FROM listings WHERE active = 1) AS active")[0],
        }

    # ------------------------------------------------------------------ verify
    def _obs_fields(obs_id: int) -> dict[str, Any]:
        r = q("SELECT * FROM observations WHERE id = ?", (obs_id,))[0]
        data = json.loads(r["data"])
        out = {f: (r["price"] if f == "price" else data.get(f)) for f in load_goals().verify.sample_fields}
        return {"obs_id": obs_id, "source": r["source"], "source_id": r["source_id"], "url": r["url"],
                "observed_at": r["observed_at"], "fields": out, "lat": data.get("lat"), "lon": data.get("lon")}

    @app.get("/api/verify/next")
    def verify_next(seed: int = 0, skip: int = 0) -> dict[str, Any]:
        with lock:
            items = verify.conflict_queue(conn) + verify.sample_queue(conn, seed)
        if skip >= len(items):
            return {"done": True, "remaining": 0}
        item = items[skip]
        out: dict[str, Any] = {"done": False, "remaining": len(items), "item": item}
        if item["kind"] in ("conflict", "listing"):
            ids = [x["obs_id"] for x in item["listings"]] if item["kind"] == "conflict" else [item["obs_id"]]
            out["observations"] = [_obs_fields(i) for i in ids]
            if item["kind"] == "conflict":
                # the queue's price_month conflicts are checked on the stored price
                out["fields"] = sorted({verify.CHECK_FIELD.get(f, f) for f in item["fields"]})
            else:
                out["fields"] = load_goals().verify.sample_fields
            label = q("SELECT address, property_name FROM places WHERE place_key = ?", (item["place_key"],))
            out["place"] = label[0] if label else None
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
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/sw.js")
    def sw() -> FileResponse:
        # served from the root so the service worker's scope covers the whole app
        return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    return app
