"""Common intake machinery: run an adapter, store raw output and observations, flag suspicious output."""

from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import identity, settings
from ..config import Area, load_areas
from ..store import COMMIT_EVERY, append_observation, finish_run, start_run
from ..store.keys import content_hash
from ..store.models import ObservationData
from .apify import ActorRun, account_remaining_usd, run_actor, run_record, settle

API_PRICE_CACHE: dict[str, dict[str, float]] = {}


@dataclass
class Parsed:
    source_id: str
    url: str | None
    data: ObservationData
    price: float | None = None
    price_basis: str | None = None
    query_checkin: str | None = None
    query_checkout: str | None = None

    def __post_init__(self) -> None:
        # a source's 0 means "no price given" (Craigslist's priceUsd 0, FurnishedFinder's "$0.00/month"):
        # unknown, never a $0 rent that sorts first and passes every budget
        if self.price is not None and self.price <= 0:
            self.price = None


def area_for(adapter: "Adapter", item: dict[str, Any], gids: list[str]) -> str | None:
    """The intake area an item counts for. area_of (when the adapter has one) decides even for single-area runs:
    an Airbnb box search returns listings outside the box, which must not be tagged with (or retired by) that box."""
    return adapter.area_of(item, gids) if adapter.area_of else (gids[0] if len(gids) == 1 else None)


@dataclass
class Adapter:
    name: str
    build_input: Callable[[dict[str, Any], list[Area]], dict[str, Any]]  # (source config, areas) -> actor input
    # (item, source config) -> one listing, several (a building's units), or None if unparseable
    parse: Callable[[dict[str, Any], dict[str, Any]], "Parsed | list[Parsed] | None"]
    area_kind: str = "zip"  # zip | city | box: the smallest area this source searches efficiently
    required: tuple[str, ...] = ("price", "lat")  # fields whose absence in many items is suspicious
    # batch=True: one actor run for all areas (worth it when the actor's start fee is high); then
    # area_of(item, area ids) must attribute each item to its area. Otherwise one actor run per area.
    batch: bool = False
    area_of: Callable[[dict[str, Any], list[str]], str | None] | None = None
    # an actor's "nothing found" marker item (e.g. memo23 {"noResults": true}): the area is empty, not broken
    is_empty_marker: Callable[[dict[str, Any]], bool] | None = None
    # items left out on purpose (e.g. a permanently closed place): counted as `skipped`, not `unparsed`
    skip: Callable[[dict[str, Any]], bool] | None = None
    # a source that is not an Apify actor (Google Places) or needs a step after the actor (portfolio's Haiku read):
    # fetch(source config, areas, max items per area, max usd, conn) -> ActorRun, replacing run_actor
    fetch: Callable[..., ActorRun] | None = None
    apify: bool = True  # False: no Apify credit check (the source is metered elsewhere)
    # (raw items) -> area ids that one of the items says are incomplete (portfolio: a city whose operator page was
    # blocked or not returned); only those areas are partial, the run's other areas keep their own state
    area_failures: Callable[[list[dict[str, Any]]], list[str]] | None = None


@dataclass
class IntakeResult:
    run_id: int
    status: str
    counts: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    console_url: str = ""


def load_sources() -> dict[str, Any]:
    with open(settings.CONFIG_DIR / "sources.toml", "rb") as f:
        return tomllib.load(f)


def skipped(source: str) -> bool:
    """`skip = true` in a source's sources.toml section: left out of routine runs, coverage and the pipeline."""
    return bool(load_sources().get(source, {}).get("skip"))


def money(text: Any) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"\d[\d,]*(\.\d+)?", str(text))
    return float(m.group(0).replace(",", "")) if m else None


