"""Dashboard: FastAPI on 127.0.0.1, JSON API plus one static page. Writes only via store.append_*."""

from __future__ import annotations

import gzip
import hashlib
import itertools
import json
import pickle
import queue
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import pipeline, settings, verify
from ..areas import in_walk_zone
from ..areas import zones as area_zones
from ..config import Goal, load_goals, load_operators, load_point_sets, load_points
from ..filter import (AMENITY_FLAGS, place_building_units, place_parking, place_restriction, best_trips, evaluate_goal, goal_inputs, nearest_by_set,
                      place_commute_terms)
from ..filter import goals as goalstore
from ..filter.rules import AGGS, EQUALS_FIELDS, KNOWN_FIELDS, NUMERIC_FIELDS, RULE_TYPES
from ..plans import inherit_baths, plan_image, site_drawings
from ..plans import plan_key as _plan_key
from ..store import append_annotation, append_check, append_link, connect, init_db
from ..store.models import CHECKABLE_FIELDS

STATIC = Path(__file__).parent / "static"
AUTHOR = "user"
# transit is shown again (user, 2026-11-25, later): Google transit through Route Matrix, KNN like the other modes
MODES = ["walk", "bike", "transit", "drive", "bike_train_bike", "bike_bus_bike"]
DEFAULT_ATTRIBUTES = ["overall", "status", "gym", "walk_to_caltrain", "living_room"]
LISTING_FIELDS = ["price", "price_basis", "price_month", "price_per_bed", "beds", "baths", "sqft", "kind",
                  "term", "furnished", "private_bath", "available_from", "available_to", "address", "unit",
                  "property_name", "operator", "title", "amenities", "floor_plan"]

def _tv(t: dict[str, Any]) -> dict[str, Any]:
    """A trip as the list shows it: minutes and provider, plus the range of a KNN trip (filter.knn)."""
    return {"minutes": t["minutes"], "provider": t["provider"],
            **({"min": t["min"], "max": t["max"]} if t["provider"] == "knn" else {})}


SCRAPE_REFRESH_S = 120  # how stale the dashboard may be while an intake run is writing
WATCH_EVERY_S = 3  # the background app checks the data version this often
WATCH_RECENT_S = 600  # and rebuilds the results read within this long
# the dashboard's whole-database results that read no goal (by name, before any '|'): a goal edit leaves them
# current; every other result (the goal results, the list, Pipeline, sampling's goal-aware ones) is rebuilt with it
GOAL_FREE = {"places_by_key", "best_trips", "goal_inputs", "goal_input_rows", "located", "listings_by_place",
             "media_files", "nearest_sets", "place_rows", "current_checks", "website_status", "plan_readings",
             "agreement", "all_conflicts", "n_sample", "sample_queue", "own_site_queue", "duplicate_queue", "counts",
             "area_map", "knn_loo", "knn_loo_samples", "est_errors"}
GOAL_WAIT_S = 20  # a goal save answers once its list is rebuilt (goal_refresh), or after this long regardless
# results the background app keeps on disk (data/cache/<db name>/), served at once after a restart (a deploy) while
# the worker rebuilds them: the list, the header counts and queues, the Pipeline panel, the KNN status table, and
# every whole-database input of the drawer (so a click after a restart never waits for a rebuild)
PERSIST = ("places_body", "pipeline", "all_conflicts", "agreement", "n_sample", "own_site_queue", "duplicate_queue",
           "counts", "knn_status|", "places_by_key", "best_trips", "evaluate_all", "located", "listings_by_place",
           "media_files", "current_checks", "website_status", "plan_readings")
PERSIST_EVERY_S = 300  # a result is written to disk at most this often (the big ones are tens of MB)


def _code_version() -> str:
    """A hash of this package's source: a snapshot from other code is served only as out of date."""
    h = hashlib.sha1()
    for f in sorted(Path(__file__).resolve().parents[1].rglob("*.py")):
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


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
            # where those baths came from when the row had none of its own (plans.inherit_baths): lower trust
            "baths_from": next((x.get("baths_from") for x in f if x.get("baths") is not None), None),
            "sqft": [min(sqft), max(sqft)] if sqft else None,
            "price_month": [min(prices), max(prices)] if prices else None,
            # the fee-inclusive monthly total a source shows beside the base rent (the price): own site, or
            # Apartments.com / Zillow when no own-site price
            "total_month": (lambda t: [min(t), max(t)] if t else None)(
                [((l.get("data") or {}).get("extra") or {}).get("total_monthly") for l in basis
                 if ((l.get("data") or {}).get("extra") or {}).get("total_monthly")]),
            "price_per_bed": (min(prices) / max(f[0]["beds"], 1)) if prices and f[0].get("beds") is not None else None,
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
    # "list": place_keys (the places the list shows); "zone": every place in a walk zone; "candidates": places, in a
    # walk zone or not, that can still pass some goal apart from commute (filter.routing_candidates)
    scope: str = "list"
    place_keys: list[str] | None = None
    seed: int | None = None


class SampleRunIn(BaseModel):
    plan: dict[str, list[str]]  # "point|mode" -> place keys, from the dry run
    label: str = ""
    confirm_elements: int  # the dry run's element count: this run's cap
    confirm_left: int | None = None  # more than TYPED_CONFIRM_OVER elements: the elements left this month, typed


INTERRUPTED = "interrupted: the dashboard restarted mid-run"
# the KNN grid's columns (bike+train and transit: commute targets only)
KNN_MODES = ["drive", "bike", "walk", "bike_train_bike", "transit"]
TARGET_ONLY_MODES = ("bike_train_bike", "transit")


TYPED_CONFIRM_OVER = 20  # a sampling run above this many Route Matrix elements needs the counts typed in


class MergeIn(BaseModel):
    into: str  # the place this one's listings join


class DupAnswerIn(BaseModel):
    """One answer to a possible-duplicate pair: `into` one of the two places merges the other into it; no `into`
    records 'not the same building'."""
    a: str
    b: str
    into: str | None = None


