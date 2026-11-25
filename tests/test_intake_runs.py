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


def test_reparse_tags_areas_and_a_zero_price_stays_unknown(conn, tmp_path):
    import json as _json

    from aptfinder.intake.base import reparse

    raw = tmp_path / "run7_94041.json"
    raw.write_text(_json.dumps({"actor": "a/b", "areas": ["94041"], "items": [{"id": "a", "p": 0}, {"id": "b"}]}))
    adapter = Adapter(name="fake", build_input=lambda cfg, areas: {}, parse=lambda item, cfg: Parsed(
        item["id"], None, ObservationData(lat=37.4, lon=-122.1, beds=2), price=item.get("p", 2500.0),
        price_basis="month"))
    import aptfinder.intake.base as b

    orig = b.load_sources
    b.load_sources = lambda: {"spend": {}}
    try:
        reparse(conn, adapter, str(raw))
    finally:
        b.load_sources = orig
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT source_id, price, price_month, price_per_bed, json_extract(data, '$.extra.area') FROM listings")}
    assert rows == {"a": (None, None, None, "94041"), "b": (2500.0, 2500.0, 1250.0, "94041")}


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


def test_skipped_source_is_refused_and_left_out_of_coverage(conn, monkeypatch, tmp_path, capsys):
    from aptfinder import pipeline
    from aptfinder.intake import cli

    monkeypatch.setattr(base, "load_sources", lambda: {"spend": {"max_usd_per_run": 1.0},
                                                       "padmapper": {"actor": "a/b", "skip": True}})
    assert base.skipped("padmapper") and not base.skipped("zumper")
    monkeypatch.setattr(base, "run_actor", lambda *a, **k: pytest.fail("a skipped source must not reach Apify"))
    with pytest.raises(SystemExit, match="padmapper is skipped"):
        cli.main(["--db", str(tmp_path / "t.db"), "run", "padmapper", "--area", "Sunnyvale"])
    cli.print_coverage(conn)
    out = capsys.readouterr().out
    assert "zumper" in out and "padmapper" not in out
    d = pipeline.overview(conn)
    assert "padmapper" not in d["intake"]["sources"]["city"] and "zumper" in d["intake"]["sources"]["city"]
    assert not any("padmapper" in c for c in next(s for s in d["stages"] if s["id"] == "intake")["commands"])


AIRBNB_BOX = Area(kind="box", id="mv", label="mv", bbox=[37.38, -122.10, 37.42, -122.06])


