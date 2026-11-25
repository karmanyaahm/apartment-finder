"""Loaders for the human-edited files in config/. Read at startup; never written by code."""

from __future__ import annotations

import csv
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import settings


class Point(BaseModel):
    set: str
    name: str
    lat: float
    lon: float
    extra: dict[str, str] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.set}:{self.name}"


class PointSetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = None
    modes: list[str] = ["walk"]
    k: int = 3  # route to the k nearest points; 0 means every point in the set
    prefilter_km: float | None = None  # skip Google routing beyond this straight-line distance
    max_km: dict[str, float] = Field(default_factory=dict)  # per-mode straight-line limit for paid routing
    paid: bool = True  # False: this set only ever gets free straight-line estimates
    # point name -> modes Google routes automatically (`compute`, `--candidates`). Missing: every mode in
    # `modes`. `modes` itself is the full table, routed on demand (dashboard button, `--on-demand`).
    auto: dict[str, list[str]] | None = None


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    rules: list[dict[str, Any]] = []
    nice: list[dict[str, Any]] = []


class VerifyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_samples: int = 5
    min_accuracy: float = 0.8
    sample_fields: list[str] = ["price", "beds", "baths", "sqft", "available_from", "address"]
    min_price_per_bed: float = 1000  # a lease below this per bedroom (studio = 1) goes to Verify: per-room or misread?


class GoalsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goals: dict[str, Goal]
    verify: VerifyConfig = VerifyConfig()


class AreaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cities: list[str]
    bbox: list[float]  # south, west, north, east