class DupBatchIn(BaseModel):
    items: list[DupAnswerIn]


class LinkIn(BaseModel):
    source: str
    source_id: str
    place_key: str | None = None  # None = split this listing into its own place


def goal_view(r: dict[str, Any], commute: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A place's result for one goal as the list needs it. `commute`: the net-commute terms, [minutes or null,
    estimate, trip] per counted commute rule of the goal (filter.place_commute_terms), combined on the card; `trip`
    (rules.commute_trips) is what the card's hover lists, so the list and the number are the same terms.
    `commute_off`: the trips of must-haves unticked from the net commute, listed on hover as not counted."""
    return {"commute": [[d["minutes"], d["est"], d] for d in commute or [] if not d.get("off")],
            "commute_off": [d for d in commute or [] if d.get("off")], "status": r["status"], "nice_score": r["nice_score"],
            "reasons": [t for s, t, *_ in r["reasons"] if s != "pass"],
            "fails": r["fails"],  # the attribute of each reason, for red/amber facts
            "unit_key": r["unit_key"], "source": r["source"], "source_id": r["source_id"],
            **({"why": r["why"]} if r.get("why") else {}),  # no_listings: no_price / unavailable
            # the card's price for this goal: the cheapest offer of the best status (filter.goal_offer)
            **({"offer": r["offer"]} if r.get("offer") else {}),
            # a goal with a parking_cost rule: what its spaces add a month at the deciding offer (rules.parking_cost)
            **({"parking_cost": {k: r["parking_cost"].get(k) for k in ("usd", "spots", "none", "why", "ev")}}
               if r.get("parking_cost") else {})}


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


class _ThreadConn:
    """One SQLite connection per thread, looked up on each use. Request threads and the recompute worker never
    share a connection, so a long whole-database read in one never waits on, or makes wait, another (WAL: readers
    and the one writer run side by side; a write waits only for another write, store.connect's timeout)."""

    def __init__(self, path: str) -> None:
        self._path, self._local = path, threading.local()

    def _conn(self) -> Any:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = connect(self._path)
        return c

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn(), name)