def airbnb_run(monkeypatch, tmp_path, conn, searches):
    """Run the real Airbnb adapter against a fake kaix: `searches(input) -> (items, page sizes in the log)`."""
    from aptfinder.intake import airbnb

    calls = []

    def run_actor(actor, run_input, *, max_items, max_charge_usd, memory_mb=None, with_log=False, **kw):
        calls.append(run_input)
        items, pages = searches(run_input)
        log = "\n".join(f"Search: found {p} listings, accepted {p} (total so far: 0)" for p in pages)
        return ActorRun(actor, f"r{len(calls)}", "SUCCEEDED", items[:max_items], 0.0, {"listing": len(items)}, "u",
                        log=log)

    monkeypatch.setattr(airbnb.apify, "run_actor", run_actor)
    monkeypatch.setattr(base, "event_cost", lambda actor, ev: 0.0001 * sum((ev or {}).values()))
    monkeypatch.setattr(base, "account_remaining_usd", lambda: 5.0)
    monkeypatch.setattr(base, "load_sources", lambda: {"spend": {"max_usd_per_run": 1.0}, "airbnb": {
        "actor": "kaix/airbnb-listing-scraper", "max_items": 4, "checkin": "2027-01-11", "checkout": "2027-02-15"}})
    monkeypatch.setattr(base.settings, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr("aptfinder.config.load_areas", lambda: [AIRBNB_BOX])
    monkeypatch.setattr("aptfinder.intake.base.load_areas", lambda: [AIRBNB_BOX])
    return calls, run_source(conn, airbnb.ADAPTER, [AIRBNB_BOX])


def kaix_item(i, lat, lon):
    return {"id": str(i), "roomType": "private_room", "sharingTitle": "Home · 1 bedroom · 1 bed · 1 shared bath",
            "location": {"lat": lat, "lng": lon}, "price": {"amount": "$2,000", "qualifier": "monthly"}}


def test_airbnb_box_under_the_cap_is_complete(conn, monkeypatch, tmp_path):
    calls, res = airbnb_run(monkeypatch, tmp_path, conn, lambda inp: (
        [kaix_item(1, 37.39, -122.09), kaix_item(2, 37.41, -122.07), kaix_item(3, 37.5, -122.0)], [18, 3]))
    assert len(calls) == 1  # the search ended on its own (last page short): no split
    assert res.counts["areas"]["mv"]["state"] == "complete" and res.counts["areas"]["mv"]["items"] == 2
    assert coverage(conn)[("airbnb", "mv")]["state"] == "complete"


def test_airbnb_capped_box_is_split_into_quadrants(conn, monkeypatch, tmp_path):
    def searches(inp):
        if len(calls_seen) == 0:  # the whole box: cap (4) hit
            calls_seen.append(1)
            return [kaix_item(i, 37.39, -122.09) for i in range(4)], [18, 18]
        calls_seen.append(1)  # each quadrant: 2 listings (one shared with the box search), search ended
        q = len(calls_seen)
        return [kaix_item(0, 37.39, -122.09), kaix_item(100 + q, inp["latitude"], inp["longitude"])], [2]

    calls_seen: list = []
    calls, res = airbnb_run(monkeypatch, tmp_path, conn, searches)
    assert len(calls) == 5  # the box, then its 4 quadrants
    assert calls[1]["maxLocationDeviation"] < calls[0]["maxLocationDeviation"]
    assert res.counts["areas"]["mv"] == {"items": 8, "state": "complete", "cost_usd": 0.0012}  # merged by id
    assert res.cost_usd == 0.0012  # every search's events
    assert coverage(conn)[("airbnb", "mv")]["state"] == "complete"


def test_airbnb_still_capped_at_max_depth_is_partial(conn, monkeypatch, tmp_path):
    from aptfinder.intake.airbnb import MAX_SPLIT_DEPTH

    calls, res = airbnb_run(monkeypatch, tmp_path, conn, lambda inp: (
        [kaix_item(f"{inp['latitude']}-{i}", inp["latitude"], inp["longitude"]) for i in range(4)], [18]))
    assert len(calls) == sum(4 ** d for d in range(MAX_SPLIT_DEPTH + 1))  # 1 + 4 + 16
    assert res.counts["areas"]["mv"]["state"] == "partial"
    assert coverage(conn)[("airbnb", "mv")]["state"] == "partial"


def test_airbnb_search_ended_needs_a_short_last_page():
    from aptfinder.intake.airbnb import search_ended

    assert search_ended("Search: found 18 listings\nSearch: found 2 listings", 128, 270)
    assert not search_ended("Search: found 18 listings", 128, 270)  # stopped on a full page: capped
    assert not search_ended("", 10, 270)  # no log: unknown, never complete
    assert not search_ended("Search: found 3 listings", 270, 270)  # item cap reached


def test_real_apify_charge_is_recorded_next_to_the_estimate(conn, fake, monkeypatch):
    calls, plan = fake
    plan["items"] = lambda inp: [{"id": f"{inp['areas'][0]}-0"}]
    real = iter([0.05, 0.07, 0.0])
    monkeypatch.setattr(base, "run_actor", lambda *a, **k: (calls.append(k) or ActorRun(
        "a/b", f"r{len(calls)}", "SUCCEEDED", plan["items"](a[1]), 0.0, {"x": 1}, "u", usage_usd=next(real),
        usage={"PROXY_RESIDENTIAL_TRANSFER_GBYTES": 0.04})))
    adapter = per_area_adapter()
    register(monkeypatch, adapter)
    res = run_source(conn, adapter, ZIPS)
    row = conn.execute("SELECT est_cost, counts FROM runs WHERE id = ?", (res.run_id,)).fetchone()
    import json

    counts = json.loads(row["counts"])
    assert row["est_cost"] == pytest.approx(0.003)  # 3 events x the fake $0.001: the estimate stays in est_cost
    assert counts["apify_usd"] == pytest.approx(0.12)
    assert [r["usd"] for r in counts["apify_runs"]] == [0.05, 0.07, 0.0]
    assert res.cost_usd == pytest.approx(0.05 + 0.07 + 0.001)  # the run cap counts max(real, estimate) per run


def test_unknown_apify_credit_stops_the_run(conn, fake, monkeypatch):
    calls, plan = fake
    plan["items"] = lambda inp: [{"id": "x"}]
    monkeypatch.setattr(base, "account_remaining_usd", lambda: None)
    adapter = per_area_adapter()
    register(monkeypatch, adapter)
    res = run_source(conn, adapter, ZIPS[:1])
    assert calls == [] and "did not report the credit" in res.warnings[0]


def test_guard_cuts_the_cap_to_credit_minus_reserve_and_refuses_below(monkeypatch):
    from aptfinder.intake import apify

    monkeypatch.setattr(apify, "credit", lambda: {"limit": 22.0, "used": 19.2, "left": 2.8, "reserve": 2.0})
    assert apify.guard(5.0) == pytest.approx(0.8)
    assert apify.guard(0.3) == pytest.approx(0.3)
    monkeypatch.setattr(apify, "credit", lambda: {"limit": 22.0, "used": 20.0, "left": 2.01, "reserve": 2.0})
    with pytest.raises(apify.SpendRefused, match="minus reserve"):
        apify.guard(1.0)
    monkeypatch.setattr(apify, "credit", lambda: {"limit": None, "used": None, "left": None, "reserve": 2.0})
    with pytest.raises(apify.SpendRefused, match="did not report"):
        apify.guard(1.0)


def test_run_actor_never_starts_when_the_guard_refuses(monkeypatch):
    from aptfinder.intake import apify

    monkeypatch.setattr(apify, "credit", lambda: {"limit": 22.0, "used": 21.0, "left": 1.0, "reserve": 2.0})
    monkeypatch.setattr(apify, "_client", lambda: pytest.fail("a refused run must not reach Apify"))
    with pytest.raises(apify.SpendRefused):
        apify.run_actor("a/b", {}, max_items=1, max_charge_usd=0.5)


def test_residential_estimate_over_the_room_refuses(monkeypatch):
    from aptfinder.intake import apify

    monkeypatch.setattr(apify, "credit", lambda tries=6: {"limit": 22.0, "used": 19.2, "left": 2.8, "reserve": 2.0})
    monkeypatch.setattr(apify, "_client", lambda: pytest.fail("a refused run must not reach Apify"))
    with pytest.raises(apify.SpendRefused, match="residential proxy"):
        apify.run_actor("apify/playwright-scraper", {}, max_items=60, max_charge_usd=1.0, est_usd=30 * 0.029)


def test_settle_rereads_each_apify_run_at_close(monkeypatch):
    from aptfinder.intake import apify

    later = {"a": 0.009, "b": 0.133, "c": None}
    monkeypatch.setattr(apify, "reread_usd", lambda rid: later[rid])
    recs = [{"run": "a,b", "usd": 0.0341}, {"run": "c", "usd": 0.01}]
    assert apify.settle(recs) == pytest.approx(0.152)  # c unreadable: keeps its at-end value
    assert recs[0]["usd"] == pytest.approx(0.142)


def test_reparse_changed_only_appends_only_changed_active_rows_of_this_file(conn, tmp_path, monkeypatch):
    """A parser that now keeps a new field: only the rows it changes get a row; unchanged, retired and other files'
    listings keep their latest row and their active state (no supersede, no resurrection)."""
    import json as _json

    from aptfinder.intake.base import reparse
    from aptfinder.store import append_observation, finish_run, start_run

    raw = tmp_path / "run1.json"
    raw.write_text(_json.dumps({"actor": "a/b", "items": [{"id": i} for i in ("same", "new-field", "gone", "absent")]}))
    other = tmp_path / "run2.json"
    r1 = start_run(conn, "intake", "fake")
    d = {"lat": 37.4, "lon": -122.1, "beds": 2}
    for sid in ("same", "new-field", "gone"):
        append_observation(conn, r1, "fake", sid, d, price=3000.0, price_basis="month", raw_path=str(raw))
    append_observation(conn, r1, "fake", "only-in-db", d, raw_path=str(raw))  # the file no longer gives it
    finish_run(conn, r1, "ok", {})
    gone = conn.execute("SELECT id FROM observations WHERE source_id = 'gone'").fetchone()[0]
    conn.execute("INSERT INTO checks (ts, author, queue, field, verdict, observation_id, stored_value, true_value) "
                 "VALUES ('2099-01-01', 'user', 'adhoc', 'active', 'wrong', ?, '\"offered\"', '\"gone\"')", (gone,))
    conn.commit()
    r2 = start_run(conn, "intake", "fake", {"scope": "targeted"})  # a newer file holds 'same' now: not this file's to touch
    append_observation(conn, r2, "fake", "same", d, price=3000.0, price_basis="month", raw_path=str(other))
    finish_run(conn, r2, "ok", {})

    def parse(item, cfg):
        extra = {"complex_home_count": 5} if item["id"] in ("new-field", "gone") else None
        return Parsed(item["id"], None, ObservationData(**d, extra=extra), price=3000.0, price_basis="month")

    import aptfinder.intake.base as b
    monkeypatch.setattr(b, "load_sources", lambda: {"spend": {}})
    adapter = Adapter(name="fake", build_input=lambda cfg, areas: {}, parse=parse)
    before = dict(conn.execute("SELECT source_id, active FROM listings").fetchall())
    res = reparse(conn, adapter, str(raw), changed_only=True)
    assert (res["observations"], res["other_file"], res["inactive"], res["not_held"]) == (1, 1, 1, 1)
    after = dict(conn.execute("SELECT source_id, active FROM listings").fetchall())
    assert after == before == {"same": 1, "new-field": 1, "gone": 0, "only-in-db": 1}
    assert conn.execute("SELECT json_extract(data, '$.extra.complex_home_count') FROM listings "
                        "WHERE source_id = 'new-field'").fetchone()[0] == 5
    # an unchanged parse appends nothing the second time
    assert reparse(conn, adapter, str(raw), changed_only=True)["unchanged"] == 1