def _toml(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_redactions(config_dir: Path | None = None) -> dict[str, str]:
    """config/redact.toml `[replace]`: real string -> what a redacted dashboard shows instead."""
    path = (config_dir or settings.CONFIG_DIR) / "redact.toml"
    return {str(k): str(v) for k, v in _toml(path).get("replace", {}).items()} if path.exists() else {}


def load_media(config_dir: Path | None = None) -> dict[str, Any]:
    """config/media.toml `[media]` (photo store, aptfinder.media): max_gb. Missing file: defaults."""
    path = (config_dir or settings.CONFIG_DIR) / "media.toml"
    return _toml(path).get("media", {}) if path.exists() else {}


def load_goals(config_dir: Path | None = None) -> GoalsConfig:
    return GoalsConfig.model_validate(_toml((config_dir or settings.CONFIG_DIR) / "goals.toml"))


def load_area(config_dir: Path | None = None) -> AreaConfig:
    return AreaConfig.model_validate(_toml((config_dir or settings.CONFIG_DIR) / "area.toml"))


def load_point_sets(config_dir: Path | None = None) -> dict[str, PointSetConfig]:
    raw = _toml((config_dir or settings.CONFIG_DIR) / "point_sets.toml")
    return {k: PointSetConfig.model_validate(v) for k, v in raw.items()}


def load_caltrain(config_dir: Path | None = None) -> dict[str, Any]:
    return _toml((config_dir or settings.CONFIG_DIR) / "caltrain.toml")


def load_points(config_dir: Path | None = None) -> dict[str, list[Point]]:
    """Every config/points/*.csv, keyed by set name. Columns set,name,lat,lon plus free columns."""
    out: dict[str, list[Point]] = {}
    for path in sorted(((config_dir or settings.CONFIG_DIR) / "points").glob("*.csv")):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if not row.get("name"):
                    continue
                base = {k: row.pop(k) for k in ("set", "name", "lat", "lon")}
                p = Point(set=base["set"] or path.stem, name=base["name"], lat=float(base["lat"]),
                          lon=float(base["lon"]), extra={k: v for k, v in row.items() if v})
                out.setdefault(p.set, []).append(p)
        out.setdefault(path.stem, [])
    return out


class Area(BaseModel):
    """One intake area. kind: zip (Zillow, Apartments.com, Craigslist), city (Furnished Finder),
    box (Airbnb: a station's walk-zone bounding box)."""
    kind: str
    id: str
    label: str
    city: str | None = None
    bbox: list[float] | None = None
    slug: str | None = None  # box: Airbnb place slug for the search URL (default Palo-Alto--CA)


CHECK_RETRY = {"blocked", "timeout", "error", "not_fetched", "empty"}  # a discovered site's page not fetched yet
# stopped by the free precheck (intake/precheck.py) before any Apify run: never retried by --retry-failed, checked
# again once older than PRECHECK_RECHECK_DAYS (or with render --site). Only a dead domain: a Cloudflare challenge to
# the plain GET does not stop Apify's browser (all 8 challenged sites rendered at HTTP 200 on 2026-11-25)
PRECHECK_SKIP = {"dead"}
PRECHECK_RECHECK_DAYS = 7
CHECK_LABEL = {"ok": "address shown", "rejected": "page does not show the street address",
               "blocked": "blocked", "timeout": "timed out", "error": "fetch failed", "not_fetched": "page not fetched",
               "empty": "page has no text", "dead": "site dead (DNS)"}


def check_outcome(c: dict[str, Any]) -> str:
    """Outcome of a `[[check]]` of a discovered site: `ok`, `rejected` (fetched, the address is not on the page;
    the only outcome that drops the site) or a retryable one (CHECK_RETRY). Checks written before outcomes were
    recorded are read from their reason ('page not fetched', 'page has no text')."""
    if c.get("outcome"):
        return c["outcome"]
    if c.get("ok"):
        return "ok"
    reason = c.get("reason") or ""
    return ("rejected" if reason.startswith(("page does not show", "landed on a contact")) else "empty" if reason == "page has no text"
            else "not_fetched")


def last_checks(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """site id -> its latest `[[check]]` (the file is append-only, so later rows are newer)."""
    return {c["id"]: c for c in raw.get("check", [])}


def latest_found(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """The `[[found]]` rows, the latest per site id (the file is append-only: a corrected URL is a new row with the
    same id), in the order the ids first appear."""
    return list({f["id"]: f for f in raw.get("found", [])}.values())


def prometheus_ids() -> dict[str, str]:
    """site id -> Prometheus community id, the cache `aptf-intake prometheus` keeps (data/prometheus_ids.json)."""
    import json

    try:
        return json.loads((settings.DATA_DIR / "prometheus_ids.json").read_text())
    except (OSError, ValueError):
        return {}


def site_key(url: str) -> str:
    """'host/path[?query]' of a site URL, the same-site rule's key: `discover.clean_url` (no fragment), host
    lowercased without www., path without its trailing slash, lowercased, and the query when there is one: a
    redirect link (`lease-apts.com/?...`) names its building only in the query. Stored site URLs have none."""
    from urllib.parse import urlsplit

    from .intake.discover import clean_url

    p = urlsplit((clean_url(url, keep_query=True)[0] or url).strip())
    return (p.netloc.lower().split(":")[0].removeprefix("www.") + p.path.rstrip("/").lower()
            + (f"?{p.query}" if p.query else ""))


def duplicate_sites(sites: dict[str, dict[str, Any]], ids: dict[str, str] | None = None) -> dict[str, str]:
    """{entry id: canonical id} for the entries of the site list that are the same site as an earlier one: the
    same `site_key`, or for Prometheus the same community id (`ids`). The canonical entry is the first in load
    order: hand-listed (direct_sites.toml) before discovered, each file in its own order."""
    owner: dict[tuple[str, str], str] = {}
    out: dict[str, str] = {}
    for sid, s in sites.items():
        keys = [("url", site_key(s["url"]))]
        if ids and s.get("platform") == "prometheus" and ids.get(sid):
            keys.append(("prometheus", str(ids[sid])))
        canon = next((owner[k] for k in keys if k in owner), None)
        if canon:
            out[sid] = canon
        for k in keys:
            owner.setdefault(k, canon or sid)
    return out


def load_sites(config_dir: Path | None = None, *, duplicates: bool = False) -> dict[str, dict[str, Any]]:
    """Community websites: hand-listed ones (config/direct_sites.toml) plus ones found by `aptf-intake
    discover-sites` (config/discovered_sites.toml, machine-appended). A discovered site is left out once a
    `[[check]]` rejected it (its latest check: the page was fetched and lacks the address, or is a contact/tour
    form; a page not fetched is retried), when it is or landed on a contact/tour form or an aggregator host
    (`discover.AGGREGATOR_HOSTS`, also for hand entries), or when a hand entry has
    the same id or place (unless you added it in Verify); it carries `verify_address`.

    An entry that is the same site as an earlier one (`duplicate_sites`: same URL, or same Prometheus community)
    is left out; `duplicates=True` keeps it, marked `duplicate_of` the canonical entry's id, for the readers that
    retire its rows (`intake.render.mark_duplicates`) and the pipeline's own-site list."""
    from .intake.discover import is_aggregator, is_form

    d = config_dir or settings.CONFIG_DIR
    out: dict[str, dict[str, Any]] = {}
    if (d / "direct_sites.toml").exists():
        out = {s["id"]: s for s in _toml(d / "direct_sites.toml").get("site", []) if not is_aggregator(s["url"])}
    if (d / "discovered_sites.toml").exists():
        raw = _toml(d / "discovered_sites.toml")
        # only a page that was fetched and lacks the address rejects a site; a page not fetched (blocked, timed
        # out, empty) is retried. The last check of a site wins.
        failed = {i for i, c in last_checks(raw).items() if check_outcome(c) == "rejected"}
        moved = {c["id"]: c for c in raw.get("check", []) if c.get("ok") and c.get("final_url")}
        places = {s.get("place_key") for s in out.values()} - {None}
        for s in latest_found(raw):
            # a URL you added in Verify is kept beside the place's other sites (a place can have several)
            same_place = s.get("place_key") in places and not str(s.get("found_by", "")).startswith("you")
            if s["id"] in failed or s["id"] in out or same_place:
                continue
            m = moved.get(s["id"])  # a redirect link that landed on the building's page: use where it landed
            landed = m["final_url"] if m else s["url"]
            if is_form(landed) or is_aggregator(landed) or is_aggregator(s["url"]):
                continue
            s = {**s, "url": m["final_url"], "platform": m.get("platform") or s.get("platform"),
                 "link": s["url"]} if m else s
            out[s["id"]] = {**s, "verify_address": True}
    dups = duplicate_sites(out, prometheus_ids())
    if not duplicates:
        return {sid: s for sid, s in out.items() if sid not in dups}
    return {sid: {**s, "duplicate_of": dups[sid]} if sid in dups else s for sid, s in out.items()}


def load_areas(config_dir: Path | None = None) -> list[Area]:
    """Every intake area; a source covers every area of its own kind (`Adapter.area_kind`)."""
    raw = _toml((config_dir or settings.CONFIG_DIR) / "areas.toml")
    out = [Area(kind="zip", id=z["code"], label=f"{z['code']} {z['city']}", city=z["city"]) for z in raw.get("zip", [])]
    out += [Area(kind="city", id=c["name"], label=c["name"], city=c["name"]) for c in raw.get("city", [])]
    out += [Area(kind="box", id=b["id"], label=f"{b['station']} station zone", bbox=b["bbox"], slug=b.get("slug"))
            for b in raw.get("box", [])]
    out += [Area(kind="site", id=d["id"], label=d.get("name", d["id"])) for d in load_sites(config_dir).values()]
    return out
