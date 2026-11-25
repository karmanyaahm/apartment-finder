"""Zillow building pages: every floor plan of a building, fetched raw, applied to its bedroom rows by the text reading."""

from __future__ import annotations

import json

import pytest

from aptfinder.intake import base
from aptfinder.intake import extract as ex
from aptfinder.intake import zillow_building as zb
from aptfinder.intake.apify import ActorRun
from aptfinder.store import append_observation, finish_run, start_run


def _unit(n, beds, baths, price, sqft, avail, zpid):
    return {"unitNumber": n, "beds": beds, "baths": baths, "price": price, "sqft": sqft, "availableFrom": avail,
            "zpid": zpid}


# trimmed from the probe (Apify run hc6g8NK0Ihdy0Px60, 2026-11-25)
CASA = {  # Casa Alberta, 1575 Tenaka Pl, Sunnyvale: 2 bd comes as 2 ba and as a 1.5 ba townhouse
    "page_type": "building", "zillow_id": "347154766", "building_name": "Casa Alberta",
    "floor_plans": [
        {"zpid": "465771831", "name": "One Bedroom One Bath", "beds": 1, "baths": 1, "sqft": 600, "minPrice": 3050,
         "units": [_unit("Unit P-1", 1, 1, 3050, 600, "1793430000000", "465771831"),
                   _unit("Unit S-3", 1, 1, 3095, 600, "1793430000000", "440257178")]},
        {"zpid": "465772033", "name": "Two Bedroom Two Bath", "beds": 2, "baths": 2, "sqft": 1000, "minPrice": 4025,
         "units": [_unit("Unit R-7", 2, 2, 4025, 1000, "1794038400000", "465772033"),
                   _unit("Unit T-6", 2, 2, 4025, 1000, "0", "2058423944")]},
        {"zpid": "349371000", "name": "Townhouse", "beds": 2, "baths": 1.5, "sqft": 1100, "minPrice": 4150,
         "units": [_unit("Unit -4", 2, 1.5, 4150, 1100, "1793430000000", "349371000")]}]}
TILLERY = {  # The Tillery, 1720 Villa St, Mountain View: the page's zillow_id is a unit's, not ours
    "page_type": "building", "zillow_id": "452170900", "building_name": "The Tillery",
    "floor_plans": [
        {"zpid": "452170900", "name": "Plan 1M", "beds": 1, "baths": 1, "sqft": 795, "minPrice": 4924,
         "units": [_unit("Unit 215", 1, 1, 4924, 795, "0", "452170900")]},
        {"zpid": "453298809", "name": "Plan 1B", "beds": 1, "baths": 1, "sqft": 677, "minPrice": 5216,
         "units": [_unit("Unit 411", 1, 1, 5216, 677, "1794816000000", "453298809")]},
        {"zpid": "448916612", "name": "Plan 2D", "beds": 2, "baths": 2, "sqft": 984, "minPrice": 6542,
         "units": [_unit("Unit 409", 2, 2, 6542, 984, "1798099200000", "448916612")]}]}
NEIGHBOUR = {"page_type": "building", "zillow_id": "999", "building_name": "Someone else",
             "floor_plans": [{"zpid": "998", "beds": 3, "baths": 3, "sqft": 1500, "minPrice": 5000, "units": []}]}


def test_bed_facts_fill_only_what_every_plan_agrees_on():
    page = {**CASA, "_fetched_at": "2026-11-25"}
    one = zb.bed_facts(page, 1.0)
    assert {k: one[k] for k in ("baths", "sqft", "price", "available_from")} == {
        "baths": 1, "sqft": 600, "price": 3050, "available_from": "2026-10-31"}
    two = zb.bed_facts(page, 2)
    assert "baths" not in two and "sqft" not in two  # 2 ba and 1.5 ba: unknown, the options kept
    assert [(p["name"], p["baths"], p["sqft"], p["price"]) for p in two["plans"]] == [
        ("Two Bedroom Two Bath", 2, 1000, 4025), ("Townhouse", 1.5, 1100, 4150)]
    assert two["price"] == 4025 and two["available_from"] == "2026-10-31"
    assert zb._date("0", "2026-11-25") == "2026-11-25"  # Unit T-6's '0' = now (the fetch date)
    assert zb.bed_facts(page, 3) == {}  # no 3 bd plan: nothing
    tl = zb.bed_facts(TILLERY, 1)
    assert tl["baths"] == 1 and "sqft" not in tl and tl["price"] == 4924  # 795 and 677 sqft: no single sqft


def test_a_page_counts_only_for_its_own_building():
    assert zb.is_ours(CASA, "347154766")  # zillow_id
    assert zb.is_ours(TILLERY, "448916612")  # one of its units
    assert not zb.is_ours(NEIGHBOUR, "448916612")
    assert not zb.is_ours({**CASA, "page_type": "property"}, "347154766")