def create_app(db_path: str | Path | None = None, redact: dict[str, str] | None = None,
               background: bool = False) -> FastAPI:
    """`redact` (real -> shown) makes a read-only view for screenshots: every API response has the strings
    swapped, longest first, and every write is refused. `background` (the served dashboard, `aptf-web`): whole-
    database results are rebuilt by one background worker and requests answer from the last snapshot meanwhile
    (stale-while-revalidate, flagged `X-Updating`); without it (tests, scripts) a request waits for a fresh result."""
    db_path = str(db_path or settings.DB_PATH)
    conn: Any = _ThreadConn(db_path)
    init_db(conn)
    # only for short check-then-write sequences and the job state: never held around a whole-database computation
    lock = threading.RLock()
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

    @app.middleware("http")
    async def snapshot_flag(request: Request, call_next):  # type: ignore[no-untyped-def]
        """X-Updating: the whole-database results this answer read from an out-of-date snapshot (being rebuilt);
        X-Snapshot-Age: seconds since the oldest of them was computed. The page fetches again a little later."""
        flag: list = []
        token = updating.set(flag)
        try:
            resp = await call_next(request)
        finally:
            updating.reset(token)
        if flag:
            resp.headers["X-Updating"] = ",".join(sorted({n for n, _ in flag}))
            resp.headers["X-Snapshot-Age"] = str(round(time.monotonic() - min(t for _, t in flag)))
        return resp

    # gzip JSON, the page and scripts (the place list is ~18 MB of JSON, ~1.7 MB gzipped). Added after the redact
    # middleware so it wraps it: redact still reads plain text. Photos are already compressed: left alone.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5,
                       exclude_content_types=("text/event-stream", "image/jpeg", "image/png", "image/webp",
                                              "image/gif", "image/svg+xml", "application/pdf"))

    # name -> (edits, scrapes, monotonic time computed, value, fn): the last snapshot of each whole-database result
    memo: dict[str, tuple[Any, ...]] = {}
    memo_lock = threading.Lock()  # guards memo, pending and last_read only (never held while computing)
    pending: dict[str, Future] = {}
    last_read: dict[str, float] = {}
    # the one worker's queue: (priority, order, name, fn, future); 0 = a request waits for it (a result with no
    # snapshot yet), 1 = a rebuild nobody waits for
    jobs: queue.PriorityQueue = queue.PriorityQueue()
    order = itertools.count()
    persisted: dict[str, float] = {}
    # a goal edit's own lane (goal_refresh): re-judging the goals must not wait behind a full rebuild on the worker
    goal_lane = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aptf-goals")
    in_worker = threading.local()
    updating: ContextVar[list | None] = ContextVar("updating", default=None)

    def raw_version() -> tuple[tuple, tuple, Any]:
        """(edits, scrapes, goals): what the pages show changes with a new row in any log or an edited config file.
        Edits (links, checks, annotations, config) are the user's own and must show at once; scrapes (observations,
        trips, runs) arrive in bursts while intake runs; goals (the goal editor's annotation rows) change only the
        goal-dependent results (GOAL_FREE). Cheap (max rowid per table, file mtimes)."""
        # identity's rule links arrive with every intake run (130 a minute seen live), so only the user's own
        # links count as edits; rule links refresh with the scrapes
        row = conn.execute(
            "SELECT (SELECT MAX(id) FROM links WHERE author = 'user'), (SELECT MAX(id) FROM checks), "
            "(SELECT MAX(id) FROM annotations WHERE entity_type <> 'goal'), "
            "(SELECT MAX(id) FROM links WHERE author <> 'user'), "
            "(SELECT MAX(id) FROM observations), (SELECT MAX(id) FROM trips), (SELECT MAX(id) FROM runs), "
            "(SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1), (SELECT MAX(id) FROM media), "
            "(SELECT MAX(id) FROM geocodes), "  # a geocoded address can move a place (places view)
            "(SELECT MAX(id) FROM annotations WHERE entity_type = 'goal')").fetchone()
        cfg = tuple(f.stat().st_mtime_ns for f in sorted(settings.CONFIG_DIR.rglob("*")) if f.is_file())
        return tuple(row[:3]) + cfg, tuple(row[3:10]), row[10]

    def version_of(name: str, raw: tuple[tuple, tuple, Any]) -> tuple[tuple, tuple]:
        """(edits, scrapes) of one result: a goal-dependent one's edits end with ("goal", the goal version)."""
        edits, scrapes, goals = raw
        return (edits if name.split("|", 1)[0] in GOAL_FREE else (*edits, ("goal", goals))), scrapes

    def data_version(name: str) -> tuple[tuple, tuple]:
        return version_of(name, raw_version())

    def goal_tagged(edits: tuple | None) -> bool:
        return bool(edits) and isinstance(edits[-1], tuple) and edits[-1][:1] == ("goal",)

    def goal_v(edits: tuple | None) -> int:
        """The goal version a goal-dependent snapshot was built at (-1: none, unknown or goal-free)."""
        return edits[-1][1] if goal_tagged(edits) and edits[-1][1] is not None else -1

    def fresh(hit: tuple | None, version: tuple[tuple, tuple]) -> bool:
        """A snapshot is current while no user edit came after it; new scraped rows make it out of date at most
        every SCRAPE_REFRESH_S seconds, so a running intake (a write every few seconds) does not rebuild every
        whole-database result after each write."""
        return bool(hit) and hit[0] == version[0] and (hit[1] == version[1]
                                                       or time.monotonic() - hit[2] < SCRAPE_REFRESH_S)

    snap_dir = (Path(db_path).parent / "cache" / Path(db_path).stem) if background and redact is None else None
    code_version = _code_version() if snap_dir is not None else ""

    def persist(name: str, version: tuple[tuple, tuple], val: Any) -> None:
        """Write a PERSIST result to disk (atomically: a temp file renamed), for the next start."""
        if snap_dir is None or not name.startswith(PERSIST) or time.monotonic() - persisted.get(name, -1e9) < PERSIST_EVERY_S:
            return
        persisted[name] = time.monotonic()
        try:
            snap_dir.mkdir(parents=True, exist_ok=True)
            f = snap_dir / (hashlib.sha1(name.encode()).hexdigest()[:16] + ".pkl")
            tmp = f.with_suffix(".tmp")
            tmp.write_bytes(pickle.dumps((code_version, name, version, time.time(), val)))
            tmp.replace(f)
        except Exception:  # noqa: BLE001  (a full disk or the like: the snapshot stays in memory only)
            pass

    def load_persisted() -> None:
        """Last run's snapshots: with the same code they keep their data version (current if nothing changed since),
        from other code they are out of date; either way the worker rebuilds what is out of date and requests are
        answered from them meanwhile, flagged X-Updating."""
        if snap_dir is None or not snap_dir.is_dir():
            return
        for f in snap_dir.glob("*.pkl"):
            try:
                code, name, version, wall, val = pickle.loads(f.read_bytes())
            except Exception:  # noqa: BLE001  (a truncated or old-format file: ignored)
                continue
            t = time.monotonic() - max(0.0, time.time() - wall)
            memo[name] = (*(version if code == code_version else (None, None)), t, val, None)

    def store(name: str, version: tuple[tuple, tuple], t: float, val: Any, fn) -> None:
        """Keep a snapshot (and persist it), unless one built against a newer goal version is already there (a
        full rebuild that started before a goal save must not put the old goals' results back over
        goal_refresh's)."""
        with memo_lock:
            old = memo.get(name)
            if old is not None and goal_v(old[0]) > goal_v(version[0]):
                return
            memo[name] = (*version, t, val, fn)
        persist(name, version, val)

    def compute(name: str, fn) -> Any:
        """fn() over the data version read first, stored as the snapshot (unless already current)."""
        version = data_version(name)
        with memo_lock:
            hit = memo.get(name)
        if fresh(hit, version):
            return hit[3]
        val = fn()
        store(name, version, time.monotonic(), val, fn)
        return val

    def work() -> None:
        """The one background worker: rebuilds queued results, the ones a request waits for first."""
        in_worker.on = True
        while True:
            _, _, name, fn, f = jobs.get()
            if f.done():  # queued twice (moved ahead for a waiting request): already built
                continue
            try:
                f.set_result(compute(name, fn))
            except BaseException as e:  # noqa: BLE001  (handed to whoever waits; the next read schedules again)
                f.set_exception(e)
            finally:
                with memo_lock:
                    if pending.get(name) is f:
                        pending.pop(name)

    def schedule(name: str, fn, urgent: bool = False) -> Future:
        """Queue a rebuild of `name` on the worker, once (a rebuild already queued is reused; `urgent`, for a request
        that waits for it, moves it ahead of the rebuilds nobody waits for)."""
        with memo_lock:
            f = pending.get(name)
            if f is None:
                f = pending[name] = Future()
                jobs.put((0 if urgent else 1, next(order), name, fn, f))
            elif urgent:
                jobs.put((0, next(order), name, fn, f))
            return f

    def cached(name: str, fn):
        """A whole-database result, reused while nothing changed. In the served dashboard (`background`) a request
        never computes one: an out-of-date snapshot is returned at once and rebuilt by the worker (the response says
        so in `X-Updating` and `X-Snapshot-Age`); only before the first snapshot does a request wait for it. The
        worker itself, and the app without `background`, compute in place."""
        version = data_version(name)
        worker_side = getattr(in_worker, "on", False)
        with memo_lock:
            hit = memo.get(name)
            if not worker_side:  # what the pages read (a result read only inside another is rebuilt with it)
                last_read[name] = time.monotonic()
        if fresh(hit, version):
            return hit[3]
        if not background or worker_side:
            return compute(name, fn)
        if hit is None:
            return schedule(name, fn, urgent=True).result()
        schedule(name, fn)
        flag = updating.get()
        if flag is not None:
            flag.append((name, hit[2]))
        return hit[3]

    def invalidate() -> None:
        """A user action the next reads must show (a route, new KNN cells, a sampling run): every snapshot is out
        of date and rebuilt (served meanwhile with X-Updating in the background app)."""
        with memo_lock:
            for k, v in list(memo.items()):
                memo[k] = (None, None, *v[2:])

    def all_places() -> dict[str, dict[str, Any]]:
        """The places view by key, read once per data version (the view is computed over everything)."""
        return cached("places_by_key", lambda: {r["place_key"]: dict(r) for r in conn.execute("SELECT * FROM places")})

    def all_trips() -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
        """best_trips (KNN included) once per data version, shared by every consumer below: goal results, the
        list, the drawer, sampling. Read only."""
        return cached("best_trips", lambda: best_trips(conn, located()))

    def results() -> dict[str, Any]:
        """Every goal judged over the cached goal_inputs (evaluate_all's work, reusing the cached trips and
        inputs: a goal edit re-judges the goals without reading the database again)."""
        return cached("evaluate_all", judge_all)

    def judge_all() -> dict[str, Any]:
        inp = inputs()
        with memo_lock:
            e = memo.get("goal_inputs")
        return judge(inp, e[2] if e and e[3] is inp else None, effective().goals)

    def judge(inp: dict[str, Any], basis: float | None, goal_defs: dict[str, Goal],
              prev: dict[str, Any] | None = None) -> dict[str, Any]:
        """{listings, places} per goal (evaluate_goal over `inp`), plus what was judged: `basis` (which goal_inputs
        snapshot: its time computed) and each goal's definition. A goal `prev` judged on the same snapshot with the
        same definition is reused (a goal edit re-judges only the goal edited)."""
        out: dict[str, Any] = {"listings": {}, "places": {}, "basis": basis, "defs": {}}
        for gid, goal in goal_defs.items():
            d = out["defs"][gid] = json.dumps(goal.model_dump(), sort_keys=True)
            if prev and basis is not None and prev.get("basis") == basis and prev["defs"].get(gid) == d:
                out["listings"][gid], out["places"][gid] = prev["listings"][gid], prev["places"][gid]
            else:
                out["listings"][gid], out["places"][gid] = evaluate_goal(inp, goal)
        return out

    def inputs() -> dict[str, Any]:
        """goal_inputs over the cached trips, for the draft preview and Verify's goal relevance."""
        return cached("goal_inputs", lambda: goal_inputs(conn, trips=all_trips()))

    def located() -> dict[str, tuple[float, float]]:
        """place_key -> (lat, lon) of located places (sampling.located), from the cached places view."""
        return cached("located", lambda: {pk: (p["lat"], p["lon"]) for pk, p in all_places().items()
                                          if p["lat"] is not None})

    def listings_by_place() -> dict[str, list[dict[str, Any]]]:
        """The listings view's placed rows grouped by place, read once per data version."""
        return cached("listings_by_place", lambda: _group_listings(conn))

    def media_files() -> dict[str, dict[str, Any]]:
        """url -> our stored copy, read once per data version."""
        from ..media import url_files

        return cached("media_files", lambda: url_files(conn))

    def q(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def latest_annotations(entity_type: str, key: str | None = None) -> dict[str, dict[str, dict[str, Any]]]:
        sql = "SELECT * FROM annotations WHERE entity_type = ?" + (" AND entity_key = ?" if key else "")
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for r in q(sql + " ORDER BY id", (entity_type, key) if key else (entity_type,)):
            prev = out.setdefault(r["entity_key"], {}).get(r["attribute"])
            if r["rating"] is None and prev:  # the latest rating survives a later comment (filter.place_annotations)
                r["rating"] = prev["rating"]
            out[r["entity_key"]][r["attribute"]] = r
        return out

    def effective() -> Any:
        return goalstore.effective_goals(conn)

    # ------------------------------------------------------------------ read API
    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        goals = effective()
        points = load_points()
        return {
            "db": Path(db_path).name,
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
    def places(request: Request) -> Response:
        # the whole list is built once per data version: every input is (goal results, trips, the places and
        # listings views, annotations, goal edits), so a reload or a goal switch reuses it; it is kept as JSON
        # bytes, gzipped too, so a request only sends them (encoding 18 MB took most of a warm request)
        body, gz = cached("places_body", encode_places)
        if gz is not None and "gzip" in request.headers.get("accept-encoding", ""):
            return Response(gz, media_type="application/json",
                            headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        return Response(body, media_type="application/json")

    def encode_places() -> tuple[bytes, bytes | None]:
        return encode_list(cached("places_list", build_places))

    def encode_list(lst: list[dict[str, Any]]) -> tuple[bytes, bytes | None]:
        body = json.dumps(lst, separators=(",", ":"), ensure_ascii=False).encode()
        # the redacted view rewrites the plain text in its middleware: no pre-gzipped copy there
        return body, (gzip.compress(body, 5) if redact is None else None)

    def build_places() -> list[dict[str, Any]]:
        return with_goals(cached("place_rows", place_rows), results(), all_trips(), effective().goals)

    def with_goals(rows: list[dict[str, Any]], res: dict[str, Any], trips: dict[str, Any],
                   goal_defs: dict[str, Goal]) -> list[dict[str, Any]]:
        """The list: each place's goal-free row (place_rows) with its goal results and the commute times shown."""
        # `minutes` feeds the cards' commute rows (app.js limitValue): commute targets one by one, sets through
        # `nearest`. Times to other sets' single points (stations, bus stops, groceries: about a fifth of all keys)
        # are left out unless a goal's commute rule names that point.
        shown = {r.get("target") for g in goal_defs.values() for r in [*g.rules, *g.nice] if r.get("type") == "commute_any"}
        out = []
        for b in rows:
            pk = b["place_key"]
            goals = {}
            for gid, pres in res["places"].items():
                r = pres.get(pk)
                if r:
                    goals[gid] = goal_view(r, place_commute_terms(goal_defs[gid], trips, pk) if gid in goal_defs else None)
            out.append({**b, "goals": goals,
                        "minutes": {f"{pt}|{m}": _tv(t) for (pt, m), t in trips.get(pk, {}).items()
                                    if pt.startswith("commute_targets:") or pt in shown}})
        return out

    def place_rows() -> list[dict[str, Any]]:
        """Every place's list row apart from its goals (with_goals): reads no goal, so a goal edit reuses it."""
        trips = all_trips()
        by_place = listings_by_place()
        anns = latest_annotations("place")
        # per place, the fastest trip to any point of each set and mode ("walk to the nearest Google bus stop"),
        # for the list's requirement rows; commute targets are in `minutes` one by one
        nearest_all = cached("nearest_sets", lambda: {
            pk: {f"{s}|{m}": {**_tv(t), "point": t["point_id"].split(":", 1)[1]}
                 for (s, m), t in nearest_by_set(tr).items() if s != "commute_targets"}
            for pk, tr in trips.items()})
        files = media_files()
        out = []
        for p in all_places().values():
            pk = p["place_key"]
            rows = by_place.get(pk, [])
            title = next((x["title"] for x in rows if x.get("title")), None)
            parsed = [{**x, "data": json.loads(x["data"])} for x in rows]
            cover = gather_photos(parsed, limit=1, files=files)
            a = anns.get(pk, {})
            out.append({
                **p,
                "label": place_label(p) or title or pk,
                "title": title,
                "photo": cover[0]["url"] if cover else None,  # map hover
                "beds": sorted({x["beds"] for x in rows if x["beds"] is not None}),
                "baths": sorted({x["baths"] for x in rows if x["baths"] is not None}),
                # the largest unit's sqft any listing here states: the sqft sort under All (a goal sorts by its offer's)
                "max_sqft": max((x["sqft"] for x in rows if x.get("sqft")), default=None),
                "nearest": nearest_all.get(pk, {}),
                # a limit on who may rent that any listing here states (income, senior, ...): the card's badge
                "restriction": place_restriction(parsed, a),
                # parking as the most informative listing here states it (filter.place_parking): the card's line
                "parking": place_parking(parsed),
                # how many units the building has, with its evidence (filter.building): the card's 🏢 / 🏠 line
                "building_units": place_building_units(parsed),
                "rating": (a.get("overall") or {}).get("rating"),
                "user_status": (a.get("status") or {}).get("comment"),
            })
        return out

    def goal_refresh() -> bool:
        """After a goal edit (goal_lane): the goal results, the list and its JSON rebuilt from the last snapshots
        of their goal-free inputs (GOAL_FREE: the trips with KNN, goal_inputs, place_rows), taken as they are even
        when new scrapes made them out of date, so it takes seconds, never a full rebuild. Each new snapshot keeps
        its predecessor's version with the new goal version: what it was out of date on stays out of date and the
        worker rebuilds it as usual. False when a snapshot is missing (the worker then builds the list)."""
        names = ("goal_inputs", "place_rows", "evaluate_all", "places_list", "places_body")
        with memo_lock:
            snap = {n: memo.get(n) for n in names}
        # the list loaded from disk at a start has no places_list beside it: it takes the list's JSON version
        snap["places_list"] = snap["places_list"] or snap["places_body"]
        if any(v is None or (v[0] is not None and n not in GOAL_FREE and not goal_tagged(v[0]))
               for n, v in snap.items()):
            return False
        goals = raw_version()[2]
        goal_defs = effective().goals
        inp = snap["goal_inputs"][3]
        res = judge(inp, snap["goal_inputs"][2], goal_defs, snap["evaluate_all"][3])
        lst = with_goals(snap["place_rows"][3], res, inp["trips"], goal_defs)
        for name, val, fn in (("evaluate_all", res, judge_all), ("places_list", lst, build_places),
                              ("places_body", encode_list(lst), encode_places)):
            old = snap[name]
            store(name, (None if old[0] is None else (*old[0][:-1], ("goal", goals)), old[1]), old[2], val, fn)
        return True

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
        by_place = listings_by_place()
        listings = by_place.get(place_key, [])
        parsed = [{**x, "data": json.loads(x["data"])} for x in listings]
        place["restriction"] = place_restriction(parsed, latest_annotations("place", place_key).get(place_key, {}))
        place["parking"] = place_parking(parsed)
        place["building_units"] = place_building_units(parsed)
        res = results()
        trips = all_trips().get(place_key, {})
        out_listings = []
        # the current_checks view joins latest_obs over everything: read it once per data version, not per listing
        all_checks = cached("current_checks", lambda: verify.current_checks(conn))
        for l in listings:
            checks = dict(all_checks.get((l["source"], l["source_id"]), {}))
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
        pstatus = pipeline.place_status(conn, place_key, web=web, by_place=by_place)
        # an own-site plan row without baths: the same plan's baths on another source, else its drawing's reading
        from ..planread import readings as plan_readings

        inherit_baths([{**l, "place_key": place_key} for l in out_listings],
                      cached("plan_readings", lambda: plan_readings(conn)), fields=lambda r: r["fields"])
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
            "related": related_places(place_key),
        }

    def related_places(place_key: str) -> list[dict[str, Any]]:
        """The building and its third-party stay places, from the cached places view: as `place_key = base OR
        place_key LIKE 'base~%'` (LIKE: '_' any one character, ASCII case-insensitive), without this place."""
        base = place_key.split("~", 1)[0]
        like = re.compile("".join("." if ch == "_" else ".*" if ch == "%" else re.escape(ch)
                                  for ch in base + "~%"), re.IGNORECASE | re.DOTALL)
        cols = ("place_key", "property_name", "operator", "address", "n_active", "min_lease_month", "min_stay_month")
        out = []
        for pk, p in all_places().items():
            if pk != place_key and (pk == base or like.fullmatch(pk)):
                r = {k: p[k] for k in cols}
                out.append(dict(r, label=place_label(r)))
        return out

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
        acc = verify.accuracy(conn)
        agree = cached("agreement", lambda: verify.agreement(conn))
        conflicts = cached("all_conflicts", lambda: verify.all_conflicts(conn))
        n_pending = sum(c["pending_render"] for c in conflicts)
        n_sample = cached("n_sample", lambda: len(verify.sample_queue(conn)))
        n_own = len(cached("own_site_queue", lambda: verify.own_site_queue(conn)))
        n_dup = len(duplicates())
        from ..trips import google_usage

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
        return {**pipeline_data(), "apify": apify_credit()}

    def pipeline_data() -> dict[str, Any]:
        return cached("pipeline", lambda: pipeline.overview(
            conn, places=list(all_places().values()), res=results(),
            conflicts=cached("all_conflicts", lambda: verify.all_conflicts(conn)),
            own_queue=cached("own_site_queue", lambda: verify.own_site_queue(conn)), dup_queue=duplicates(),
            web=cached("website_status", lambda: pipeline.website_status(conn))))

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
        if not goal and not zone:
            return (cached("all_conflicts", lambda: verify.all_conflicts(conn)),
                    {"irrelevant": 0, "settled": 0, "same": 0, "zone": 0})

        def compute() -> tuple[list[dict[str, Any]], dict[str, int]]:
            # every input read here, never captured from the request: the worker reruns this after data changes
            keep = cached("all_conflicts", lambda: verify.all_conflicts(conn))
            hidden = {"irrelevant": 0, "settled": 0, "same": 0, "zone": 0}
            if goal:
                g = effective().goals.get(goal)
                if g is None:
                    raise HTTPException(400, f"unknown goal {goal!r}")
                lres = results()["listings"].get(goal, {})
                rows = cached("goal_input_rows", lambda: {(r["source"], r["source_id"]): r for r in
                                                          inputs()["listings"]})
                low = inputs()["low"]
                out = []
                for c in keep:
                    v = verify.goal_relevance(c, g.rules, lres, rows, low, people=g.people)
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

        items = duplicates()
        allp = all_places()
        page = [{**e, "places": [{**p, "label": place_label(allp.get(p["place_key"]) or p) or p["place_key"]}
                                 for p in e["places"]]} for e in items[max(0, skip):max(0, skip) + n]]
        return {"total": len(items), "skip": skip, "n": n, "items": page, "labels": verify.DUP_REASONS,
                "by_reason": dict(Counter(r["kind"] for e in items for r in e["reasons"]))}

    @app.get("/api/verify/next")
    def verify_next(seed: int = 0, skip: int = 0) -> dict[str, Any]:
        """The sample, one item at a time. Conflicts are paged by /api/verify/batch, possible duplicates and
        buildings whose own site gave nothing usable are lists of their own (/api/verify/duplicates, /own_sites)."""
        items = cached(f"sample_queue|{seed}", lambda: verify.sample_queue(conn, seed))
        if skip >= len(items):
            return {"done": True, "remaining": 0}
        item = items[skip]
        out: dict[str, Any] = {"done": False, "remaining": len(items), "item": item}
        if item["kind"] == "listing":
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
        comment = a.comment
        if a.entity_type == "place" and a.attribute.strip().lower() == "restriction":
            # who may rent: the goals' `equals restriction` rule reads this comment, so it must be one of its values
            # (Verify's buttons and the drawer's select send them; a note typed in the drawer is checked here)
            comment = (a.comment or "").strip().lower()
            if comment not in EQUALS_FIELDS["restriction"]:
                raise HTTPException(400, "restriction must be one of: " + ", ".join(EQUALS_FIELDS["restriction"]))
        with lock:
            aid = append_annotation(conn, AUTHOR, a.entity_type, a.entity_key, a.attribute.strip().lower(),
                                    goal_id=a.goal_id, rating=a.rating, comment=comment)
        return {"id": aid}

    # ------------------------------------------------------------------ goals (filter.goals): goals.toml is the
    # seed; an edit, a new goal or a delete is an annotations row (goal / <id> / definition), latest wins
    def goal_refs() -> dict[str, Any]:
        pts = load_points()
        return {"modes": MODES, "points": {p.id for v in pts.values() for p in v},
                "sets": set(pts) | set(load_point_sets())}

    def checked_goal(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return goalstore.validate_goal(body, **goal_refs())
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    def save_goal(gid: str, d: dict[str, Any]) -> dict[str, Any]:
        with lock:
            aid = append_annotation(conn, AUTHOR, "goal", gid, "definition", comment=json.dumps(d, sort_keys=True))
        if background:
            # the list with the new goals in seconds (goal_refresh), on its own lane so it never waits behind a
            # full rebuild; the answer waits for it, so the page's next /api/places is already fresh
            done = False
            try:
                done = goal_lane.submit(goal_refresh).result(timeout=GOAL_WAIT_S)
            except Exception:  # noqa: BLE001  (slow or failed: the worker rebuilds the list as usual)
                pass
            if not done:
                schedule("places_body", encode_places)
        return {"id": aid, "goal": gid}

    @app.get("/api/goals")
    def list_goals() -> dict[str, Any]:
        seed = load_goals().goals
        edits = goalstore.goal_edits(conn)
        refs = goal_refs()
        return {
            "goals": [{"id": g, "label": v.label, "rules": v.rules, "nice": v.nice, "people": v.people,
                       "origin": "new" if g not in seed else "edited" if g in edits else "goals.toml"}
                      for g, v in effective().goals.items()],
            "deleted": sorted(g for g, d in edits.items() if d is None),
            # what the editor offers: rule types (label, required and optional keys), fields, presets
            "types": {t: {"label": lab, "keys": req, "optional": opt} for t, (lab, req, opt) in RULE_TYPES.items()},
            "aggs": AGGS, "numeric_fields": NUMERIC_FIELDS, "equals_fields": EQUALS_FIELDS, "known_fields": KNOWN_FIELDS,
            "amenities": {k: {"match": w, "exclude": x} for k, (w, x) in AMENITY_FLAGS.items()},
            "operators": [o["name"] for o in load_operators()],  # a not_operator rule's choices
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
        inp = inputs()
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

    def merge_links(place_key: str, into: str) -> int:
        """Append the user links that move every listing of place_key to `into`, each keeping its own unit
        ('<this place>/<unit>' -> '<other place>/<unit>'); no commit. Raises HTTPException when it cannot."""
        if into == place_key:
            raise HTTPException(400, "a place cannot merge into itself")
        if not q("SELECT 1 FROM places WHERE place_key = ?", (into,)):
            raise HTTPException(404, "no such place to merge into")
        rows = q("SELECT source, source_id, unit_key FROM listings WHERE place_key = ?", (place_key,))
        if not rows:
            raise HTTPException(404, "no listings at this place")
        with lock:
            for r in rows:
                uk = r["unit_key"] or place_key
                unit = into + uk[len(place_key):] if uk.startswith(place_key) else into
                append_link(conn, AUTHOR, r["source"], r["source_id"], into, unit,
                            f"manual merge of {place_key}", commit=False)
        return len(rows)

    @app.post("/api/places/{place_key}/merge")
    def merge_place(place_key: str, body: MergeIn) -> dict[str, Any]:
        """Every listing of this place becomes the other place's, as user links (append-only; each listing keeps
        its own unit: '<this place>/<unit>' -> '<other place>/<unit>'). Undo a listing with 'split'."""
        with lock:
            n = merge_links(place_key, body.into)
            conn.commit()
        return {"merged": n, "into": body.into}

    @app.post("/api/verify/duplicates/batch")
    def answer_duplicates(body: DupBatchIn) -> dict[str, Any]:
        """Answer several possible-duplicate pairs in order, in one transaction: each item a merge (the same links
        as POST /api/places/{key}/merge) or 'not the same building' (the same annotation as the single button:
        place_pair / pair_key / same_building = no). Later items see earlier ones (a place merged away earlier
        cannot merge again). An item that cannot be done writes nothing and comes back with its error; the rest
        commit together."""
        out = []
        with lock:
            try:
                for it in body.items:
                    pair = verify.pair_key(it.a, it.b)
                    try:
                        if it.a == it.b:
                            raise HTTPException(400, "a pair needs two places")
                        if it.into is None:
                            append_annotation(conn, AUTHOR, "place_pair", pair, "same_building", comment="no",
                                              commit=False)
                            out.append({"pair": pair, "ok": True, "same_building": "no"})
                        elif it.into in (it.a, it.b):
                            src = it.b if it.into == it.a else it.a
                            out.append({"pair": pair, "ok": True, "merged": merge_links(src, it.into),
                                        "from": src, "into": it.into})
                        else:
                            raise HTTPException(400, "into must be one of the pair")
                    except HTTPException as e:
                        out.append({"pair": pair, "ok": False, "error": e.detail})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"results": out, "done": sum(r["ok"] for r in out), "failed": sum(not r["ok"] for r in out)}

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
            n = compute_for_place(conn, commute, rows[0], on_demand=True)
        except SpendCapReached as e:
            raise HTTPException(429, str(e))
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        finally:
            # the user's own routing shows at once: trips count as scraped rows, which the cache may reuse for
            # SCRAPE_REFRESH_S, so the drawer refreshed with the old estimates
            with lock:
                invalidate()
        if background and n:
            # the user's own route shows in the drawer the page reloads next: this request (only it, holding no lock)
            # waits for the worker to rebuild the trips and goal results; every other request keeps answering
            schedule("best_trips", lambda: best_trips(conn, located()), urgent=True).result()
            schedule("evaluate_all", judge_all, urgent=True).result()
        return {"new_trips": n, "provider": provider, "api_calls": getattr(source, "calls", 0)}

    # ------------------------------------------------------------------ the Pipeline panel's background job
    # One Google sampling run at a time (POST /api/sample/run, below) in a thread of this process; GET
    # /api/compute/status reports it (the page polls it, scripts/deploy.sh waits on it) and /api/compute/log lists runs.
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

    def _compute_places(body: SampleIn) -> list[str]:
        located = {r["place_key"]: (r["lat"], r["lon"]) for r in all_places().values() if r["lat"] is not None}
        if body.scope == "zone":
            keys = [pk for pk, (la, lo) in located.items() if in_walk_zone(la, lo)]
        elif body.scope == "candidates":
            from ..filter import routing_candidates

            res = results()
            cands = routing_candidates(conn, res=res)
            keys = [pk for pk in cands if pk in located]  # in a walk zone or not: stays are mostly outside
        else:
            keys = body.place_keys or []
        # a stay place (`<building>~<operator>`) reads its building's trips: route the building when it is a place
        out = {(pk.split("~", 1)[0] if pk.split("~", 1)[0] in located else pk) for pk in keys}
        return sorted(pk for pk in out if pk in located)

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
            if p not in ids or m not in KNN_MODES or (m in TARGET_ONLY_MODES and p.startswith("set:")):
                raise HTTPException(400, f"no such cell {p} x {m}")
        with lock:
            append_annotation(conn, AUTHOR, *CELLS_KEY, comment=json.dumps(sorted(set(map(tuple, body.cells)))))
            invalidate()
        return {"cells": sorted(set(map(tuple, body.cells)))}

    def cell_accuracy(pairs: list[tuple[str, str]], keys: list[str]) -> dict[str, Any]:
        """Per checked cell: how the scope's places get their time (own Google route / KNN range / estimate, %) and
        the KNN accuracy by leave-one-out over every place with its own Google route (scope-free, cached per cell)."""
        from .. import sampling

        trips = all_trips()
        loc = located()
        return {"|".join(p): {**sampling.cell_shown(trips, p, keys),
                              "loo": cached(f"knn_loo|{'|'.join(p)}",
                                            lambda p=p: sampling.cell_loo(all_trips(), p, located()))}
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

        return cached("candidate_goals", lambda: sampling.candidate_goals(conn, res=results(), loc=located()))

    def hinges() -> dict[str, Any]:
        """place_key -> goal -> the commute pairs its status waits on (sampling.hinges), cached."""
        from .. import sampling

        return cached("hinges", lambda: sampling.hinges(results(), all_trips(), goalstore.effective_goals(conn),
                                                         located()))

    def scope_status(keys: list[str], pairs: list[tuple[str, str]]) -> dict[str, Any]:
        """sampling.commute_status for one scope; each verdict's plan costs come from the same planners as the dry
        run (cover within knn.RADIUS_M, refine)."""
        from .. import sampling
        from ..filter.knn import RADIUS_M, loo_samples
        from ..trips import planned_legs

        trips = all_trips()
        loc = located()
        cfg = goalstore.effective_goals(conn)

        def cost(method: str, pair: tuple[str, str]) -> tuple[int, int]:
            if method == "cover":
                pks = sampling.cover_plan(trips, [pair], keys, loc, RADIUS_M)[pair]
            else:
                pks = sampling.refine_plan(trips, [pair], keys, cfg, candidates())[0][pair]
            return len(pks), (len(planned_legs(db_path, pks, pair, loc)) if pks else 0)

        return sampling.commute_status(
            trips, pairs, keys, hinges(),
            lambda p: cached(f"knn_loo_samples|{'|'.join(p)}",
                             lambda: loo_samples(all_trips(), located(), sampling.cell_keys(all_trips(), p))),
            lambda p: cached(f"est_errors|{'|'.join(p)}",
                             lambda: sampling.estimate_errors(conn, sampling.cell_keys(all_trips(), p))), cost)

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
        trips = all_trips()
        if body.method == "random":
            plan = sampling.random_plan(trips, pairs, keys, body.pct, body.seed)
            label = f"random {body.pct:g}%"
        elif body.method == "cover" and body.scope == "candidates":
            # per pair, only the candidates of a goal with a commute rule on it (a stay needs transit, a lease not)
            plan = sampling.goal_cover_plan(trips, pairs, keys, located(), goalstore.effective_goals(conn),
                                            candidates(), body.radius_m)
        elif body.method == "cover":
            plan = sampling.cover_plan(trips, pairs, keys, located(), body.radius_m)
            label = f"cover within {body.radius_m:g} m"
        elif body.method == "refine":
            plan, notes = sampling.refine_plan(trips, pairs, keys, goalstore.effective_goals(conn), candidates())
            label = "refine"
        else:
            raise HTTPException(400, "method must be cover, random or refine")
        summ = sampling.summary(conn, db_path, plan, keys, trips, located())
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
                invalidate()
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

    if background:
        load_persisted()
        threading.Thread(target=work, daemon=True, name="aptf-recompute").start()
        from ..planread import readings as plan_readings
        # first snapshots at start (the list, the header counts, the Pipeline panel), one queued task each, the list
        # first, so the first page load waits only for what it reads; then a watcher rebuilds, as soon as the data
        # changes, every result a page read in the last WATCH_RECENT_S seconds, so an edit shows up unasked
        for name, fn in (("places_body", encode_places),
                         ("current_checks", lambda: verify.current_checks(conn)),  # the drawer's other inputs
                         ("website_status", lambda: pipeline.website_status(conn)),
                         ("plan_readings", lambda: plan_readings(conn)),
                         ("all_conflicts", lambda: verify.all_conflicts(conn)),
                         ("agreement", lambda: verify.agreement(conn)),
                         ("n_sample", lambda: len(verify.sample_queue(conn))),
                         ("own_site_queue", lambda: verify.own_site_queue(conn)),
                         ("duplicate_queue", lambda: verify.duplicate_queue(conn, list(all_places().values()))),
                         ("pipeline", lambda: pipeline_data()),
                         # what a goal save and the draft preview judge over (goal_refresh), also after a restart
                         # that loaded the list from disk
                         ("goal_inputs", lambda: goal_inputs(conn, trips=all_trips())),
                         ("place_rows", place_rows)):
            schedule(name, fn)

        def points_signature() -> tuple:
            """The point files' mtimes (point_sets.toml and config/points/): what decides each place's nearest points."""
            files = [settings.CONFIG_DIR / "point_sets.toml", *sorted((settings.CONFIG_DIR / "points").glob("*"))]
            return tuple((f.name, f.stat().st_mtime_ns) for f in files if f.is_file())

        seen_points: list = [None]

        def fill_estimates() -> None:
            """At start and whenever a point file changes: straight-line estimates for the places whose current
            nearest points lack one (trips.estimate_missing; a stop added to a set), so the list shows the new
            nearest point without anyone recomputing by hand. Free; logged as a commute/estimate run when any."""
            sig = points_signature()
            if sig == seen_points[0]:
                return
            from ..trips import estimate_missing

            if estimate_missing(conn, db_path, after="point files changed")["new_trips"]:
                invalidate()
            seen_points[0] = sig

        def watch() -> None:
            while True:
                time.sleep(WATCH_EVERY_S)
                try:
                    fill_estimates()
                except Exception:  # noqa: BLE001  (a locked or busy database: try again next round)
                    pass
                try:
                    raw = raw_version()
                    now = time.monotonic()
                    with memo_lock:
                        todo = [(k, v[4]) for k, v in memo.items() if v[4] is not None
                                and now - last_read.get(k, 0) < WATCH_RECENT_S and not fresh(v, version_of(k, raw))]
                    todo.sort(key=lambda kv: kv[0] != "places_body")  # the list first: it is what an edit is for
                    for name, fn in todo:
                        schedule(name, fn)
                except Exception:  # noqa: BLE001  (a locked or busy database: try again next round)
                    pass

        threading.Thread(target=watch, daemon=True, name="aptf-watch").start()

    return app