def event_cost(actor: str, events: dict[str, int] | None) -> float:
    """Estimated spend from charged events x the actor's FREE-tier event prices: kept as `runs.est_cost` for
    comparison; the real charge is Apify's usageTotalUsd (`counts.apify_usd`, decision log 2026-11-25)."""
    if not events:
        return 0.0
    if actor not in API_PRICE_CACHE:
        import httpx

        r = httpx.get(f"https://api.apify.com/v2/acts/{actor.replace('/', '~')}",
                      headers={"Authorization": f"Bearer {settings.secret('APIFY_TOKEN')}"}, timeout=30)
        prices = {}
        for info in r.json()["data"].get("pricingInfos") or []:
            for k, v in ((info.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}).items():
                tier = (v.get("eventTieredPricingUsd") or {}).get("FREE", {}).get("tieredEventPriceUsd")
                prices[k] = tier if tier is not None else v.get("eventPriceUsd", 0.0)
        API_PRICE_CACHE[actor] = prices
    prices = API_PRICE_CACHE[actor]
    return round(sum(n * prices.get(k, 0.0) for k, n in events.items()), 4)


def suspicious(parsed: list[Parsed], n_items: int, required: tuple[str, ...], n_unparsed: int = 0) -> list[str]:
    warnings = []
    if n_items == 0:
        return ["actor returned no items"]
    if n_unparsed > 0.2 * n_items:
        warnings.append(f"{n_unparsed} of {n_items} items could not be parsed")
    for f in required:
        have = sum(1 for p in parsed if (p.price if f == "price" else getattr(p.data, f)) is not None)
        if parsed and have < 0.7 * len(parsed):
            warnings.append(f"{f} missing in {len(parsed) - have} of {len(parsed)} items")
    prices = [p.price for p in parsed if p.price]
    if prices and len(set(prices)) == 1 and len(prices) > 5:
        warnings.append(f"every price is identical ({prices[0]})")
    return warnings


def _area_states(per_area: dict[str, int], cap_each: int, exhausted: bool, ok: bool) -> dict[str, str]:
    """complete: the actor finished the area under its cap; partial: cap hit or the actor failed after
    returning items; empty: finished with nothing; not_reached: the run stopped before getting to it."""
    out = {}
    for aid, n in per_area.items():
        if n >= cap_each:
            out[aid] = "partial"
        elif n == 0:
            out[aid] = "empty" if (exhausted and ok) else "not_reached"
        else:
            out[aid] = "complete" if (exhausted and ok) else "partial"
    return out


def spend_room(sources: dict[str, Any], run_left: float, apify: bool = True) -> tuple[float, str | None]:
    """The budget guard of a multi-run loop, checked before each actor run: what may still be spent (the run's cap
    left, and the Apify credit left minus `spend.reserve_usd`), and why nothing may (None while more than $0.02 is
    left; an unknown credit stops it). `apify.run_actor` checks the credit again itself (`apify.guard`)."""
    remaining = run_left
    account = account_remaining_usd() if apify else None
    reserve = sources["spend"].get("reserve_usd", 0.0)
    if apify and account is None:
        return 0.0, "Apify did not report the credit left (GET /v2/users/me/limits)"
    if account is not None:
        remaining = min(remaining, account - reserve)
    if remaining > 0.02:
        return remaining, None
    return remaining, (f"Apify credit left ${account:.2f} minus reserve ${reserve:.2f}" if account is not None
                       and account - reserve < run_left else f"run spend cap left ${run_left:.2f}")