def _rows(conn):
    run = start_run(conn, "intake", "zillow", {"scope": "full"})
    for sid, zpid, beds, price, addr, lat in (
            ("347154766:1bd", 347154766, 1, 3050, "1575 Tenaka Pl, Sunnyvale, CA 94087", 37.341934),
            ("347154766:2bd", 347154766, 2, 4025, "1575 Tenaka Pl, Sunnyvale, CA 94087", 37.341934),
            ("448916612:1bd", 448916612, 1, 4924, "1720 Villa St, Mountain View, CA 94041", 37.398403),
            ("448916612:2bd", 448916612, 2, 6126, "1720 Villa St, Mountain View, CA 94041", 37.398403),
            ("111:3bd", 111, 3, 7000, "1 Elsewhere St, Palo Alto, CA 94301", 37.44)):
        append_observation(conn, run, "zillow", sid, {"address": addr, "beds": beds, "lat": lat, "lon": -122.0,
                                                       "kind": "apartment", "property_name": "Bldg",
                                                       "floor_plan": sid.split(":")[1], "extra": {"zpid": zpid}},
                           url="https://x", price=price, price_basis="month")
    finish_run(conn, run, "ok", {})


@pytest.fixture
def fake(monkeypatch, tmp_path):
    calls = []
    by_lat = {"37.341934": CASA, "37.398403": TILLERY, "37.440000": NEIGHBOUR}

    def run_actor(actor, run_input, *, max_items, max_charge_usd, **kw):
        calls.append(run_input["Urls"])
        items = [{**by_lat[u.split("/")[-2].split(",")[0]], "original_url": u} for u in run_input["Urls"]]
        return ActorRun(actor, f"r{len(calls)}", "SUCCEEDED", items, 0.0, {"page": len(items)}, "u")

    monkeypatch.setattr("aptfinder.intake.apify.run_actor", run_actor)
    monkeypatch.setattr(base, "event_cost", lambda actor, ev: 0.001 * sum((ev or {}).values()))
    monkeypatch.setattr(base, "account_remaining_usd", lambda: 5.0)
    monkeypatch.setattr(base, "load_sources", lambda: {"spend": {"max_usd_per_run": 1.0, "reserve_usd": 1.0}})
    monkeypatch.setattr(zb, "load_sources", base.load_sources)
    monkeypatch.setattr(zb.settings, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(zb.settings, "DATA_DIR", tmp_path)
    return calls


def test_fetch_is_raw_only_and_skips_another_buildings_page(conn, fake):
    _rows(conn)
    before = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert zb.fetch(conn, max_usd=1.0, dry_run=True)["to_fetch"] == 3  # one per building, not per row
    res = zb.fetch(conn, max_usd=1.0)
    assert len(fake) == 1 and any(u.endswith("/37.341934,-122.000000_ll/") for u in fake[0])
    assert res["fetched"] == 2 and res["mismatched"][0]["zpid"] == "111"
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == before
    run = conn.execute("SELECT * FROM runs WHERE source = 'zillow_building'").fetchone()
    assert json.loads(run["params"])["scope"] == "targeted" and run["status"] == "ok"
    assert not [k for k in base.coverage(conn) if k[0] == "zillow_building"]
    assert set(zb.load_buildings()) == {"347154766", "448916612"}
    assert zb.fetch(conn, max_usd=1.0)["to_fetch"] == 1  # only the mismatched one is tried again


def test_extract_applies_the_building_to_its_bedroom_rows_and_again_after_a_new_scrape(conn, fake):
    _rows(conn)
    zb.fetch(conn, max_usd=1.0)
    stub = lambda batch, model, cap: ([{"key": b["key"], "facts": {}} for b in batch], 0.0)  # noqa: E731
    ex.extract(conn, "zillow", ask_fn=stub)

    def row(sid):
        r = conn.execute("SELECT data, price FROM latest_obs WHERE source_id = ?", (sid,)).fetchone()
        return json.loads(r["data"]), r["price"]

    d, p = row("347154766:1bd")
    assert (d["baths"], d["sqft"], p) == (1, 600, 3050)
    assert d["extra"]["extracted"]["units"]["baths"] == {"value": 1, "from": "zillow building"}
    d, p = row("347154766:2bd")
    assert d.get("baths") is None and len(d["extra"]["building_plans"]) == 2 and p == 4025
    d, _ = row("448916612:2bd")
    assert d["baths"] == 2 and d["sqft"] == 984
    assert row("111:3bd")[0].get("baths") is None  # its page was another building's
    # the next search run writes the row again without baths; the reading after it (cached) fills it again
    run = start_run(conn, "intake", "zillow", {"scope": "full"})
    append_observation(conn, run, "zillow", "448916612:2bd", {
        "address": "1720 Villa St, Mountain View, CA 94041", "beds": 2, "lat": 37.398403, "lon": -122.0,
        "kind": "apartment", "property_name": "Bldg", "floor_plan": "2bd", "extra": {"zpid": 448916612}},
        url="https://x", price=6126, price_basis="month")
    finish_run(conn, run, "ok", {})
    assert row("448916612:2bd")[0].get("baths") is None
    ex.extract(conn, "zillow", ask_fn=stub)
    assert row("448916612:2bd")[0]["baths"] == 2
