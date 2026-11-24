"""Common intake machinery: run an adapter, store raw output and observations, flag suspicious output."""

from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import identity, settings
from ..config import Area, load_areas
from ..store import append_observation, finish_run, start_run
from ..store.models import ObservationData
from .apify import ActorRun, account_remaining_usd, run_actor

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


def money(text: Any) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"\d[\d,]*(\.\d+)?", str(text))
    return float(m.group(0).replace(",", "")) if m else None


def event_cost(actor: str, events: dict[str, int] | None) -> float:
    """Real spend from charged events x the actor's FREE-tier event prices (usageTotalUsd omits
    pay-per-event charges)."""
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


def run_source(conn: sqlite3.Connection, adapter: Adapter, areas: list[Area], *, max_items: int | None = None,
               max_usd: float | None = None) -> IntakeResult:
    """Collect a source over some areas. Records, per area, how many items came back and whether the area
    is complete (counts.areas), which is what the coverage ledger and liveness read."""
    sources = load_sources()
    cfg = sources[adapter.name]
    cap = max_usd if max_usd is not None else sources["spend"]["max_usd_per_run"]
    cap_each = max_items or cfg.get("max_items", 200)
    bad = [a.id for a in areas if a.kind != adapter.area_kind]
    if bad:
        raise ValueError(f"{adapter.name} runs on {adapter.area_kind} areas, not {bad}")
    ids = [a.id for a in areas]
    run_id = start_run(conn, "intake", adapter.name, {"actor": cfg["actor"], "area_kind": adapter.area_kind,
                                                     "areas": ids, "scope": "partial", "max_usd": cap,
                                                     "max_items_per_area": cap_each})
    counts: dict[str, Any] = {"items": 0, "observations": 0, "new": 0, "changed": 0, "unparsed": 0, "areas": {}}
    warnings: list[str] = []
    spent, console = 0.0, ""
    raw_dir = settings.RAW_DIR / adapter.name
    raw_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    try:
        prev = {r["source_id"]: r["content_hash"] for r in conn.execute(
            "SELECT source_id, content_hash FROM latest_obs WHERE source = ?", (adapter.name,))}
        groups = [areas] if adapter.batch else [[a] for a in areas]
        for group in groups:
            gids = [a.id for a in group]
            remaining = cap - spent
            account = account_remaining_usd()
            reserve = sources["spend"].get("reserve_usd", 0.0)
            if account is not None:
                remaining = min(remaining, account - reserve)
            if remaining <= 0.02:
                why = (f"Apify credit left ${account:.2f} minus reserve ${reserve:.2f}" if account is not None
                       and account - reserve < cap - spent else f"run spend cap ${cap:.2f}")
                warnings.append(f"stopped before {', '.join(gids)}: {why}")
                for aid in gids:
                    counts["areas"][aid] = {"items": 0, "state": "not_reached"}
                continue
            run: ActorRun = run_actor(cfg["actor"], adapter.build_input(cfg, group),
                                      max_items=cap_each * len(group), max_charge_usd=remaining,
                                      memory_mb=cfg.get("memory_mb"))
            cost = event_cost(cfg["actor"], run.charged_events) or (run.cost_usd or 0.0)
            spent += cost
            console = run.console_url
            raw_path = raw_dir / f"run{run_id}_{re.sub(r'[^A-Za-z0-9]+', '_', '-'.join(gids))[:60]}.json"
            raw_path.write_text(json.dumps({"actor": cfg["actor"], "areas": gids, "apify_run": run.run_id,
                                            "status": run.status, "cost_usd": cost, "items": run.items}))
            ok = run.status == "SUCCEEDED"
            if not ok:
                warnings.append(f"{', '.join(gids)}: actor run {run.status} ({run.console_url})")
            per_area = {aid: 0 for aid in gids}
            parsed: list[tuple[Parsed, str | None]] = []
            unparsed = 0
            for item in run.items:
                aid = gids[0] if len(gids) == 1 else (adapter.area_of(item, gids) if adapter.area_of else None)
                if aid in per_area:
                    per_area[aid] += 1
                p = adapter.parse(item, cfg)
                if not p:
                    unparsed += 1
                    continue
                for q in p if isinstance(p, list) else [p]:
                    parsed.append((q, aid))
            exhausted = len(run.items) < cap_each * len(group)
            states = _area_states(per_area, cap_each, exhausted, ok)
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
            conn.commit()
        for r in conn.execute("SELECT source_id, content_hash FROM observations WHERE run_id = ?", (run_id,)):
            if r["source_id"] not in prev:
                counts["new"] += 1
            elif prev[r["source_id"]] != r["content_hash"]:
                counts["changed"] += 1
        if counts["items"] == 0:
            warnings.append("actor returned no items for any area")
        counts["warnings"] = warnings
        status = "error" if counts["items"] == 0 else "ok"
        finish_run(conn, run_id, status, counts, est_cost=round(spent, 4),
                   error="; ".join(warnings) if status == "error" else None)
    except Exception as e:  # record the failure, then re-raise
        finish_run(conn, run_id, "error", counts, est_cost=round(spent, 4), error=repr(e)[:500])
        raise
    identity.run(conn)
    return IntakeResult(run_id, status, counts, warnings, round(spent, 4), console)


