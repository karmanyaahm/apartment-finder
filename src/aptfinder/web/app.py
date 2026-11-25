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
from ..config import Goal, load_goals, load_point_sets, load_points
from ..filter import (AMENITY_FLAGS, amenity_flags, place_restriction, best_trips, evaluate_all, evaluate_goal, goal_inputs, nearest_by_set,
                      place_commute_terms)
from ..filter import goals as goalstore
from ..filter.rules import AGGS, EQUALS_FIELDS, KNOWN_FIELDS, NUMERIC_FIELDS, RULE_TYPES
from ..plans import plan_image, site_drawings
from ..plans import plan_key as _plan_key
from ..store import append_annotation, append_check, append_link, connect, init_db
from ..store.models import CHECKABLE_FIELDS

STATIC = Path(__file__).parent / "static"
AUTHOR = "user"
# transit is not offered or shown (user, 2026-11-25): bike+train is the transit option; stored transit trips stay
MODES = ["walk", "bike", "drive", "bike_train_bike", "bike_bus_bike"]
DEFAULT_ATTRIBUTES = ["overall", "status", "gym", "walk_to_caltrain", "living_room"]
LISTING_FIELDS = ["price", "price_basis", "price_month", "price_per_bed", "beds", "baths", "sqft", "kind",
                  "term", "furnished", "private_bath", "available_from", "available_to", "address", "unit",
                  "property_name", "operator", "title", "amenities", "floor_plan"]

def _tv(t: dict[str, Any]) -> dict[str, Any]:
    """A trip as the list shows it: minutes and provider, plus the range of a KNN trip (filter.knn)."""
    return {"minutes": t["minutes"], "provider": t["provider"],
            **({"min": t["min"], "max": t["max"]} if t["provider"] == "knn" else {})}


SCRAPE_REFRESH_S = 120  # how stale the dashboard may be while an intake run is writing


def _group_listings(conn) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM listings WHERE place_key IS NOT NULL ORDER BY source, source_id"):
        out.setdefault(r["place_key"], []).append(dict(r))
    return out


