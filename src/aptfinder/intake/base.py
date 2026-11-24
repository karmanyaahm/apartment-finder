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
from .apify import ActorRun, run_actor

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


def run_source(conn: sqlite3.Connection, adapter: Adapter, areas: list[Area], *, max_items: int | None = None,
               max_usd: float | None = None) -> IntakeResult:
    """One actor run covering a batch of areas (batched so the actor's start fee is paid once).
    The run's params record which areas it covered; `coverage()` derives the done/left ledger from that."""
    sources = load_sources()
    cfg = sources[adapter.name]
    cap = max_usd if max_usd is not None else sources["spend"]["max_usd_per_run"]
    bad = [a.id for a in areas if a.kind != adapter.area_kind]
    if bad:
        raise ValueError(f"{adapter.name} runs on {adapter.area_kind} areas, not {bad}")
    ids = [a.id for a in areas]
    run_id = start_run(conn, "intake", adapter.name, {"actor": cfg["actor"], "area_kind": adapter.area_kind,
                                                     "areas": ids, "scope": "partial", "max_usd": cap})
    counts: dict[str, Any] = {"items": 0, "observations": 0, "new": 0, "changed": 0, "unparsed": 0}
    warnings: list[str] = []
    spent, console = 0.0, ""
    raw_dir = settings.RAW_DIR / adapter.name
    raw_dir.mkdir(parents=True, exist_ok=True)
    try:
        prev = {r["source_id"]: r["content_hash"] for r in conn.execute(
            "SELECT source_id, content_hash FROM latest_obs WHERE source = ?", (adapter.name,))}
        run: ActorRun = run_actor(cfg["actor"], adapter.build_input(cfg, areas),
                                  max_items=max_items or cfg.get("max_items", 200), max_charge_usd=cap,
                                  memory_mb=cfg.get("memory_mb"))
        spent = event_cost(cfg["actor"], run.charged_events) or (run.cost_usd or 0.0)
        console = run.console_url
        raw_path = raw_dir / f"run{run_id}.json"
        raw_path.write_text(json.dumps({"actor": cfg["actor"], "areas": ids, "apify_run": run.run_id,
                                        "status": run.status, "cost_usd": spent, "items": run.items}))
        if run.status != "SUCCEEDED":
            warnings.append(f"actor run {run.status} ({run.console_url})")
        parsed: list[Parsed] = []
        for item in run.items:
            p = adapter.parse(item, cfg)
            if not p:
                counts["unparsed"] += 1
            else:
                parsed.extend(p if isinstance(p, list) else [p])
        warnings += suspicious(parsed, len(run.items), adapter.required, counts["unparsed"])
        if len(run.items) >= (max_items or cfg.get("max_items", 200)):
            warnings.append(f"hit the {len(run.items)}-item cap: these areas are probably incomplete")
        counts["items"] = len(run.items)
        seen: set[str] = set()
        for p in parsed:
            if p.source_id in seen:
                continue
            seen.add(p.source_id)
            append_observation(conn, run_id, adapter.name, p.source_id, p.data, url=p.url, price=p.price,
                               price_basis=p.price_basis, query_checkin=p.query_checkin,
                               query_checkout=p.query_checkout, raw_path=str(raw_path), commit=False)
            counts["observations"] += 1
        conn.commit()
        for r in conn.execute("SELECT source_id, content_hash FROM observations WHERE run_id = ?", (run_id,)):
            if r["source_id"] not in prev:
                counts["new"] += 1
            elif prev[r["source_id"]] != r["content_hash"]:
                counts["changed"] += 1
        counts["warnings"] = warnings
        status = "error" if any("no items" in w for w in warnings) else "ok"
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


def coverage(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """(source, area id) -> latest successful run covering it: {run_id, date, legacy}."""
    from .. import intake

    _, areas = load_areas()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    legacy_box = load_sources().get("airbnb", {}).get("box")
    for r in conn.execute("SELECT id, source, started_at, params FROM runs WHERE component = 'intake' "
                          "AND status = 'ok' ORDER BY id"):
        params = json.loads(r["params"])
        if params.get("reparse_of") or r["source"] not in intake.ADAPTERS:
            continue
        kind = intake.ADAPTERS[r["source"]].area_kind
        info = {"run_id": r["id"], "date": r["started_at"][:10], "legacy": False}
        if "areas" in params:
            covered = params["areas"]
        else:  # legacy city / box runs
            info["legacy"] = True
            cities = set(params.get("cities") or [])
            if kind == "box":
                covered = [a.id for a in areas if a.kind == "box" and legacy_box
                           and "Palo Alto box" in cities and _overlap(a.bbox, legacy_box) >= 0.9]
            else:
                covered = [a.id for a in areas if a.kind == kind and a.city in (cities & LEGACY_CITY_COVERS)]
        for aid in covered:
            out[(r["source"], aid)] = info
    return out


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