# Runs made before areas existed (2026-11-24 morning) queried whole cities or one Airbnb box.
LEGACY_CITY_COVERS = {"Mountain View"}  # zip and city areas whose city is Mountain View


def _overlap(a: list[float], b: list[float]) -> float:
    """Fraction of box a (s, w, n, e) inside box b."""
    s, w, n, e = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if s >= n or w >= e:
        return 0.0
    return (n - s) * (e - w) / ((a[2] - a[0]) * (a[3] - a[1]))


LEGACY_PARTIAL = {"craigslist"}  # its whole-city run hit the 300-item cap


def coverage(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """(source, area id) -> {run_id, date, state, items, legacy} from the latest run that reached the area.
    state: complete | empty | partial (cap hit or actor failed). Areas never reached are absent (TODO)."""
    from .. import intake

    _, areas = load_areas()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    legacy_box = load_sources().get("airbnb", {}).get("box")
    for r in conn.execute("SELECT id, source, started_at, params, counts FROM runs WHERE component = 'intake' "
                          "AND status = 'ok' ORDER BY id"):
        params = json.loads(r["params"])
        counts = json.loads(r["counts"] or "{}")
        if params.get("reparse_of") or r["source"] not in intake.ADAPTERS:
            continue
        kind = intake.ADAPTERS[r["source"]].area_kind
        base = {"run_id": r["id"], "date": r["started_at"][:10]}
        if "areas" in params:
            per = counts.get("areas") or {aid: {"state": "complete", "items": None} for aid in params["areas"]}
            for aid, a in per.items():
                if a.get("state") in ("complete", "empty", "partial"):
                    out[(r["source"], aid)] = {**base, "state": a["state"], "items": a.get("items"),
                                               "legacy": False}
            continue
        cities = set(params.get("cities") or [])  # legacy whole-city / box runs
        if kind == "box":
            covered = [a.id for a in areas if a.kind == "box" and legacy_box
                       and "Palo Alto box" in cities and _overlap(a.bbox, legacy_box) >= 0.9]
        else:
            covered = [a.id for a in areas if a.kind == kind and a.city in (cities & LEGACY_CITY_COVERS)]
        state = "partial" if r["source"] in LEGACY_PARTIAL else "complete"
        for aid in covered:
            out[(r["source"], aid)] = {**base, "state": state, "items": None, "legacy": True}
    return out


def done(cov: dict, source: str, area_id: str) -> bool:
    c = cov.get((source, area_id))
    return bool(c) and c["state"] in ("complete", "empty")


def reparse(conn: sqlite3.Connection, adapter: Adapter, raw_file: str) -> dict[str, Any]:
    """Parse a saved raw file again as a new (targeted) run. Costs nothing; appends observations."""
    from pathlib import Path

    blob = json.loads(Path(raw_file).read_text())
    cfg = load_sources()[adapter.name]
    run_id = start_run(conn, "intake", adapter.name, {"reparse_of": raw_file, "scope": "targeted",
                                                     "actor": blob.get("actor"), "cities": [blob.get("city")]})
    counts = {"items": len(blob["items"]), "observations": 0, "unparsed": 0}
    seen: set[str] = set()
    for item in blob["items"]:
        p = adapter.parse(item, cfg)
        if not p:
            counts["unparsed"] += 1
            continue
        for q in p if isinstance(p, list) else [p]:
            if q.source_id in seen:
                continue
            seen.add(q.source_id)
            append_observation(conn, run_id, adapter.name, q.source_id, q.data, url=q.url, price=q.price,
                               price_basis=q.price_basis, query_checkin=q.query_checkin,
                               query_checkout=q.query_checkout, raw_path=raw_file, commit=False)
            counts["observations"] += 1
    conn.commit()
    finish_run(conn, run_id, "ok", counts, est_cost=0.0)
    identity.run(conn)
    return {"run_id": run_id, **counts}
