"""run_source against a fake Apify: per-area states, batching, caps, coverage credit, liveness."""

from __future__ import annotations

import pytest

from aptfinder.config import Area
from aptfinder.intake import base
from aptfinder.intake.apify import ActorRun
from aptfinder.intake.base import Adapter, Parsed, coverage, done, run_source
from aptfinder.store.models import ObservationData

ZIPS = [Area(kind="zip", id=z, label=z, city="Mountain View") for z in ("94040", "94041", "94043")]


def item_parser(item, cfg):
    return Parsed(item["id"], None, ObservationData(lat=37.4, lon=-122.1, address=f"{item['id']} A St, Mountain View",
                                                     beds=2), price=3000.0, price_basis="month")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    calls = []
    plan: dict = {}

    def run_actor(actor, run_input, *, max_items, max_charge_usd, memory_mb=None, **kw):
        calls.append({"input": run_input, "max_items": max_items})
        items = plan["items"](run_input)[:max_items]
        return ActorRun(actor, f"r{len(calls)}", plan.get("status", "SUCCEEDED"), items, 0.0, {"x": len(items)}, "u")

    monkeypatch.setattr(base, "run_actor", run_actor)
    monkeypatch.setattr(base, "event_cost", lambda actor, ev: 0.001 * sum((ev or {}).values()))
    monkeypatch.setattr(base, "account_remaining_usd", lambda: plan.get("account", 5.0))
    monkeypatch.setattr(base, "load_sources", lambda: {"spend": {"max_usd_per_run": 1.0},
                                                       "fake": {"actor": "a/b", "max_items": 3}})
    monkeypatch.setattr(base.settings, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr("aptfinder.intake.base.load_areas", lambda: ZIPS)
    return calls, plan


def per_area_adapter(**kw):
    return Adapter(name="fake", build_input=lambda cfg, areas: {"areas": [a.id for a in areas]},
                   parse=item_parser, **kw)


def register(monkeypatch, adapter):
    import aptfinder.intake as intake

    monkeypatch.setitem(intake.ADAPTERS, "fake", adapter)


def test_one_run_per_area_with_states(conn, fake, monkeypatch):
    calls, plan = fake
    sizes = {"94040": 2, "94041": 3, "94043": 0}  # complete, cap hit (cap 3), empty
    plan["items"] = lambda inp: [{"id": f"{inp['areas'][0]}-{i}"} for i in range(sizes[inp["areas"][0]])]
    adapter = per_area_adapter()
    register(monkeypatch, adapter)
    res = run_source(conn, adapter, ZIPS)
    assert len(calls) == 3 and all(c["max_items"] == 3 for c in calls)
    assert {a: v["state"] for a, v in res.counts["areas"].items()} == {
        "94040": "complete", "94041": "partial", "94043": "empty"}
    cov = coverage(conn)
    assert done(cov, "fake", "94040") and done(cov, "fake", "94043") and not done(cov, "fake", "94041")
    area = conn.execute("SELECT json_extract(data, '$.extra.area') FROM observations LIMIT 1").fetchone()[0]
    assert area == "94040"


def test_batch_attributes_items_and_scales_cap(conn, fake, monkeypatch):
    calls, plan = fake
    plan["items"] = lambda inp: [{"id": "1", "zip": "94040"}, {"id": "2", "zip": "94041"}, {"id": "3", "zip": "94041"}]
    adapter = per_area_adapter(batch=True, area_of=lambda item, ids: item["zip"] if item["zip"] in ids else None)
    register(monkeypatch, adapter)
    res = run_source(conn, adapter, ZIPS)
    assert len(calls) == 1 and calls[0]["max_items"] == 9
    assert {a: (v["items"], v["state"]) for a, v in res.counts["areas"].items()} == {
        "94040": (1, "complete"), "94041": (2, "complete"), "94043": (0, "empty")}


def test_failed_actor_is_not_coverage(conn, fake, monkeypatch):
    calls, plan = fake
    plan["status"] = "FAILED"
    plan["items"] = lambda inp: [{"id": f"{inp['areas'][0]}-0"}]
    adapter = per_area_adapter()
    register(monkeypatch, adapter)
    res = run_source(conn, adapter, ZIPS[:1])
    assert res.counts["areas"]["94040"]["state"] == "partial"
    assert not done(coverage(conn), "fake", "94040")


def test_listing_goes_inactive_after_complete_rerun_of_its_area(conn, fake, monkeypatch):
    calls, plan = fake
    adapter = per_area_adapter()
    register(monkeypatch, adapter)
    plan["items"] = lambda inp: [{"id": "a"}, {"id": "b"}]
    run_source(conn, adapter, ZIPS[:1])
    plan["items"] = lambda inp: [{"id": "a"}]  # b taken down
    run_source(conn, adapter, ZIPS[:1])
    active = dict(conn.execute("SELECT source_id, active FROM listings").fetchall())
    assert active == {"a": 1, "b": 0}
    # a partial rerun proves nothing about b
    plan["status"] = "FAILED"
    plan["items"] = lambda inp: [{"id": "a"}]
    run_source(conn, adapter, ZIPS[:1])
    assert dict(conn.execute("SELECT source_id, active FROM listings").fetchall())["b"] == 0


def test_budget_guard_stops_before_running_out(conn, fake, monkeypatch):
    calls, plan = fake
    plan["account"] = 0.25  # below the reserve
    plan["items"] = lambda inp: [{"id": "x"}]
    adapter = per_area_adapter()
    register(monkeypatch, adapter)
    base_sources = base.load_sources()
    monkeypatch.setattr(base, "load_sources", lambda: {**base_sources, "spend": {"max_usd_per_run": 1.0,
                                                                                  "reserve_usd": 0.3}})
    res = run_source(conn, adapter, ZIPS[:2])
    assert calls == [] and all(v["state"] == "not_reached" for v in res.counts["areas"].values())
    assert "Apify credit left" in res.warnings[0]


def test_reparse_supersedes_listings_it_no_longer_produces(conn, tmp_path):
    import json as _json

    from aptfinder.intake.base import reparse
    from aptfinder.store import append_observation, start_run

    raw = tmp_path / "run1.json"
    raw.write_text(_json.dumps({"actor": "a/b", "items": [{"id": "new-id"}]}))
    r = start_run(conn, "intake", "fake")
    append_observation(conn, r, "fake", "old-id", {"lat": 37.4, "lon": -122.1}, raw_path=str(raw))
    adapter = per_area_adapter()
    import aptfinder.intake.base as b

    orig = b.load_sources
    b.load_sources = lambda: {"fake": {"actor": "a/b"}, "spend": {}}
    try:
        reparse(conn, adapter, str(raw))
    finally:
        b.load_sources = orig
    active = dict(conn.execute("SELECT source_id, active FROM listings").fetchall())
    assert active == {"old-id": 0, "new-id": 1}


def test_unparseable_run_is_not_coverage_and_empty_marker_is_empty(conn, fake, monkeypatch):
    calls, plan = fake
    adapter = Adapter(name="fake", build_input=lambda cfg, areas: {"areas": [a.id for a in areas]},
                      parse=lambda item, cfg: None, is_empty_marker=lambda i: bool(i.get("noResults")))
    register(monkeypatch, adapter)
    plan["items"] = lambda inp: [{"junk": 1}]
    res = run_source(conn, adapter, ZIPS[:1])
    assert res.counts["areas"]["94040"]["state"] == "failed" and not done(coverage(conn), "fake", "94040")
    plan["items"] = lambda inp: [{"noResults": True}]
    res = run_source(conn, adapter, ZIPS[1:2])
    assert res.counts["areas"]["94041"]["state"] == "empty" and done(coverage(conn), "fake", "94041")


def test_cli_max_items_reaches_actor_input(conn, fake, monkeypatch):
    calls, plan = fake
    seen = {}
    adapter = Adapter(name="fake", parse=item_parser,
                      build_input=lambda cfg, areas: seen.setdefault("cap", cfg["max_items"]) and {"areas": [a.id for a in areas]})
    register(monkeypatch, adapter)
    plan["items"] = lambda inp: []
    run_source(conn, adapter, ZIPS[:1], max_items=7)
    assert seen["cap"] == 7


def test_area_of_overrides_single_area_tagging(conn, fake, monkeypatch):
    calls, plan = fake
    adapter = Adapter(name="fake", parse=item_parser, build_input=lambda cfg, areas: {"areas": [a.id for a in areas]},
                      area_of=lambda item, ids: ids[0] if item["id"] == "in" else None)
    register(monkeypatch, adapter)
    plan["items"] = lambda inp: [{"id": "in"}, {"id": "out"}]
    res = run_source(conn, adapter, ZIPS[:1])
    assert res.counts["areas"]["94040"]["items"] == 1
    tags = dict(conn.execute("SELECT source_id, json_extract(data, '$.extra.area') FROM observations").fetchall())
    assert tags == {"in": "94040", "out": None}