def run_source(conn: sqlite3.Connection, adapter: Adapter, areas: list[Area], *, max_items: int | None = None,
               max_usd: float | None = None, rebuild_identity: bool = True) -> IntakeResult:
    """Collect a source over some areas. Records, per area, how many items came back and whether the area
    is complete (counts.areas), which is what the coverage ledger and liveness read. `rebuild_identity=False`:
    the caller rebuilds it (the CLI's after-intake hook), so it runs once per run."""
    sources = load_sources()
    cfg = sources[adapter.name]
    cap = max_usd if max_usd is not None else sources["spend"]["max_usd_per_run"]
    cap_each = max_items or cfg.get("max_items", 200)
    bad = [a.id for a in areas if a.kind != adapter.area_kind]
    if bad:
        raise ValueError(f"{adapter.name} runs on {adapter.area_kind} areas, not {bad}")
    ids = [a.id for a in areas]
    run_id = start_run(conn, "intake", adapter.name, {"actor": cfg.get("actor"), "area_kind": adapter.area_kind,
                                                     "areas": ids, "scope": "partial", "max_usd": cap,
                                                     "max_items_per_area": cap_each})
    counts: dict[str, Any] = {"items": 0, "observations": 0, "new": 0, "changed": 0, "unparsed": 0, "areas": {}}
    warnings: list[str] = []
    spent, est, console = 0.0, 0.0, ""
    raw_dir = settings.RAW_DIR / adapter.name
    raw_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    try:
        prev = {r["source_id"]: r["content_hash"] for r in conn.execute(
            "SELECT source_id, content_hash FROM latest_obs WHERE source = ?", (adapter.name,))}
        groups = [areas] if adapter.batch else [[a] for a in areas]
        for group in groups:
            gids = [a.id for a in group]
            remaining, why = spend_room(sources, cap - spent, adapter.apify)
            if why:
                warnings.append(f"stopped before {', '.join(gids)}: {why}")
                for aid in gids:
                    counts["areas"][aid] = {"items": 0, "state": "not_reached"}
                continue
            # the per-area cap also goes into the actor's own input (its maxResults etc.), not only Apify's maxItems
            if adapter.fetch:
                run = adapter.fetch({**cfg, "max_items": cap_each}, group, cap_each, remaining, conn)
            else:
                run = run_actor(cfg["actor"], adapter.build_input({**cfg, "max_items": cap_each}, group),
                                max_items=cap_each * len(group), max_charge_usd=remaining,
                                memory_mb=cfg.get("memory_mb"))
            cost = (event_cost(cfg["actor"], run.charged_events) if cfg.get("actor") else 0.0) or (run.cost_usd or 0.0)
            est += cost
            if adapter.apify:  # Apify's real charge; the cap counts whichever of real and estimate is larger
                real = run.usage_usd or 0.0
                counts["apify_usd"] = round(counts.get("apify_usd", 0.0) + real, 4)
                counts.setdefault("apify_runs", []).append(run_record(run))
                spent += max(cost, real)
            else:
                spent += cost
            console = run.console_url
            raw_path = raw_dir / f"run{run_id}_{re.sub(r'[^A-Za-z0-9]+', '_', '-'.join(gids))[:60]}.json"
            raw_path.write_text(json.dumps({"actor": cfg.get("actor"), "areas": gids, "apify_run": run.run_id,
                                            "status": run.status, "cost_usd": cost, "items": run.items}))
            ok = run.status == "SUCCEEDED"
            if not ok:
                warnings.append(f"{', '.join(gids)}: actor run {run.status} ({run.console_url})")
            per_area = {aid: 0 for aid in gids}
            parsed: list[tuple[Parsed, str | None]] = []
            unparsed = 0
            markers = [i for i in run.items if adapter.is_empty_marker and adapter.is_empty_marker(i)]
            run.items = [i for i in run.items if i not in markers]
            counts["empty_markers"] = counts.get("empty_markers", 0) + len(markers)
            skipped = [i for i in run.items if adapter.skip and adapter.skip(i)]
            run.items = [i for i in run.items if not (adapter.skip and adapter.skip(i))]
            counts["skipped"] = counts.get("skipped", 0) + len(skipped)
            for item in run.items:
                aid = area_for(adapter, item, gids)
                if aid in per_area:
                    per_area[aid] += 1
                p = adapter.parse(item, cfg)
                if not p:
                    unparsed += 1
                    continue
                for q in p if isinstance(p, list) else [p]:
                    parsed.append((q, aid))
            if run.complete is not None:  # the fetch judged it (Airbnb: every search ended under the cap)
                states = _area_states(per_area, float("inf"), run.complete, ok)
            else:
                exhausted = len(run.items) + len(skipped) < cap_each * len(group) and not run.truncated
                states = _area_states(per_area, cap_each, exhausted, ok)
            failing = adapter.area_failures(run.items) if adapter.area_failures else []
            for aid in [*(run.truncated_areas or []), *failing]:  # more in this area than it returned, or a failed page
                if states.get(aid) == "complete":
                    states[aid] = "partial"
            if run.items and not parsed:  # nothing usable came back: never coverage
                states = {aid: "failed" for aid in gids}
                warnings.append(f"{', '.join(gids)}: no item could be parsed; areas not credited")
            for aid in gids:
                counts["areas"][aid] = {"items": per_area[aid], "state": states[aid], "cost_usd": round(
                    cost / len(gids), 4)}
                if states[aid] == "partial":
                    warnings.append(f"{aid}: incomplete ({per_area[aid]} items, cap {cap_each})")
            warnings += [f"{', '.join(gids)}: {w}" for w in suspicious(
                [q for q, _ in parsed], len(run.items), adapter.required, unparsed)
                if not w.startswith("actor returned no items")]
            counts["items"] += len(run.items)
            counts["unparsed"] += unparsed
            for q, aid in parsed:
                if q.source_id in seen:
                    continue
                seen.add(q.source_id)
                if aid:
                    q.data.extra = {**(q.data.extra or {}), "area": aid}
                append_observation(conn, run_id, adapter.name, q.source_id, q.data, url=q.url, price=q.price,
                                   price_basis=q.price_basis, query_checkin=q.query_checkin,
                                   query_checkout=q.query_checkout, raw_path=str(raw_path), commit=False)
                counts["observations"] += 1
                if counts["observations"] % COMMIT_EVERY == 0:
                    conn.commit()
            conn.commit()  # before the next area's Apify wait
        for r in conn.execute("SELECT source_id, content_hash FROM observations WHERE run_id = ?", (run_id,)):
            if r["source_id"] not in prev:
                counts["new"] += 1
            elif prev[r["source_id"]] != r["content_hash"]:
                counts["changed"] += 1
        nothing = counts["items"] == 0 and not counts.get("empty_markers")
        if nothing:
            warnings.append("actor returned no items for any area")
        counts["warnings"] = warnings
        if counts.get("apify_runs"):
            counts["apify_usd"] = settle(counts["apify_runs"])
        status = "error" if nothing else "ok"
        finish_run(conn, run_id, status, counts, est_cost=round(est, 4),
                   error="; ".join(warnings) if status == "error" else None)
    except Exception as e:  # record the failure, then re-raise
        finish_run(conn, run_id, "error", counts, est_cost=round(est, 4), error=repr(e)[:500])
        raise
    if rebuild_identity:
        identity.run(conn)
    return IntakeResult(run_id, status, counts, warnings, round(spent, 4), console)  # spent: max(real, est) per run