def group_plans(listings: list[dict[str, Any]], files: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Floor plans of a place: listings grouped by beds, baths and plan name across all sources. When the
    building's own site prices an offer, other sources' prices are only 'also listed' (own website wins)."""
    drawings = site_drawings([l for l in listings if l["fields"].get("kind") == "site_info"])  # own-site drawings
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
            # one drawing: the community's own site first, then any source; hotlink-blocked hosts only from our stored copy
            "image": plan_image(ls, drawings, skip=HOTLINK_BLOCKED, local=files or {}),
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
# 2026-11-24). Their photos are listed last unless we hold our own copy (aptfinder.media; the box's downloads of
# them succeed, 2026-11-25).
HOTLINK_BLOCKED = ("images1.apartments.com",)


def local_plan_images(plans: list[dict[str, Any]], files: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Each plan's drawing from our stored copy when we have one (aptfinder.media), else the source URL."""
    for p in plans:
        if (f := files.get(p.get("image") or "")):
            p["image"] = f"media/{f['file']}.jpg"
    return plans


def gather_photos(listings: list[dict[str, Any]], limit: int = 30,
                  files: dict[str, dict[str, Any]] | None = None) -> list[dict[str, str]]:
    """Photos from every listing. A photo we have downloaded (`files`: url -> stored file, media.url_files) is
    served from our copy (`media/<sha>.jpg`, relative so it works under the base path), and photos that share one
    stored file (the same picture on several sites: the store maps near-duplicates of a place to one file) show
    once, and so do photos within media.PHOTO_MAX_DISTANCE pHash bits of one already shown. Others fall back to the
    source's URL. The community's own website comes first; hotlink-blocked URLs without a local copy go last, so
    loadable photos fill the limit first."""
    from ..media import listing_images, same_picture

    files = files or {}
    seen, seen_files, out = set(), {}, []
    for l in sorted(listings, key=lambda l: l["source"] != "direct"):
        for u, kind in listing_images(l["data"]):
            if kind != "photo" or u in seen:
                continue
            seen.add(u)
            f = files.get(u)
            # a copy of a photo already shown: the same stored file, or near in pHash (copies stored before the
            # threshold was tuned, or of another place's file)
            if f and (f["file"] in seen_files or any(same_picture(f["phash"], ph, "photo") is not None
                                                     for ph in seen_files.values())):
                continue
            if f:
                seen_files[f["file"]] = f["phash"]
            # `page`: the listing or site page the photo was found on; `site`: that page's host, for the overlay
            out.append({"url": f"media/{f['file']}.jpg" if f else u, "source": l["source"], "page": l.get("url"),
                        "site": site_name(l.get("url")), "original": u})
    out.sort(key=lambda p: p["url"].startswith("http") and any(h in p["url"] for h in HOTLINK_BLOCKED))
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


class GoneIn(BaseModel):
    source: str
    source_id: str
    gone: bool = True  # false: undo (the listing is still for rent)
    queue: str = "adhoc"  # 'conflict' from a Verify stale item, 'adhoc' from the drawer


class CellsIn(BaseModel):
    cells: list[tuple[str, str]]  # (point id or `set:<name>`, mode): the KNN grid's checked cells


class SampleIn(BaseModel):
    # cover: greedy set cover within radius_m; random: pct % of the scope; refine: KNN ranges straddling a goal's
    # limit or wider than sampling.REFINE_MAX_SPREAD_MIN, at places that can still pass that goal
    method: str = "cover"
    pct: float = 10
    radius_m: float = 400
    scope: str = "list"  # list (place_keys: the places the list shows) | zone | candidates, as ComputeIn
    place_keys: list[str] | None = None
    seed: int | None = None


class SampleRunIn(BaseModel):
    plan: dict[str, list[str]]  # "point|mode" -> place keys, from the dry run
    label: str = ""
    confirm_elements: int  # the dry run's element count: this run's cap
    confirm_left: int | None = None  # more than TYPED_CONFIRM_OVER elements: the elements left this month, typed


INTERRUPTED = "interrupted: the dashboard restarted mid-run"
KNN_MODES = ["drive", "bike", "walk", "bike_train_bike"]  # the KNN grid's columns (bike+train: commute targets only)


class ComputeIn(BaseModel):
    point: str  # a point id (`commute_targets:Meta HQ`) or `set:<name>`
    mode: str
    provider: str = "estimate"
    # "list": place_keys (the places the list shows); "zone": every place in a walk zone; "candidates": places in a
    # walk zone that can still pass some goal apart from commute (filter.routing_candidates)
    scope: str = "list"
    place_keys: list[str] | None = None
    confirm_calls: int | None = None  # Google: the dry run's call count, used as this run's cap
    # Google, more than TYPED_CONFIRM_OVER calls: the calls left this month, typed by hand from the dry run
    confirm_left: int | None = None


TYPED_CONFIRM_OVER = 20  # a Google run above this many calls needs the counts typed in


class MergeIn(BaseModel):
    into: str  # the place this one's listings join


class LinkIn(BaseModel):
    source: str
    source_id: str
    place_key: str | None = None  # None = split this listing into its own place


def goal_view(r: dict[str, Any], commute: list[tuple[float | None, bool]] | None = None) -> dict[str, Any]:
    """A place's result for one goal as the list needs it. `commute`: the net-commute terms, [minutes or null,
    estimate] per hard commute rule of the goal (filter.place_commute_terms), summed on the card."""
    return {"commute": [[m, e] for m, e in commute or []],"status": r["status"], "nice_score": r["nice_score"],
            "reasons": [t for s, t, *_ in r["reasons"] if s != "pass"],
            "fails": r["fails"],  # the attribute of each reason, for red/amber facts
            "unit_key": r["unit_key"], "source": r["source"], "source_id": r["source_id"]}


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
        # identity's rule links arrive with every intake run (130 a minute seen live), so only the user's own
        # links count as edits; rule links refresh with the scrapes
        row = conn.execute(
            "SELECT (SELECT MAX(id) FROM links WHERE author = 'user'), (SELECT MAX(id) FROM checks), "
            "(SELECT MAX(id) FROM annotations), (SELECT MAX(id) FROM links WHERE author <> 'user'), "
            "(SELECT MAX(id) FROM observations), (SELECT MAX(id) FROM trips), (SELECT MAX(id) FROM runs), "
            "(SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1), (SELECT MAX(id) FROM media)").fetchone()
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

    def media_files() -> dict[str, dict[str, Any]]:
        """url -> our stored copy, read once per data version."""
        from ..media import url_files

        return cached("media_files", lambda: url_files(conn))

    def q(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with lock:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def latest_annotations(entity_type: str, key: str | None = None) -> dict[str, dict[str, dict[str, Any]]]:
        sql = "SELECT * FROM annotations WHERE entity_type = ?" + (" AND entity_key = ?" if key else "")
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for r in q(sql + " ORDER BY id", (entity_type, key) if key else (entity_type,)):
            out.setdefault(r["entity_key"], {})[r["attribute"]] = r
        return out

    def effective() -> Any:
        with lock:
            return goalstore.effective_goals(conn)

    # ------------------------------------------------------------------ read API
    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        goals = effective()
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
            "readonly": redact is not None,  # the redacted view refuses every write: the goal editor is read-only
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
        goal_defs = effective().goals
        listings = q("SELECT * FROM listings WHERE place_key IS NOT NULL")
        by_place: dict[str, list[dict[str, Any]]] = {}
        for row in listings:
            by_place.setdefault(row["place_key"], []).append(row)
        anns = latest_annotations("place")
        # per place, the fastest trip to any point of each set and mode ("walk to the nearest Google bus stop"),
        # for the list's requirement rows; commute targets are in `minutes` one by one
        nearest_all = cached("nearest_sets", lambda: {
            pk: {f"{s}|{m}": {**_tv(t), "point": t["point_id"].split(":", 1)[1]}
                 for (s, m), t in nearest_by_set(tr).items() if s != "commute_targets"}
            for pk, tr in trips.items()})
        files = media_files()
        out = []
        for p in q("SELECT * FROM places"):
            pk = p["place_key"]
            rows = by_place.get(pk, [])
            goals = {}
            for gid, pres in res["places"].items():
                r = pres.get(pk)
                if r:
                    goals[gid] = goal_view(r, place_commute_terms(goal_defs[gid], trips, pk) if gid in goal_defs else None)
            title = next((x["title"] for x in rows if x.get("title")), None)
            parsed = [{**x, "data": json.loads(x["data"])} for x in rows]
            cover = gather_photos(parsed, limit=1, files=files)
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
                "minutes": {f"{pt}|{m}": _tv(t)
                            for (pt, m), t in trips.get(pk, {}).items()},
                "nearest": nearest_all.get(pk, {}),
                # amenity flags (filter.amenity_flags over every listing's list here): true / false, null when nothing listed
                "amenities": amenity_flags(sorted({a for x in rows if x.get("amenities")
                                                   for a in json.loads(x["amenities"])})),
                # a limit on who may rent that any listing here states (income, senior, ...): the card's badge
                "restriction": place_restriction(parsed),
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
        place["restriction"] = place_restriction([{**x, "data": json.loads(x["data"])} for x in listings])
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
                # off the market (view withdrawals): since when, by whom ('user' or 'zillow_detail'), the page's event
                "gone_at": l.get("gone_at"), "gone_by": l.get("gone_by"), "gone_why": l.get("gone_why"),
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
        matrix = {f"{pt}|{m}": {k: v[k] for k in ("id", "minutes", "provider", "legs", "distance_m", "min", "max", "neighbors")
                                if k in v}
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
            "plans": local_plan_images(group_plans(out_listings, media_files()), media_files()),
            "photos": gather_photos(out_listings, files=media_files()),
            "websites": websites(out_listings),
            "listings": out_listings,
            "matrix": matrix,
            "nearest": nearest,
            "annotations": anns,
            # other places within 300 m, for a manual merge of a duplicate building
            "nearby": nearby_places(place_key),
            # the building and its third-party stay places (`<building>~<operator>`), linked both ways
            "related": [dict(r, label=place_label(r)) for r in q(
                "SELECT place_key, property_name, operator, address, n_active, min_lease_month, min_stay_month "
                "FROM places WHERE (place_key = ? OR place_key LIKE ?) AND place_key <> ?",
                (place_key.split("~", 1)[0], place_key.split("~", 1)[0] + "~%", place_key))],
        }

    credit_memo: dict[str, Any] = {}

    def apify_credit() -> dict[str, Any]:
        """The live Apify credit (intake.apify.credit), reused for 60 s; {"error": ...} when it cannot be read."""
        from ..intake import apify

        if credit_memo and time.monotonic() - credit_memo["t"] < 60:
            return credit_memo["v"]
        try:
            v = apify.credit(tries=1)  # one try: an Apify outage must not hang the dashboard
        except (Exception, SystemExit) as e:  # a missing token raises SystemExit: never kill the request
            v = {"error": str(e)[:200]}
        credit_memo.update(t=time.monotonic(), v=v)
        return v

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        runs = q("""SELECT r.* FROM runs r JOIN (SELECT component, source, MAX(id) AS id FROM runs
                    GROUP BY component, source) m ON m.id = r.id ORDER BY r.component, r.source""")
        for r in runs:
            r["counts"] = json.loads(r["counts"]) if r["counts"] else {}
        spend = q("SELECT COALESCE(SUM(est_cost), 0) AS total FROM runs")[0]["total"]
        apify_real = q("SELECT COALESCE(SUM(json_extract(counts, '$.apify_usd')), 0) AS t FROM runs")[0]["t"]
        errors = q("SELECT id, started_at, component, source, error FROM runs WHERE status = 'error' "
                   "ORDER BY id DESC LIMIT 5")
        with lock:
            acc = verify.accuracy(conn)
            agree = cached("agreement", lambda: verify.agreement(conn))
            conflicts = cached("all_conflicts", lambda: verify.all_conflicts(conn))
            n_pending = sum(c["pending_render"] for c in conflicts)
            n_sample = cached("n_sample", lambda: len(verify.sample_queue(conn)))
            n_own = len(cached("own_site_queue", lambda: verify.own_site_queue(conn)))
            n_dup = len(duplicates())
        from ..trips import google_usage

        with lock:
            google = google_usage(conn)
        return {
            "runs": runs, "spend": spend, "apify_real": apify_real, "apify": apify_credit(), "errors": errors,
            "google": google,
            "accuracy": [{"source": s, "field": f, **a} for (s, f), a in sorted(acc.items())],
            "agreement": agree, "queues": {"conflict": len(conflicts) - n_pending, "conflict_pending": n_pending,
                                           "sample": n_sample,
                                           "own_site": n_own, "duplicates": n_dup},
            "counts": cached("counts", lambda: q("SELECT (SELECT COUNT(*) FROM places) AS places, "
                                                 "(SELECT COUNT(*) FROM listings) AS listings, "
                                                 "(SELECT COUNT(*) FROM listings WHERE active = 1) AS active")[0]),
        }

    @app.get("/api/pipeline")
    def pipeline_overview() -> dict[str, Any]:
        """Each pipeline stage's state from the logs: done / partial / todo / stale / blocked, and the commands
        that would run the rest (aptfinder.pipeline)."""
        return {**cached("pipeline", lambda: pipeline.overview(conn)), "apify": apify_credit()}

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
            out["fields"] = (["price", "beds"] if item.get("cheap")  # the price is the check that settles it
                             else sorted({verify.CHECK_FIELD.get(f, f) for f in item["fields"]}))
        else:
            out["fields"] = load_goals().verify.sample_fields
        p = all_places().get(item["place_key"])
        out["place"] = {"address": p["address"], "property_name": p["property_name"]} if p else None
        out["label"] = place_label(p) if p else None
        return out

    def filtered_conflicts(goal: str, zone: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """The Verify conflicts that matter to one goal (verify.goal_relevance) and/or lie in a station walk zone,
        plus how many each filter hid. goal '' and zone '' keep everything."""
        items = cached("all_conflicts", lambda: verify.all_conflicts(conn))
        hidden = {"irrelevant": 0, "settled": 0, "same": 0, "zone": 0}
        if not goal and not zone:
            return items, hidden

        def compute() -> tuple[list[dict[str, Any]], dict[str, int]]:
            keep = items
            if goal:
                g = effective().goals.get(goal)
                if g is None:
                    raise HTTPException(400, f"unknown goal {goal!r}")
                lres = cached("evaluate_all", lambda: evaluate_all(conn))["listings"].get(goal, {})
                rows = cached("goal_input_rows", lambda: {(r["source"], r["source_id"]): r for r in
                                                          cached("goal_inputs", lambda: goal_inputs(conn))["listings"]})
                low = cached("goal_inputs", lambda: goal_inputs(conn))["low"]
                out = []
                for c in keep:
                    v = verify.goal_relevance(c, g.rules, lres, rows, low)
                    if v == "shown":
                        out.append(c)
                    else:
                        hidden[v] += 1
                keep = out
            if zone == "walk":
                places = all_places()
                out = []
                for c in keep:
                    p = places.get(c["place_key"] or "")
                    if p and in_walk_zone(p["lat"], p["lon"]):
                        out.append(c)
                    else:
                        hidden["zone"] += 1
                keep = out
            return keep, hidden

        return cached(f"conflicts|{goal}|{zone}", compute)

    @app.get("/api/verify/batch")
    def verify_batch(kind: str = "conflict", skip: int = 0, n: int = 10, goal: str = "",
                     zone: str = "") -> dict[str, Any]:
        """Up to n conflicts from `skip` (actionable first, those waiting for an own-site render last), each with
        its observations, disagreeing fields and place label, plus the totals. `goal`: only conflicts whose
        resolution could change that goal's outcome (verify.goal_relevance); `zone=walk`: only places inside a
        station walk zone. `all_total` counts every open conflict, `hidden` what each filter left out."""
        if kind != "conflict":
            raise HTTPException(400, "batch is for conflicts; use /api/verify/next for the rest")
        if zone not in ("", "walk"):
            raise HTTPException(400, "zone is '' or 'walk'")
        with lock:
            all_items = cached("all_conflicts", lambda: verify.all_conflicts(conn))
            items, hidden = filtered_conflicts(goal, zone)
            checked = verify.current_checks(conn)
        n = max(1, min(n, 50))
        pending = sum(c["pending_render"] for c in items)
        page = [{"item": it, **_listing_detail(it)} for it in items[max(0, skip):max(0, skip) + n]]
        for e in page:  # verdicts already given on a partly checked conflict, so the screen shows them
            for o in e["observations"]:
                o["checked"] = checked.get((o["source"], o["source_id"]), {})
        return {"kind": kind, "skip": skip, "n": n, "total": len(items), "actionable": len(items) - pending,
                "pending": pending, "cheap": sum(bool(c.get("cheap")) for c in items), "items": page,
                "goal": goal, "zone": zone, "all_total": len(all_items), "hidden": hidden,
                "hidden_labels": {**verify.GOAL_HIDDEN, "zone": "outside the station walk zones"}}

    @app.get("/api/verify/own_sites")
    def verify_own_sites(skip: int = 0, n: int = 20) -> dict[str, Any]:
        """Named buildings whose own site gave no usable offer, n at a time, each with its reason and every URL
        tried (verify.own_site_queue), plus the count per reason."""
        with lock:
            items = cached("own_site_queue", lambda: verify.own_site_queue(conn))
        from collections import Counter

        return {"total": len(items), "skip": skip, "n": n, "items": items[skip:skip + n],
                "by_reason": dict(Counter(i["reason"] for i in items)), "labels": verify.OWN_SITE_REASONS}

    def duplicates() -> list[dict[str, Any]]:
        return cached("duplicate_queue", lambda: verify.duplicate_queue(conn, list(all_places().values())))

    @app.get("/api/verify/duplicates")
    def verify_duplicates(skip: int = 0, n: int = 20) -> dict[str, Any]:
        """Pairs of places that are probably one building, n at a time, each with its reasons (verify.duplicate_queue),
        plus the count per reason. Answer with POST /api/places/{key}/merge or an annotation place_pair / pair_key /
        same_building = no."""
        from collections import Counter

        with lock:
            items = duplicates()
        allp = all_places()
        page = [{**e, "places": [{**p, "label": place_label(allp.get(p["place_key"]) or p) or p["place_key"]}
                                 for p in e["places"]]} for e in items[max(0, skip):max(0, skip) + n]]
        return {"total": len(items), "skip": skip, "n": n, "items": page, "labels": verify.DUP_REASONS,
                "by_reason": dict(Counter(r["kind"] for e in items for r in e["reasons"]))}

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

    @app.post("/api/listings/gone")
    def post_gone(g: GoneIn) -> dict[str, Any]:
        """The user's 'no longer for rent' mark on a listing (gone), or its undo: an appended checks row on field
        `active` against the listing's newest observation. The listings view treats the listing as inactive while
        this is its newest withdrawal evidence (view withdrawals); a later fresh scrape that sees it brings it back."""
        if g.queue not in ("conflict", "adhoc"):
            raise HTTPException(400, "queue must be 'conflict' or 'adhoc'")
        rows = q("SELECT id FROM observations WHERE source = ? AND source_id = ? ORDER BY observed_at DESC, id DESC "
                 "LIMIT 1", (g.source, g.source_id))
        if not rows:
            raise HTTPException(404, "no such listing")
        with lock:
            cid = append_check(conn, AUTHOR, g.queue, "active", "wrong", observation_id=rows[0]["id"],
                               stored_value="offered" if g.gone else "gone",
                               true_value="gone" if g.gone else "offered",
                               note=None if g.gone else "undo: still for rent")
        return {"id": cid}

    # ------------------------------------------------------------------ other writes
    @app.post("/api/annotations")
    def post_annotation(a: AnnotationIn) -> dict[str, Any]:
        if a.entity_type == "goal":
            raise HTTPException(400, "goals are edited through /api/goals")
        if a.rating is not None and not 1 <= a.rating <= 5:
            raise HTTPException(400, "rating must be 1 to 5")
        if a.rating is None and not (a.comment or "").strip():
            raise HTTPException(400, "rating or comment required")
        with lock:
            aid = append_annotation(conn, AUTHOR, a.entity_type, a.entity_key, a.attribute.strip().lower(),
                                    goal_id=a.goal_id, rating=a.rating, comment=a.comment)
        return {"id": aid}

    # ------------------------------------------------------------------ goals (filter.goals): goals.toml is the
    # seed; an edit, a new goal or a delete is an annotations row (goal / <id> / definition), latest wins
    def goal_refs() -> dict[str, Any]:
        pts = load_points()
        # a goal may still name transit (stored Google transit trips); it is only not offered or shown
        return {"modes": MODES + ["transit"], "points": {p.id for v in pts.values() for p in v},
                "sets": set(pts) | set(load_point_sets())}

    def checked_goal(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return goalstore.validate_goal(body, **goal_refs())
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    def save_goal(gid: str, d: dict[str, Any]) -> dict[str, Any]:
        with lock:
            aid = append_annotation(conn, AUTHOR, "goal", gid, "definition", comment=json.dumps(d, sort_keys=True))
        return {"id": aid, "goal": gid}

    @app.get("/api/goals")
    def list_goals() -> dict[str, Any]:
        seed = load_goals().goals
        with lock:
            edits = goalstore.goal_edits(conn)
        refs = goal_refs()
        return {
            "goals": [{"id": g, "label": v.label, "rules": v.rules, "nice": v.nice,
                       "origin": "new" if g not in seed else "edited" if g in edits else "goals.toml"}
                      for g, v in effective().goals.items()],
            "deleted": sorted(g for g, d in edits.items() if d is None),
            # what the editor offers: rule types (label, required and optional keys), fields, presets
            "types": {t: {"label": lab, "keys": req, "optional": opt} for t, (lab, req, opt) in RULE_TYPES.items()},
            "aggs": AGGS, "numeric_fields": NUMERIC_FIELDS, "equals_fields": EQUALS_FIELDS, "known_fields": KNOWN_FIELDS,
            "amenities": {k: {"match": w, "exclude": x} for k, (w, x) in AMENITY_FLAGS.items()},
            "modes": refs["modes"], "points": sorted(refs["points"]), "sets": sorted(refs["sets"]),
        }

    @app.post("/api/goals")
    def create_goal(body: dict[str, Any]) -> dict[str, Any]:
        d = checked_goal(body)
        gid = body.get("id") or re.sub(r"[^a-z0-9]+", "_", d["label"].lower()).strip("_")[:40]
        try:
            goalstore.check_id(gid)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if gid in effective().goals:
            raise HTTPException(409, f"goal {gid!r} exists")
        return save_goal(gid, d)

    @app.put("/api/goals/{gid}")
    def update_goal(gid: str, body: dict[str, Any]) -> dict[str, Any]:
        if gid not in effective().goals:
            raise HTTPException(404, f"no goal {gid!r}")
        return save_goal(gid, checked_goal(body))

    @app.delete("/api/goals/{gid}")
    def delete_goal(gid: str) -> dict[str, Any]:
        if gid not in effective().goals:
            raise HTTPException(404, f"no goal {gid!r}")
        return save_goal(gid, {"deleted": True})

    @app.post("/api/goals/preview")
    def preview_goal(body: dict[str, Any]) -> dict[str, Any]:
        """An unsaved goal judged on the stored data, writes nothing: place_key -> the same per-goal entry as
        /api/places, for the list while you edit."""
        d = checked_goal(body)
        inp = cached("goal_inputs", lambda: goal_inputs(conn))
        goal = Goal(**d)
        _, pres = evaluate_goal(inp, goal)
        return {"places": {pk: goal_view(r, place_commute_terms(goal, inp["trips"], pk)) for pk, r in pres.items()}}

    def nearby_places(place_key: str, max_m: float = 300.0, n: int = 8) -> list[dict[str, Any]]:
        from ..commute.geo import haversine_m

        allp = all_places()
        me = allp.get(place_key)
        if not me or me.get("lat") is None:
            return []
        out = []
        for k, p in allp.items():
            if k == place_key or p.get("lat") is None or "~" in k:
                continue
            d = haversine_m((me["lat"], me["lon"]), (p["lat"], p["lon"]))
            if d <= max_m:
                out.append({"place_key": k, "label": place_label(p) or k, "address": p.get("address"),
                            "metres": round(d), "n_listings": p.get("n_listings")})
        return sorted(out, key=lambda x: x["metres"])[:n]

    @app.post("/api/places/{place_key}/merge")
    def merge_place(place_key: str, body: MergeIn) -> dict[str, Any]:
        """Every listing of this place becomes the other place's, as user links (append-only; each listing keeps
        its own unit: '<this place>/<unit>' -> '<other place>/<unit>'). Undo a listing with 'split'."""
        if body.into == place_key:
            raise HTTPException(400, "a place cannot merge into itself")
        if not q("SELECT 1 FROM places WHERE place_key = ?", (body.into,)):
            raise HTTPException(404, "no such place to merge into")
        rows = q("SELECT source, source_id, unit_key FROM listings WHERE place_key = ?", (place_key,))
        if not rows:
            raise HTTPException(404, "no listings at this place")
        with lock:
            for r in rows:
                uk = r["unit_key"] or place_key
                unit = body.into + uk[len(place_key):] if uk.startswith(place_key) else body.into
                append_link(conn, AUTHOR, r["source"], r["source_id"], body.into, unit,
                            f"manual merge of {place_key}", commit=False)
            conn.commit()
        return {"merged": len(rows), "into": body.into}

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
        # never a silent fallback to estimates: without a key, Google routing is an error the button shows
        provider = provider or "google"
        if provider not in ("google", "estimate"):
            raise HTTPException(400, "provider must be google or estimate")
        if provider == "google" and not settings.secret("GOOGLE_MAPS_API_KEY"):
            raise HTTPException(400, "GOOGLE_MAPS_API_KEY is not set on the server; nothing was routed")
        source = make_source(provider, max_calls=60)
        commute = build_commute(db_path, source)
        try:
            with lock:
                n = compute_for_place(conn, commute, rows[0], on_demand=True)
        except SpendCapReached as e:
            raise HTTPException(429, str(e))
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        finally:
            # the user's own routing shows at once: trips count as scraped rows, which the cache may reuse for
            # SCRAPE_REFRESH_S, so the drawer refreshed with the old estimates
            with lock:
                memo.clear()
        return {"new_trips": n, "provider": provider, "api_calls": getattr(source, "calls", 0)}

    # ------------------------------------------------------------------ compute one attribute (Pipeline panel)
    # One (point or set, mode) for a chosen list of places: a dry run first (places, uncached paid calls, cost
    # against the monthly caps), then a run in a background thread. A Google run must carry the dry run's call
    # count, which becomes its --max-calls, so it never spends more than the dry run showed.
    job: dict[str, Any] = {"state": "idle"}
    # A job runs in a thread of this process, so a restart (a deploy) kills it mid-run and leaves its runs row open.
    # On start, close out this dashboard's own open commute runs (params `from` = dashboard; a CLI or after-intake run
    # in another process may still be going) and report them, instead of a bare 'idle' the page read as an error.
    if redact is None:
        with lock:
            from ..store import finish_run

            cut = [r for r in conn.execute("SELECT id, params FROM runs WHERE component = 'commute' AND "
                                           "status = 'running' AND finished_at IS NULL").fetchall()
                   if json.loads(r[1] or "{}").get("from") == "dashboard"]
            for r in cut:
                finish_run(conn, r[0], "error", {}, error=INTERRUPTED)
            if cut:
                job.update(state="error", interrupted=[r[0] for r in cut],
                           result={"error": f"{INTERRUPTED} (run{'s' if len(cut) > 1 else ''} "
                                            f"{', '.join(str(r[0]) for r in cut)}); press Run again: elements already "
                                            "paid for are cached and not billed again"})

    def _compute_places(body: ComputeIn) -> list[str]:
        located = {r["place_key"]: (r["lat"], r["lon"]) for r in all_places().values() if r["lat"] is not None}
        if body.scope == "zone":
            keys = [pk for pk, (la, lo) in located.items() if in_walk_zone(la, lo)]
        elif body.scope == "candidates":
            from ..filter import routing_candidates

            res = cached("evaluate_all", lambda: evaluate_all(conn))
            cands = routing_candidates(conn, res=res)
            keys = [pk for pk in cands if pk in located and in_walk_zone(*located[pk])]
        else:
            keys = body.place_keys or []
        # a stay place (`<building>~<operator>`) reads its building's trips: route the building when it is a place
        out = {(pk.split("~", 1)[0] if pk.split("~", 1)[0] in located else pk) for pk in keys}
        return sorted(pk for pk in out if pk in located)

    def _check_spec(body: ComputeIn) -> tuple[str, str]:
        if body.provider not in ("estimate", "google"):
            raise HTTPException(400, "provider must be estimate or google")
        if body.mode not in MODES:
            raise HTTPException(400, f"unknown mode {body.mode!r}")
        sets = load_point_sets()
        if body.point.startswith("set:"):
            if body.point[4:] not in sets:
                raise HTTPException(400, f"no point set {body.point[4:]!r}")
        elif not any(p.id == body.point for p in load_points().get(body.point.split(":", 1)[0], [])):
            raise HTTPException(400, f"no point {body.point!r}")
        return body.point, body.mode

    @app.post("/api/compute/dry_run")
    def compute_dry_run(body: ComputeIn) -> dict[str, Any]:
        from ..trips import dry_run, google_usage

        only = _check_spec(body)
        keys = _compute_places(body)
        if not keys:
            raise HTTPException(400, "no located places in that scope")
        if body.provider == "estimate":  # free: no calls to count
            with lock:
                use = google_usage(conn)
            return {"places": len(keys), "would_call": 0, "est_cost_usd": 0.0, "over_caps": [],
                    "google_usage_now": use, "provider": "estimate"}
        res = dry_run(db_path, "google", keys, on_demand=True, only=only)
        cfg = load_point_sets().get(only[0][4:] if only[0].startswith("set:") else only[0].split(":", 1)[0])
        if cfg is not None and not cfg.paid:
            res["note"] = "point_sets.toml has paid = false for this set: Google is never used for it"
        return {**res, "provider": "google"}

    @app.post("/api/compute/run")
    def compute_run(body: ComputeIn) -> dict[str, Any]:
        from ..store import begin_write, finish_run, start_run
        from ..trips import build_commute, compute_all, make_source

        only = _check_spec(body)
        keys = _compute_places(body)
        if not keys:
            raise HTTPException(400, "no located places in that scope")
        if body.provider == "google":
            if not body.confirm_calls or body.confirm_calls < 1:
                raise HTTPException(400, "a Google run needs the dry run's call count (confirm_calls)")
            if not settings.secret("GOOGLE_MAPS_API_KEY"):
                raise HTTPException(400, "GOOGLE_MAPS_API_KEY is not set")
            if body.confirm_calls > TYPED_CONFIRM_OVER:
                from ..trips import google_usage

                with lock:
                    u = google_usage(conn)
                left = u["cap"] - u["calls"]
                if left < 5000 and body.confirm_left != left:
                    raise HTTPException(400, f"more than {TYPED_CONFIRM_OVER} Google calls: type the calls left this "
                                             f"month ({left}) to confirm")
        with lock:
            if job["state"] == "running":
                raise HTTPException(409, "a computation is already running")
            job.clear()
            job.update(state="running", spec={"point": body.point, "mode": body.mode, "provider": body.provider},
                       places=len(keys), done=0, new_trips=0, started=time.time())

        def work() -> None:
            wconn = connect(db_path)  # its own connection: the dashboard stays responsive while this runs
            run = None
            try:
                source = make_source(body.provider, max_calls=body.confirm_calls or 0, meter_db=db_path)
                commute = build_commute(db_path, source)
                # SQLite has one writer: say so while another job holds the write lock, instead of looking stuck
                begin_write(wconn, on_wait=lambda: job.update(note="waiting for the database (another job is writing)"))
                run = start_run(wconn, "commute", body.provider, {"places": len(keys), "only": list(only),
                                                                   "from": "dashboard"})
                job.pop("note", None)

                def progress(done: int, total: int, new: int) -> None:
                    job.update(done=done, new_trips=new)

                res = compute_all(wconn, commute, keys, on_demand=True, only=only, progress=progress)
                calls = getattr(source, "calls", 0)
                finish_run(wconn, run, "error" if res["error"] else "ok", {**res, "api_calls": calls},
                           est_cost=round(calls * 0.005, 3) if body.provider == "google" else 0.0,
                           error=res["error"])
                final = {"state": "error" if res["error"] else "done", "result": {**res, "api_calls": calls}}
            except BaseException as e:  # SystemExit from make_source included; 'database is locked' shown as is
                final = {"state": "error", "result": {"error": str(e) or repr(e)}}
                try:
                    if run is not None:
                        finish_run(wconn, run, "error", {}, error=str(e))
                except Exception:  # noqa: BLE001  (the job must still end, whatever the database says)
                    pass
            finally:
                wconn.close()
            with lock:
                memo.clear()  # the user's own run: show its trips now, not after SCRAPE_REFRESH_S
                job.pop("note", None)
                job.update(final, finished=time.time())  # published only after the cache is cleared

        threading.Thread(target=work, daemon=True).start()
        return {"started": True, "places": len(keys)}

    @app.get("/api/compute/status")
    def compute_status() -> dict[str, Any]:
        return dict(job)

    @app.get("/api/compute/log")
    def compute_log(n: int = 15) -> list[dict[str, Any]]:
        """The latest commute computations (dashboard or CLI): when, what, how many places, Google calls, outcome.
        Every single Google call is in `api_calls` as well."""
        rows = q("SELECT id, started_at, finished_at, source, params, counts, est_cost, status, error FROM runs "
                 "WHERE component = 'commute' ORDER BY id DESC LIMIT ?", (max(1, min(n, 100)),))
        for r in rows:
            r["params"] = json.loads(r["params"] or "{}")
            r["counts"] = json.loads(r["counts"] or "{}")
        return rows

    # ------------------------------------------------------------------ KNN cells and Google sampling (Pipeline panel)
    # The checked cells (destination x mode) decide which pairs get KNN ranges (filter.knn) and which pairs a sample
    # routes; they are an annotations row like a goal edit (latest wins), shared by everyone.
    @app.get("/api/knn/cells")
    def knn_cells_get() -> dict[str, Any]:
        from ..filter.knn import K, RADIUS_M, cells

        from ..trips import K_STATIONS, load_google_caps

        sets, pts = load_point_sets(), load_points()
        rows = [{"id": p.id, "label": p.name, "kind": "target", "paid": True}
                for p in pts.get("commute_targets", [])]
        # k and points: how many routes one place costs (k nearest by straight line; k = 0: every point)
        rows += [{"id": f"set:{k}", "label": v.label or k, "kind": "set", "paid": v.paid, "k": v.k,
                  "points": len(pts.get(k, []))} for k, v in sets.items() if k != "commute_targets"]
        caps = load_google_caps()
        return {"cells": [list(c) for c in cells(conn)], "rows": rows, "modes": KNN_MODES, "radius_m": RADIUS_M,
                "knn_k": K, "typed_over": TYPED_CONFIRM_OVER, "k_stations": K_STATIONS, "matrix_cap": caps.get("matrix_monthly_element_cap", 0),
                "matrix_free": caps.get("matrix_free_elements_per_month", 0)}

    @app.post("/api/knn/cells")
    def knn_cells_set(body: CellsIn) -> dict[str, Any]:
        from ..filter.knn import CELLS_KEY

        ids = {r["id"] for r in knn_cells_get()["rows"]}
        for p, m in body.cells:
            if p not in ids or m not in KNN_MODES or (m == "bike_train_bike" and p.startswith("set:")):
                raise HTTPException(400, f"no such cell {p} x {m}")
        with lock:
            append_annotation(conn, AUTHOR, *CELLS_KEY, comment=json.dumps(sorted(set(map(tuple, body.cells)))))
            memo.clear()
        return {"cells": sorted(set(map(tuple, body.cells)))}

    def cell_accuracy(pairs: list[tuple[str, str]], keys: list[str]) -> dict[str, Any]:
        """Per checked cell: how the scope's places get their time (own Google route / KNN range / estimate, %) and
        the KNN accuracy by leave-one-out over every place with its own Google route (scope-free, cached per cell)."""
        from .. import sampling

        with lock:
            trips = cached("best_trips", lambda: best_trips(conn))
            loc = cached("located", lambda: sampling.located(conn))
            return {"|".join(p): {**sampling.cell_shown(trips, p, keys),
                                  "loo": cached(f"knn_loo|{'|'.join(p)}", lambda p=p: sampling.cell_loo(trips, p, loc))}
                    for p in pairs}

    @app.post("/api/knn/accuracy")
    def knn_accuracy(body: SampleIn) -> dict[str, Any]:
        """The Pipeline panel's accuracy table for the checked cells over the chosen scope (cell_accuracy)."""
        from ..filter.knn import K, RADIUS_M, cells
        from ..filter.rules import KNN_PAD_MIN

        keys = _compute_places(body)
        return {"cells": cell_accuracy(cells(conn), keys), "scope": len(keys), "radius_m": RADIUS_M, "knn_k": K,
                "pad_min": KNN_PAD_MIN}

    def candidates() -> dict[str, set[str]]:
        """place_key -> goals it can still pass apart from commute rules (sampling.candidate_goals), cached."""
        from .. import sampling

        return cached("candidate_goals", lambda: sampling.candidate_goals(
            conn, res=cached("evaluate_all", lambda: evaluate_all(conn))))

    def hinges() -> dict[str, Any]:
        """place_key -> goal -> the commute pairs its status waits on (sampling.hinges), cached."""
        from .. import sampling

        return cached("hinges", lambda: sampling.hinges(cached("evaluate_all", lambda: evaluate_all(conn)),
                                                         cached("best_trips", lambda: best_trips(conn)),
                                                         goalstore.effective_goals(conn),
                                                         cached("located", lambda: sampling.located(conn))))

    def scope_status(keys: list[str], pairs: list[tuple[str, str]]) -> dict[str, Any]:
        """sampling.commute_status for one scope; each verdict's plan costs come from the same planners as the dry
        run (cover within knn.RADIUS_M, refine)."""
        from .. import sampling
        from ..filter.knn import RADIUS_M, loo_samples
        from ..trips import planned_legs

        trips = cached("best_trips", lambda: best_trips(conn))
        loc = cached("located", lambda: sampling.located(conn))
        cfg = goalstore.effective_goals(conn)

        def cost(method: str, pair: tuple[str, str]) -> tuple[int, int]:
            if method == "cover":
                pks = sampling.cover_plan(trips, [pair], keys, loc, RADIUS_M)[pair]
            else:
                pks = sampling.refine_plan(trips, [pair], keys, cfg, candidates())[0][pair]
            return len(pks), (len(planned_legs(db_path, pks, pair)) if pks else 0)

        return sampling.commute_status(
            trips, pairs, keys, hinges(),
            lambda p: cached(f"knn_loo_samples|{'|'.join(p)}",
                             lambda: loo_samples(trips, loc, sampling.cell_keys(trips, p))),
            lambda p: cached(f"est_errors|{'|'.join(p)}",
                             lambda: sampling.estimate_errors(conn, sampling.cell_keys(trips, p))), cost)

    @app.post("/api/knn/status")
    def knn_status(body: SampleIn) -> dict[str, Any]:
        """The panel's 'do I still need to sample' table: per checked cell, for the current search results (the
        place_keys sent) and for every place in a walk zone, p90 / p99 expected error, places whose goal status
        waits on the cell, and a verdict (sampling.commute_status). Computed on read, cached per data version."""
        from ..filter.knn import RADIUS_M, cells
        from ..sampling import DONE_P99_MIN

        sets = load_point_sets()
        pairs = [c for c in cells(conn) if not c[0].startswith("set:") or (c[0][4:] in sets and sets[c[0][4:]].paid)]
        out: dict[str, Any] = {"done_p99": DONE_P99_MIN, "radius_m": RADIUS_M}
        for scope, spec in (("list", SampleIn(scope="list", place_keys=body.place_keys or [])),
                            ("zone", SampleIn(scope="zone"))):
            keys = _compute_places(spec)
            tag = hashlib.sha1("\n".join(keys).encode()).hexdigest()[:12]
            out[scope] = cached(f"knn_status|{tag}|{json.dumps(pairs)}", lambda keys=keys: scope_status(keys, pairs))
        return out

    @app.post("/api/sample/dry_run")
    def sample_dry_run(body: SampleIn) -> dict[str, Any]:
        """A sampling plan for the checked cells over the scope, with places, uncached Route Matrix elements and
        coverage (places with a routed place within the KNN radius) before -> after per cell. Nothing is routed."""
        from .. import sampling
        from ..filter.knn import cells
        from ..trips import google_usage

        pairs = [c for c in cells(conn) if load_point_sets().get(c[0][4:] if c[0].startswith("set:")
                                                                     else c[0].split(":", 1)[0]) is not None]
        if not pairs:
            raise HTTPException(400, "check at least one cell (destination x mode) first")
        skipped = [c for c in pairs if c[0].startswith("set:") and not load_point_sets()[c[0][4:]].paid]
        pairs = [c for c in pairs if c not in skipped]
        keys = _compute_places(body)
        if not keys:
            raise HTTPException(400, "no located places in that scope")
        with lock:
            trips = cached("best_trips", lambda: best_trips(conn))
            if body.method == "random":
                plan = sampling.random_plan(trips, pairs, keys, body.pct, body.seed)
                label = f"random {body.pct:g}%"
            elif body.method == "cover":
                plan = sampling.cover_plan(trips, pairs, keys, sampling.located(conn), body.radius_m)
                label = f"cover within {body.radius_m:g} m"
            elif body.method == "refine":
                plan, notes = sampling.refine_plan(trips, pairs, keys, goalstore.effective_goals(conn), candidates())
                label = "refine"
            else:
                raise HTTPException(400, "method must be cover, random or refine")
            summ = sampling.summary(conn, db_path, plan, keys, trips)
            hinge = sampling.hinge_counts(hinges(), keys, pairs, plan)
            use = google_usage(conn)
        left = use["matrix_cap"] - use["matrix_elements"]
        if body.method == "refine":
            for p, n in notes.items():
                summ["pairs"]["|".join(p)].update(n)
        return {**summ, "label": label, "plan": {"|".join(p): v for p, v in plan.items()}, "usage": use,
                "hinge": hinge,
                "accuracy": cell_accuracy(pairs, keys),
                "left": left, "typed": summ["elements"] > TYPED_CONFIRM_OVER and left < 5000,
                "skipped": [f"{p} x {m}: paid = false in point_sets.toml" for p, m in skipped]}

    @app.post("/api/sample/run")
    def sample_run(body: SampleRunIn) -> dict[str, Any]:
        from ..store import begin_write
        from ..trips import google_usage, route_plan

        if not settings.secret("GOOGLE_MAPS_API_KEY"):
            raise HTTPException(400, "GOOGLE_MAPS_API_KEY is not set")
        if body.confirm_elements < 1:
            raise HTTPException(400, "nothing to route")
        with lock:
            u = google_usage(conn)
        left = u["matrix_cap"] - u["matrix_elements"]
        if body.confirm_elements > TYPED_CONFIRM_OVER and left < 5000 and body.confirm_left != left:
            raise HTTPException(400, f"more than {TYPED_CONFIRM_OVER} Route Matrix elements: type the elements left "
                                     f"this month ({left}) to confirm")
        plan = {tuple(k.split("|", 1)): v for k, v in body.plan.items() if v}
        n = sum(len(v) for v in plan.values())
        with lock:
            if job["state"] == "running":
                raise HTTPException(409, "a computation is already running")
            job.clear()
            job.update(state="running", spec={"point": ", ".join(" x ".join(p) for p in plan), "mode": body.label,
                                              "provider": "google matrix"},
                       places=n, done=0, new_trips=0, started=time.time())

        def work() -> None:
            wconn = connect(db_path)
            try:
                begin_write(wconn, on_wait=lambda: job.update(note="waiting for the database (another job is writing)"))
                job.pop("note", None)
                res = route_plan(wconn, db_path, plan, body.label, body.confirm_elements,
                                 progress=lambda d, t: job.update(done=d, new_trips=t), origin="dashboard")
                final = {"state": "error" if res["error"] else "done",
                         "result": {"places": n, "places_done": n if not res["error"] else job.get("done", 0),
                                    "new_trips": res["new_trips"], "api_calls": res["api_calls"],
                                    "matrix_elements": res["matrix_elements"], "error": res["error"]}}
            except BaseException as e:  # noqa: BLE001  (SystemExit from a missing key included)
                final = {"state": "error", "result": {"error": str(e) or repr(e)}}
            finally:
                wconn.close()
            with lock:
                memo.clear()
                job.pop("note", None)
                job.update(final, finished=time.time())

        threading.Thread(target=work, daemon=True).start()
        return {"started": True, "places": n}

    # ------------------------------------------------------------------ static
    settings.MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    @app.get("/media/{sha}.jpg")
    def media_file(sha: str) -> FileResponse:
        """A stored photo (aptfinder.media): the name is the hash of the bytes, so it is cacheable forever.
        Registered before the /media mount, which keeps serving the flat seed files."""
        from ..media import media_path

        if not re.fullmatch(r"[0-9a-f]{64}", sha) or not media_path(sha).exists():
            raise HTTPException(404, "no such media file")
        return FileResponse(media_path(sha), media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

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