# Runs made before areas existed (2026-11-24 morning) queried whole cities or one Airbnb box.
LEGACY_CITY_COVERS = {"Mountain View"}  # zip and city areas whose city is Mountain View


def _overlap(a: list[float], b: list[float]) -> float:
    """Fraction of box a (s, w, n, e) inside box b."""
    s, w, n, e = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if s >= n or w >= e:
        return 0.0
    return (n - s) * (e - w) / ((a[2] - a[0]) * (a[3] - a[1]))


# Craigslist's whole-city run hit the 300-item cap; Airbnb's (tri_angle) box runs were ranked subsets
LEGACY_PARTIAL = {"craigslist", "airbnb"}
# Actors whose area search returned a ranked subset, not everything in the area: their runs never count as complete
# (tri_angle's Mountain View box run 61 returned 33 of the 51 listings known inside the box). Airbnb through kaix
# (2026-11-25) is complete when every search of the box ended on its own (intake/airbnb.py fetch).
RANKED_ACTORS = {"tri_angle/airbnb-scraper"}


def coverage(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """(source, area id) -> {run_id, date, state, items, legacy} from the latest run that reached the area.
    state: complete | empty | partial (cap hit or actor failed). Areas never reached are absent (TODO)."""
    from .. import intake

    areas = load_areas()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    legacy_box = load_sources().get("airbnb", {}).get("box")
    for r in conn.execute("SELECT id, source, started_at, params, counts FROM runs WHERE component = 'intake' "
                          "AND status = 'ok' ORDER BY id"):
        params = json.loads(r["params"])
        counts = json.loads(r["counts"] or "{}")
        if (params.get("reparse_of") and "areas" not in params) or r["source"] not in intake.ADAPTERS:
            continue
        kind = intake.ADAPTERS[r["source"]].area_kind
        base = {"run_id": r["id"], "date": r["started_at"][:10]}
        if "areas" in params:
            if counts.get("items") and not counts.get("observations") and counts.get("unparsed") == counts.get("items"):
                continue  # every item unparseable (run 17, before 'failed' existed): not coverage
            per = counts.get("areas") or {aid: {"state": "complete", "items": None} for aid in params["areas"]}
            for aid, a in per.items():
                if a.get("state") in ("complete", "empty", "partial"):
                    st = "partial" if params.get("actor") in RANKED_ACTORS and a["state"] == "complete" else a["state"]
                    out[(r["source"], aid)] = {**base, "state": st, "items": a.get("items"),
                                               "legacy": False}
            continue
        cities = set(params.get("cities") or [])  # legacy whole-city / box runs
        if kind == "box":
            covered = [a.id for a in areas if a.kind == "box" and legacy_box
                       and any(c.endswith(" box") for c in cities) and _overlap(a.bbox, legacy_box) >= 0.9]
        else:
            covered = [a.id for a in areas if a.kind == kind and a.city in (cities & LEGACY_CITY_COVERS)]
        state = "partial" if r["source"] in LEGACY_PARTIAL else "complete"
        for aid in covered:
            out[(r["source"], aid)] = {**base, "state": state, "items": None, "legacy": True}
    return out


def done(cov: dict, source: str, area_id: str) -> bool:
    c = cov.get((source, area_id))
    return bool(c) and c["state"] in ("complete", "empty")


def reparse(conn: sqlite3.Connection, adapter: Adapter, raw_file: str, covers: str | None = None,
            rebuild_identity: bool = True, changed_only: bool = False) -> dict[str, Any]:
    """Parse a saved raw file again as a new (targeted) run. Costs nothing; appends observations.

    `covers` ('complete' or 'partial') credits the file's areas in the coverage ledger: for a scrape made outside
    this database (a trial on a copy) whose areas were never recorded here. Without it a reparse is never coverage
    (the original run already is). `covers='auto'` (adapters with `area_failures`) recomputes each area's state from
    the file: partial if an item says so, else complete (empty when no item reached it), e.g. to correct a run
    whose areas were all marked partial by one failed page.

    `changed_only`: append a row only for a listing that is active, whose latest observation came from this raw file,
    and whose parse now differs from its newest intake observation (a parser that keeps a new field). Nothing else
    moves: the run (`params.changed_only`) supersedes none of the file's listings it skips (view `listings_base`),
    never adds a listing, and an inactive one stays retired (a new row's run id would bring it back)."""
    if covers == "auto" and not adapter.area_failures:
        raise ValueError(f"--covers auto needs an adapter that reports failed areas; {adapter.name} does not")
    from pathlib import Path

    blob = json.loads(Path(raw_file).read_text())
    cfg = load_sources().get(adapter.name, {})  # parsers read little config; a reparse must not need the section
    params: dict[str, Any] = {"reparse_of": raw_file, "scope": "targeted", "actor": blob.get("actor"),
                              "cities": [blob.get("city")]}
    area_ids: list[str] = []
    if covers:
        areas = load_areas()
        named = set(blob.get("areas") or [blob.get("city")])
        area_ids = [a.id for a in areas if a.kind == adapter.area_kind and (a.id in named or a.city in named)]
        if not area_ids:
            raise ValueError(f"{raw_file} names no {adapter.area_kind} area of config/areas.toml: {sorted(named)}")
        params["areas"] = area_ids
    current: dict[str, tuple[int, str]] = {}  # source_id -> (active, raw_path of the latest observation)
    last_hash: dict[str, str] = {}  # source_id -> content hash of the newest intake observation
    if changed_only:
        if covers:
            raise ValueError("--changed-only never credits coverage; drop --covers")
        params["changed_only"] = True
        current = {r[0]: (r[1], r[2] or "") for r in conn.execute(
            "SELECT source_id, active, raw_path FROM listings_base WHERE source = ?", (adapter.name,))}
        for sid, h in conn.execute("SELECT o.source_id, o.content_hash FROM observations o JOIN runs r ON r.id = o.run_id "
                                   "WHERE o.source = ? AND r.component = 'intake' ORDER BY o.id", (adapter.name,)):
            last_hash[sid] = h
    run_id = start_run(conn, "intake", adapter.name, params)
    counts = {"items": len(blob["items"]), "observations": 0, "unparsed": 0, "skipped": 0}
    if changed_only:
        counts.update(unchanged=0, inactive=0, other_file=0, not_held=0)
    seen: set[str] = set()
    gids = blob.get("areas") or []
    try:
        for item in blob["items"]:
            if adapter.skip and adapter.skip(item):
                counts["skipped"] += 1
                continue
            p = adapter.parse(item, cfg)
            if not p:
                counts["unparsed"] += 1
                continue
            aid = area_for(adapter, item, gids)  # tagged as the original run tagged it, so area runs still retire it
            for q in p if isinstance(p, list) else [p]:
                if q.source_id in seen:
                    continue
                seen.add(q.source_id)
                if aid:
                    q.data.extra = {**(q.data.extra or {}), "area": aid}
                if changed_only:
                    cur = current.get(q.source_id)
                    why = ("not_held" if cur is None else "inactive" if not cur[0]
                           else "other_file" if not cur[1].endswith(raw_file) else
                           "unchanged" if content_hash(q.data.model_dump(exclude_none=True), q.price, q.price_basis,
                                                       q.query_checkin, q.query_checkout) == last_hash.get(q.source_id)
                           else None)
                    if why:
                        counts[why] += 1
                        continue
                append_observation(conn, run_id, adapter.name, q.source_id, q.data, url=q.url, price=q.price,
                                   price_basis=q.price_basis, query_checkin=q.query_checkin,
                                   query_checkout=q.query_checkout, raw_path=raw_file, commit=False)
                counts["observations"] += 1
                if counts["observations"] % COMMIT_EVERY == 0:
                    conn.commit()
        conn.commit()
    except Exception as e:  # close the run (its commit keeps the rows appended so far), then re-raise
        finish_run(conn, run_id, "error", counts, est_cost=0.0, error=repr(e)[:500])
        raise
    if covers == "auto":
        failing = set(adapter.area_failures(blob["items"]))
        n_items = Counter(area_for(adapter, it, area_ids) for it in blob["items"])
        counts["areas"] = {aid: {"items": n_items[aid], "state": "partial" if aid in failing else
                                 "complete" if n_items[aid] else "empty"} for aid in area_ids}
    elif covers:
        counts["areas"] = {aid: {"state": covers, "items": counts["items"]} for aid in area_ids}
    finish_run(conn, run_id, "ok", counts, est_cost=0.0)
    if rebuild_identity:
        identity.run(conn)
    return {"run_id": run_id, **counts}
